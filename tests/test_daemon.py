"""The daemon's device queues, unix socket listener, and `fire`'s socket-first dispatch."""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ambient import fire, paths
from ambient.config import DeviceConfig, parse_config
from ambient.daemon import __main__ as daemon_main
from ambient.daemon.listener import AlreadyRunningError, EventListener, is_listening
from ambient.daemon.queue import DeviceQueues
from ambient.effects import AnyEffect, Solid
from ambient.events import HookEvent

CONSOLE = DeviceConfig(driver="console")
RED = Solid(color="#f00", duration=0.01)


@pytest.fixture
def sock_path() -> Iterator[Path]:
    # pytest's tmp_path is too long for a unix socket on macOS (104 bytes).
    directory = Path(tempfile.mkdtemp(prefix="amb-", dir="/tmp"))
    yield directory / "d.sock"
    shutil.rmtree(directory)


class Recorder:
    """A player that records calls and can be held mid-effect."""

    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[str, list[AnyEffect]]] = []
        self.result = result
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def __call__(self, device_id: str, config: DeviceConfig, effects: list[AnyEffect]) -> bool:
        self.started.set()
        self.release.wait(5)
        self.calls.append((device_id, effects))
        return self.result


def test_queue_plays_each_device_in_order() -> None:
    player = Recorder()
    queues = DeviceQueues(player, clock=lambda: 42.0)
    blue = Solid(color="#00f")
    queues.submit("a", CONSOLE, [RED])
    queues.submit("a", CONSOLE, [blue])
    queues.submit("b", CONSOLE, [RED])
    queues.close(timeout=5)
    assert [effects for device, effects in player.calls if device == "a"] == [[RED], [blue]]
    assert ("b", [RED]) in player.calls
    status = queues.status()
    assert status["a"].last_result == "ok"
    assert status["a"].last_at == 42.0
    assert status["a"].queued == 0


def test_queue_reports_playing_and_queued() -> None:
    player = Recorder()
    player.release.clear()
    queues = DeviceQueues(player)
    queues.submit("a", CONSOLE, [RED])
    assert player.started.wait(5)
    queues.submit("a", CONSOLE, [RED])
    status = queues.status()["a"]
    assert status.playing
    assert status.queued == 1
    player.release.set()
    queues.close(timeout=5)
    assert len(player.calls) == 2


def test_queue_survives_failures() -> None:
    def broken(device_id: str, config: DeviceConfig, effects: list[AnyEffect]) -> bool:
        raise RuntimeError("boom")

    queues = DeviceQueues(broken)
    queues.submit("a", CONSOLE, [RED])
    queues.submit("a", CONSOLE, [RED])
    queues.close(timeout=5)
    assert queues.status()["a"].last_result == "failed"


def test_queue_close_timeout_bounds_the_whole_wait() -> None:
    player = Recorder()
    player.release.clear()
    queues = DeviceQueues(player)
    for device in ("a", "b", "c"):
        queues.submit(device, CONSOLE, [RED])
    start = time.monotonic()
    queues.close(timeout=0.3)
    # Waiting the timeout per device would take 0.9 s.
    assert time.monotonic() - start < 0.6
    player.release.set()


def test_daemon_entrypoint_exits_without_waiting_for_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(daemon_main, "main", lambda argv: 3)
    monkeypatch.setattr(os, "_exit", exits.append)
    daemon_main.run([])
    assert exits == [3]


def test_queue_rejects_work_after_close() -> None:
    queues = DeviceQueues(Recorder())
    queues.close()
    with pytest.raises(RuntimeError):
        queues.submit("a", CONSOLE, [RED])


def test_submit_event_resolves_the_config() -> None:
    config = parse_config(
        """
devices: {a: {driver: console}, b: {driver: console}}
events:
  Stop:
    - {device: [a, b], effect: {type: solid, color: '#f00'}}
    - {device: a, effect: {type: flash, color: '#0f0'}}
"""
    )
    player = Recorder()
    queues = DeviceQueues(player)
    assert queues.submit_event(config, "Stop") == ["a", "b"]
    assert queues.submit_event(config, "UserPromptSubmit") == []
    queues.close(timeout=5)
    assert sorted((d, len(e)) for d, e in player.calls) == [("a", 2), ("b", 1)]


def _listen(path: Path) -> tuple[EventListener, list[HookEvent], threading.Event]:
    received: list[HookEvent] = []
    got = threading.Event()

    def on_event(event: HookEvent) -> None:
        received.append(event)
        got.set()

    return EventListener(path, on_event), received, got


def _send(path: Path, data: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(path))
        sock.sendall(data)


def test_listener_delivers_events(sock_path: Path) -> None:
    listener, received, got = _listen(sock_path)
    try:
        assert sock_path.stat().st_mode & 0o077 == 0
        _send(sock_path, b'{"event": "Stop"}\n')
        assert got.wait(5)
        assert received == ["Stop"]
    finally:
        listener.close()
    assert not sock_path.exists()


@pytest.mark.parametrize(
    "data", [b"not json\n", b'{"event": "Nope"}\n', b"[1]\n", b'{"event": "Notification"}\n']
)
def test_listener_ignores_bad_messages(sock_path: Path, data: bytes) -> None:
    listener, received, got = _listen(sock_path)
    try:
        _send(sock_path, data)
        _send(sock_path, b'{"event": "UserPromptSubmit"}\n')
        assert got.wait(5)
        assert received == ["UserPromptSubmit"]
    finally:
        listener.close()


def test_listener_replaces_a_stale_socket(sock_path: Path) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()  # the file stays, nobody listens
    listener, _, _ = _listen(sock_path)
    try:
        assert is_listening(sock_path)
    finally:
        listener.close()


def test_listener_refuses_to_steal_a_live_socket(sock_path: Path) -> None:
    listener, _, _ = _listen(sock_path)
    try:
        with pytest.raises(AlreadyRunningError):
            _listen(sock_path)
    finally:
        listener.close()


def test_fire_sends_to_a_running_daemon(sock_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "daemon_socket", lambda: sock_path)
    spawned: list[str] = []
    monkeypatch.setattr(fire, "spawn_worker", spawned.append)
    listener, received, got = _listen(sock_path)
    try:
        fire.dispatch("Stop")
        assert got.wait(5)
    finally:
        listener.close()
    assert received == ["Stop"]
    assert spawned == []


def test_fire_falls_back_without_a_daemon(sock_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "daemon_socket", lambda: sock_path)
    spawned: list[str] = []
    monkeypatch.setattr(fire, "spawn_worker", spawned.append)
    fire.dispatch("Stop")
    # A socket file left by a dead daemon also falls back.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()
    fire.dispatch("UserPromptSubmit")
    assert spawned == ["Stop", "UserPromptSubmit"]


def test_send_to_daemon_message_format(sock_path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(sock_path))
        server.listen()
        assert fire.send_to_daemon("Stop", sock_path)
        conn, _ = server.accept()
        with conn:
            assert json.loads(conn.makefile("rb").readline()) == {"event": "Stop"}
