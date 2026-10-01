from pathlib import Path

import pytest

from ambient.drivers import logitech_g102, nuphy_air75_v3


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The config, daemon socket, locks, baselines, logs and Claude's settings.json must never
    # touch the real home dir.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


@pytest.fixture(autouse=True)
def no_usb_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    # USB discovery and opening go through these; tests must never see a real device.
    monkeypatch.setattr(logitech_g102, "_enumerate", lambda product_id: [])
    monkeypatch.setattr(nuphy_air75_v3, "_enumerate", lambda product_id: [])
