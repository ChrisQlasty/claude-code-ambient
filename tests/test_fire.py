import io
import subprocess
import sys
from typing import Any

import pytest

from ambient import fire, main, paths


class FakeStdin(io.BytesIO):
    def __init__(self, data: bytes, *, tty: bool = False) -> None:
        super().__init__(data)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def run_fire(data: bytes, **kwargs: Any) -> list[str]:
    spawned: list[str] = []
    fire.fire(FakeStdin(data, **kwargs), spawn=spawned.append)
    return spawned


@pytest.mark.parametrize("event", ["Stop", "UserPromptSubmit"])
def test_handled_events_spawn_a_worker(event: str) -> None:
    payload = f'{{"hook_event_name": "{event}", "session_id": "s", "cwd": "/x"}}'.encode()
    assert run_fire(payload) == [event]


@pytest.mark.parametrize(
    "payload", [b'{"hook_event_name": "Notification"}', b"{}", b"", b"[1, 2]", b'"Stop"']
)
def test_other_payloads_are_ignored(payload: bytes) -> None:
    assert run_fire(payload) == []


def test_tty_stdin_is_not_read() -> None:
    assert run_fire(b'{"hook_event_name": "Stop"}', tty=True) == []


def test_invalid_json_is_logged_not_raised() -> None:
    assert run_fire(b"not json") == []
    assert "invalid hook JSON" in paths.log_file().read_text()


def test_spawn_failure_is_logged_not_raised() -> None:
    def boom(event: str) -> None:
        raise OSError("no fork for you")

    fire.fire(FakeStdin(b'{"hook_event_name": "Stop"}'), spawn=boom)
    assert "no fork for you" in paths.log_file().read_text()


def test_spawn_worker_detaches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    fire.spawn_worker("Stop")
    [(args, kwargs)] = calls
    assert args == [sys.executable, "-m", "ambient.worker", "Stop"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_main_exits_zero_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fire, "fire", lambda stdin: None)
    with pytest.raises(SystemExit) as exc:
        fire.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


def test_entry_point_dispatches_fire_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["ambient", "fire"])
    monkeypatch.setattr(fire, "main", lambda: called.append(True))
    main.main()
    assert called == [True]


def test_fire_path_imports_no_heavy_modules() -> None:
    # Importing typer/pydantic alone costs more than the hook's latency budget.
    code = (
        "import sys, ambient.main, ambient.fire; "
        "print(sorted(m for m in ('typer', 'pydantic', 'yaml', 'ambient.cli', 'ambient.config',"
        " 'ambient.engine', 'ambient.log') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
