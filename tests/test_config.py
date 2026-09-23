from pathlib import Path

import pytest

from ambient.config import ConfigError, json_schema, load_config, parse_config
from ambient.effects import RGB, Flash, Pulse

EXAMPLE = Path(__file__).parent.parent / "examples" / "config.yaml"

PLAN_EXAMPLE = """
version: 1
devices:
  lines:
    driver: nanoleaf
    host: 192.168.1.50
    token: "abc"
  keyboard:
    driver: nuphy_air75_v3
  mouse:
    driver: logitech_g102
    baseline: { color: "#ffffff", brightness: 60 }
events:
  UserPromptSubmit:
    - device: lines
      effect: { type: flash, color: "#3050ff", duration: 0.6, times: 1 }
  Notification:
    - device: [lines, keyboard]
      effect: { type: pulse, color: "#ffa000", duration: 3 }
  Stop:
    - device: [lines, keyboard, mouse]
      effect: { type: flash, color: "#00ff60", duration: 2, times: 3 }
"""


def test_example_file_is_valid() -> None:
    config = load_config(EXAMPLE)
    assert set(config.events) == {"UserPromptSubmit", "Notification", "Stop"}


def test_parses_plan_example() -> None:
    config = parse_config(PLAN_EXAMPLE)

    assert config.devices["lines"].options == {"host": "192.168.1.50", "token": "abc"}
    mouse_baseline = config.devices["mouse"].baseline
    assert mouse_baseline is not None
    assert mouse_baseline.color == RGB(255, 255, 255)

    prompt = config.events["UserPromptSubmit"][0]
    assert prompt.device == ["lines"]
    assert isinstance(prompt.effect, Flash)
    assert prompt.effect.color == RGB(0x30, 0x50, 0xFF)

    notification = config.events["Notification"][0]
    assert notification.device == ["lines", "keyboard"]
    assert isinstance(notification.effect, Pulse)
    assert notification.effect.times == 1


def test_empty_file_is_empty_config() -> None:
    config = parse_config("")
    assert config.devices == {}
    assert config.events == {}


def test_short_hex_color_expands() -> None:
    config = parse_config(
        "devices: {d: {driver: console}}\n"
        "events: {Stop: [{device: d, effect: {type: solid, color: '#f0a'}}]}\n"
    )
    assert config.events["Stop"][0].effect.color == RGB(0xFF, 0x00, 0xAA)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            "events: {Stop: [{device: nope, effect: {type: solid, color: '#fff'}}]}",
            "unknown device",
        ),
        ("devices: {d: {driver: console}}\nevents: {Stopp: []}", "Stopp"),
        (
            "devices: {d: {driver: console}}\n"
            "events: {Stop: [{device: d, effect: {type: solid, color: 'red'}}]}",
            "invalid color",
        ),
        (
            "devices: {d: {driver: console}}\n"
            "events: {Stop: [{device: d, effect: {type: strobe, color: '#fff'}}]}",
            "strobe",
        ),
        (
            "devices: {d: {driver: console}}\n"
            "events: {Stop: [{device: d, effect: {type: flash, color: '#fff', times: 0}}]}",
            "times",
        ),
        ("version: 2", "version"),
        ("devices: [", "invalid YAML"),
    ],
)
def test_invalid_config(yaml_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(yaml_text)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_json_schema_describes_colors_as_strings() -> None:
    schema = json_schema()
    solid = schema["$defs"]["Solid"]["properties"]["color"]
    assert solid["type"] == "string"
