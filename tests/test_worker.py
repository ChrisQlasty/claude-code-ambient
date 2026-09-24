from pathlib import Path

import pytest

from ambient import paths, worker

CONFIG = """
devices: {desk: {driver: console}}
events:
  Stop: [{device: desk, effect: {type: solid, color: '#0f0', duration: 0.01}}]
"""


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AMBIENT_CONFIG", str(path))
    return path


def test_worker_plays_the_event(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_file.write_text(CONFIG)
    assert worker.main(["Stop"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[desk] snapshot  #000000   0%"
    assert out[-1] == "[desk] restore   #000000   0%"
    assert "Stop: playing on desk" in paths.log_file().read_text()


def test_worker_without_config_does_nothing(config_file: Path) -> None:
    assert worker.main(["Stop"]) == 0
    assert "no config" in paths.log_file().read_text()


def test_worker_logs_invalid_config(config_file: Path) -> None:
    config_file.write_text("events: {Stop: nope}")
    assert worker.main(["Stop"]) == 1
    assert "invalid config" in paths.log_file().read_text()


@pytest.mark.parametrize("argv", [[], ["Notification"], ["Stop", "extra"]])
def test_worker_rejects_unhandled_events(argv: list[str]) -> None:
    assert worker.main(argv) == 2
    assert "expected one of UserPromptSubmit, Stop" in paths.log_file().read_text()
