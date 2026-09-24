"""CLI commands for the Claude Code hook integration: simulate, install-hooks, logs."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ambient import paths
from ambient.cli import app

runner = CliRunner()

CONFIG = """
devices: {desk: {driver: console, baseline: {color: '#ffffff', brightness: 60}}}
events:
  UserPromptSubmit: [{device: desk, effect: {type: flash, color: '#3050ff', duration: 0.02}}]
"""


@pytest.fixture(autouse=True)
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AMBIENT_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "ambient" / "config.yaml"


@pytest.fixture
def configured(config_file: Path) -> Path:
    config_file.parent.mkdir(parents=True)
    config_file.write_text(CONFIG)
    return config_file


@pytest.mark.usefixtures("configured")
def test_simulate_plays_configured_effects() -> None:
    result = runner.invoke(app, ["simulate", "UserPromptSubmit"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "[desk] snapshot  #ffffff  60%"
    assert "#3050ff 100%" in lines[1]
    assert lines[-1] == "[desk] restore   #ffffff  60%"


@pytest.mark.usefixtures("configured")
def test_simulate_without_effects() -> None:
    result = runner.invoke(app, ["simulate", "Stop"])
    assert result.exit_code == 0
    assert "no effects configured for Stop" in result.output


@pytest.mark.usefixtures("configured")
def test_simulate_rejects_notification() -> None:
    assert runner.invoke(app, ["simulate", "Notification"]).exit_code == 2


def test_simulate_requires_config() -> None:
    result = runner.invoke(app, ["simulate", "Stop"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_install_hooks_dry_run_writes_nothing(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    result = runner.invoke(app, ["install-hooks", "--dry-run", "--settings", str(settings)])
    assert result.exit_code == 0, result.output
    assert '+    "UserPromptSubmit": [' in result.output
    assert "/ambient fire" in result.output
    assert not settings.exists()


def test_install_and_uninstall_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "claude" / "settings.json"  # via CLAUDE_CONFIG_DIR from conftest
    result = runner.invoke(app, ["install-hooks"])
    assert result.exit_code == 0, result.output
    assert set(json.loads(settings.read_text())["hooks"]) == {"UserPromptSubmit", "Stop"}

    assert "already up to date" in runner.invoke(app, ["install-hooks"]).output

    result = runner.invoke(app, ["uninstall-hooks"])
    assert result.exit_code == 0, result.output
    assert "backup:" in result.output
    assert json.loads(settings.read_text()) == {}


def test_install_hooks_reports_bad_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{nope")
    result = runner.invoke(app, ["install-hooks", "--settings", str(settings)])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert settings.read_text() == "{nope"


def test_logs_shows_the_tail() -> None:
    log_file = paths.log_file()
    log_file.parent.mkdir(parents=True)
    log_file.write_text("".join(f"line {i}\n" for i in range(10)))
    result = runner.invoke(app, ["logs", "-n", "2"])
    assert result.exit_code == 0
    assert result.stdout == "line 8\nline 9\n"
