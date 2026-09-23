from pathlib import Path

import pytest
from typer.testing import CliRunner

from ambient.cli import app
from ambient.config import load_config
from ambient.drivers.base import DiscoveredDevice, DriverError
from ambient.drivers.nanoleaf import NanoleafDriver

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
    isolated_config.write_text("devices: {lines: {driver: lifx}}\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "unknown driver 'lifx'" in result.output


LINES = DiscoveredDevice("nanoleaf", "Lines 2F4C", "10.0.0.2", 16021, {"md": "NL59"})


@pytest.fixture
def fake_nanoleaf(monkeypatch: pytest.MonkeyPatch) -> list[DiscoveredDevice]:
    """Stub out the network: discovery returns the list, pairing returns a token."""
    found = [LINES]
    monkeypatch.setattr(NanoleafDriver, "discover", classmethod(lambda cls, timeout: found))

    def pair(self: NanoleafDriver, timeout: float) -> dict[str, object]:
        return {"host": self.options.host, "port": self.options.port, "token": "tok"}

    monkeypatch.setattr(NanoleafDriver, "pair", pair)
    return found


def test_discover(fake_nanoleaf: list[DiscoveredDevice]) -> None:
    result = runner.invoke(app, ["discover", "nanoleaf"])
    assert result.exit_code == 0, result.output
    assert result.output == "nanoleaf\tLines 2F4C\t10.0.0.2:16021\tmd=NL59\n"


def test_discover_nothing(fake_nanoleaf: list[DiscoveredDevice]) -> None:
    fake_nanoleaf.clear()
    result = runner.invoke(app, ["discover"])
    assert result.output == "no devices found\n"


def test_pair_discovers_host_and_writes_config(
    fake_nanoleaf: list[DiscoveredDevice], isolated_config: Path
) -> None:
    result = runner.invoke(app, ["pair", "lines", "--driver", "nanoleaf"])
    assert result.exit_code == 0, result.output
    assert "hold the power button" in result.output
    device = load_config(isolated_config).devices["lines"]
    assert device.driver == "nanoleaf"
    assert device.options == {"host": "10.0.0.2", "port": 16021, "token": "tok"}


def test_pair_uses_configured_host(
    fake_nanoleaf: list[DiscoveredDevice], isolated_config: Path
) -> None:
    fake_nanoleaf.clear()
    isolated_config.write_text("devices:\n  lines: {driver: nanoleaf, host: 10.9.9.9}\n")
    result = runner.invoke(app, ["pair", "lines"])
    assert result.exit_code == 0, result.output
    assert load_config(isolated_config).devices["lines"].options["host"] == "10.9.9.9"
    assert "backed up" in result.output


@pytest.mark.parametrize(
    ("found", "args", "message"),
    [
        ([LINES], ["pair", "lines"], "pass --driver"),
        ([], ["pair", "lines", "--driver", "nanoleaf"], "no devices found"),
        ([LINES, LINES], ["pair", "lines", "--driver", "nanoleaf"], "several devices"),
        ([LINES], ["pair", "lines", "--driver", "console"], "doesn't need pairing"),
    ],
)
def test_pair_errors(
    fake_nanoleaf: list[DiscoveredDevice],
    isolated_config: Path,
    found: list[DiscoveredDevice],
    args: list[str],
    message: str,
) -> None:
    fake_nanoleaf[:] = found
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert message in result.output
    assert not isolated_config.exists()


def test_test_reports_driver_errors_and_still_restores(
    monkeypatch: pytest.MonkeyPatch, isolated_config: Path
) -> None:
    isolated_config.write_text("devices:\n  lines: {driver: nanoleaf, host: 10.0.0.2, token: t}\n")
    restored: list[object] = []
    monkeypatch.setattr(NanoleafDriver, "snapshot", lambda self: {"on": True})
    monkeypatch.setattr(NanoleafDriver, "restore", lambda self, state: restored.append(state))

    def unreachable(self: NanoleafDriver, effect: object) -> None:
        raise DriverError("lines: can't reach 10.0.0.2:16021 (ConnectError)")

    monkeypatch.setattr(NanoleafDriver, "play", unreachable)
    result = runner.invoke(app, ["test", "lines", "flash", "#f00"])
    assert result.exit_code == 1
    assert "can't reach" in result.output
    assert restored == [{"on": True}]
