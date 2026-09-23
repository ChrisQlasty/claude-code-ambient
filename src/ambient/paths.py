"""Filesystem locations.

XDG-style paths on every platform (including macOS), so the config lives in
``~/.config/ambient/config.yaml`` as documented. Each root honours the matching
``XDG_*_HOME`` variable, and ``AMBIENT_CONFIG`` overrides the config file path.
"""

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or default).expanduser() / "ambient"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def config_file() -> Path:
    override = os.environ.get("AMBIENT_CONFIG")
    return Path(override).expanduser() if override else config_dir() / "config.yaml"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", "~/.cache")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", "~/.local/state")


def locks_dir() -> Path:
    return cache_dir() / "locks"


def baseline_dir() -> Path:
    return cache_dir() / "baseline"


def log_file() -> Path:
    return state_dir() / "ambient.log"


def daemon_socket() -> Path:
    return config_dir() / "ambientd.sock"
