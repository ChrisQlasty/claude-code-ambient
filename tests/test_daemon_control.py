"""Starting, stopping and inspecting the daemon, and its CLI commands."""

import json
import os
import signal
import subprocess

import pytest
from typer.testing import CliRunner

from ambient import paths
from ambient.cli import app
from ambient.daemon import control

runner = CliRunner()


def _write_state(pid: int, url: str = "http://127.0.0.1:8765") -> None:
    path = paths.daemon_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "url": url}))


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_running_without_a_state_file() -> None:
    assert control.running() is None


def test_running_reads_the_state_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "is_listening", lambda path: path == paths.daemon_socket())
    _write_state(os.getpid())
    assert control.running() == control.DaemonInfo(os.getpid(), "http://127.0.0.1:8765")


def test_running_ignores_a_live_pid_without_a_socket() -> None:
    # A daemon that died hard leaves its state file, and its pid may now be another process.
    _write_state(os.getpid())
    assert control.running() is None


def test_running_ignores_dead_or_garbled_state() -> None:
    _write_state(_dead_pid())
    assert control.running() is None
    paths.daemon_state_file().write_text("{nope")
    assert control.running() is None


def test_start_spawns_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    up = control.DaemonInfo(123, "http://127.0.0.1:9000")
    answers: list[control.DaemonInfo | None] = [None, None, up]
    monkeypatch.setattr(control, "running", lambda: answers.pop(0))
    info = control.start(9000, spawn=spawned.append, sleep=lambda s: None)
    assert info == up
    assert spawned == [control.daemon_command(9000)]
    assert spawned[0][1:] == ["-P", "-m", "ambient.daemon", "--port", "9000"]


def test_start_returns_a_running_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    up = control.DaemonInfo(1, "u")
    monkeypatch.setattr(control, "running", lambda: up)
    assert control.start(9000, spawn=lambda args: pytest.fail("spawned")) == up


def test_start_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "running", lambda: None)
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(control.DaemonError, match="didn't start"):
        control.start(9000, spawn=lambda args: None, sleep=sleep, clock=lambda: now[0], timeout=1)


def test_stop_when_not_running() -> None:
    assert control.stop() is False


def test_stop_leaves_a_stale_pid_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_state(os.getpid())
    signals: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(sig))
    assert control.stop() is False
    assert signal.SIGTERM not in signals


def test_start_replaces_a_stale_state_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_state(os.getpid())
    spawned: list[list[str]] = []

    def spawn(args: list[str]) -> None:
        spawned.append(args)
        monkeypatch.setattr(control, "is_listening", lambda path: True)

    info = control.start(9000, spawn=spawn, sleep=lambda s: None)
    assert spawned == [control.daemon_command(9000)]
    assert info.pid == os.getpid()


@pytest.mark.parametrize(
    ("error", "expected"), [(ProcessLookupError, True), (PermissionError, control.DaemonError)]
)
def test_stop_handles_kill_errors(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError], expected: object
) -> None:
    monkeypatch.setattr(control, "running", lambda: control.DaemonInfo(4242, "u"))

    def kill(pid: int, sig: int) -> None:
        raise error

    monkeypatch.setattr(os, "kill", kill)
    if expected is True:
        assert control.stop() is True
    else:
        with pytest.raises(control.DaemonError, match="not allowed"):
            control.stop()


def test_stop_signals_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "running", lambda: control.DaemonInfo(4242, "u"))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    alive = [True, False]
    monkeypatch.setattr(control, "_pid_alive", lambda pid: alive.pop(0))
    assert control.stop(sleep=lambda s: None) is True
    assert signals[0][0] == 4242


def test_cli_status_not_running() -> None:
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 1
    assert "isn't running" in result.output


def test_cli_status_running(monkeypatch: pytest.MonkeyPatch) -> None:
    info = control.DaemonInfo(7, "http://127.0.0.1:8765")
    monkeypatch.setattr(control, "running", lambda: info)
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "pid 7" in result.output


def test_cli_stop_not_running() -> None:
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0
    assert "isn't running" in result.output


def test_cli_ui_starts_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    ports: list[int] = []

    def start(port: int) -> control.DaemonInfo:
        ports.append(port)
        return control.DaemonInfo(7, f"http://127.0.0.1:{port}")

    monkeypatch.setattr(control, "start", start)
    result = runner.invoke(app, ["ui", "--port", "9001", "--no-open"])
    assert result.exit_code == 0, result.output
    assert ports == [9001]
    assert "http://127.0.0.1:9001" in result.output


def test_cli_ui_reports_a_failed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def start(port: int) -> control.DaemonInfo:
        raise control.DaemonError("the daemon didn't start")

    monkeypatch.setattr(control, "start", start)
    result = runner.invoke(app, ["ui", "--no-open"])
    assert result.exit_code == 1
    assert "didn't start" in result.output
