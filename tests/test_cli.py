from pathlib import Path

import pytest
from typer.testing import CliRunner

from ambient.cli import app

runner = CliRunner()
EXAMPLE = Path(__file__).parent.parent / "examples" / "config.yaml"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate via the XDG root rather than $AMBIENT_CONFIG, which counts as an explicit path.
    monkeypatch.delenv("AMBIENT_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "ambient" / "config.yaml"
    path.parent.mkdir()
    return path


def test_test_console_without_config() -> None:
    result = runner.invoke(app, ["test", "console", "flash", "#f00", "--duration", "0.1"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("[console] snapshot")
    assert "#ff0000 100%" in lines[1]
    assert "#ff0000   0%" in lines[2]
    assert lines[-1].startswith("[console] restore")


def test_test_configured_device_uses_baseline() -> None:
    result = runner.invoke(
        app, ["test", "desk", "solid", "#00ff60", "--duration", "0.05", "-c", str(EXAMPLE)]
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "[desk] restore   #ffffff  60%"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["test", "lamp", "flash", "#f00"], "unknown device 'lamp'"),
        (["test", "console", "flash", "blue"], "invalid color"),
        (["test", "console", "flash", "#f00", "--times", "0"], "times"),
    ],
)
def test_test_errors(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert message in result.output


def test_test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "confg.yaml"
    result = runner.invoke(app, ["test", "console", "solid", "#f00", "-c", str(missing)])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_test_missing_env_config_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMBIENT_CONFIG", str(tmp_path / "missing.yaml"))
    result = runner.invoke(app, ["test", "console", "solid", "#f00"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_config_path(isolated_config: Path) -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.output.strip() == str(isolated_config)


def test_config_validate_ok() -> None:
    result = runner.invoke(app, ["config", "validate", "-c", str(EXAMPLE)])
    assert result.exit_code == 0, result.output
    assert "ok: 1 device(s), 3 event(s)" in result.output


def test_config_validate_missing_file() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_config_validate_unknown_driver(isolated_config: Path) -> None:
    isolated_config.write_text("devices: {lines: {driver: nanoleaf}}\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "unknown driver 'nanoleaf'" in result.output
