"""Config models (``~/.config/ambient/config.yaml``), YAML loading and JSON Schema export."""

import io
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from ambient.effects import Brightness, Color, Effect

EventName = Literal["UserPromptSubmit", "Notification", "Stop"]


class ConfigError(Exception):
    """The config file is missing, is not valid YAML, or fails validation."""


class Baseline(BaseModel):
    """State to restore to when a device can't report its current state."""

    model_config = ConfigDict(extra="forbid")

    color: Color
    brightness: Brightness = 100


class DeviceConfig(BaseModel):
    """A configured device. Driver-specific fields (host, token, ...) are kept as extras."""

    model_config = ConfigDict(extra="allow")

    driver: str
    baseline: Baseline | None = None

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


def _as_list(value: object) -> object:
    return [value] if isinstance(value, str) else value


class EventAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: Annotated[list[str], BeforeValidator(_as_list), Field(min_length=1)]
    effect: Effect


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    events: dict[EventName, list[EventAction]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_device_refs(self) -> "Config":
        for event, actions in self.events.items():
            for action in actions:
                for device in action.device:
                    if device not in self.devices:
                        raise ValueError(f"events.{event}: unknown device {device!r}")
        return self


def parse_config(text: str) -> Config:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    try:
        return Config.model_validate(data or {})
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: Path) -> Config:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    return parse_config(text)


def json_schema() -> dict[str, Any]:
    return Config.model_json_schema()


def update_device(path: Path, name: str, fields: dict[str, Any]) -> Path | None:
    """Merge ``fields`` into ``devices.<name>`` in the config file, creating it if needed.

    Comments and formatting are preserved. The result is validated before anything is written,
    the previous file is copied to ``<name>.bak`` first, and the write is atomic. Returns the
    backup path, or ``None`` when the file didn't exist.
    """
    rt = YAML()
    rt.preserve_quotes = True
    text = path.read_text() if path.exists() else ""
    data = rt.load(text) if text.strip() else None
    if data is None:
        data = rt.load("version: 1\ndevices: {}\n")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    if data.get("devices") is None:
        data["devices"] = {}
    if data["devices"].get(name) is None:
        data["devices"][name] = {}
    data["devices"][name].update(fields)

    buffer = io.StringIO()
    rt.dump(data, buffer)
    new_text = buffer.getvalue()
    parse_config(new_text)

    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        # The file holds device tokens, so a new one is private; an existing one keeps its mode.
        if backup:
            shutil.copymode(backup, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup
