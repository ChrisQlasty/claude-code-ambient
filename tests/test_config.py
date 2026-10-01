from pathlib import Path

import pytest

from ambient.config import (
    ConfigError,
    json_schema,
    load_config,
    load_raw,
    parse_config,
    save_config,
    update_device,
)
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


def test_update_device_preserves_comments_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = EXAMPLE.read_text()
    path.write_text(original)
    path.chmod(0o640)

    backup = update_device(path, "lines", {"driver": "nanoleaf", "host": "10.0.0.2", "token": "t"})

    assert backup == tmp_path / "config.yaml.bak"
    assert backup.read_text() == original
    text = path.read_text()
    assert "# prints effects to the terminal; no hardware needed" in text
    config = load_config(path)
    assert config.devices["lines"].options == {"host": "10.0.0.2", "token": "t"}
    assert config.devices["desk"].baseline is not None
    assert path.stat().st_mode & 0o777 == 0o640


def test_update_device_merges_into_existing_device(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("devices:\n  lines:\n    driver: nanoleaf\n    host: old\n    port: 1\n")
    update_device(path, "lines", {"host": "new", "token": "t"})
    assert load_config(path).devices["lines"].options == {"host": "new", "port": 1, "token": "t"}


def test_update_device_creates_private_file(tmp_path: Path) -> None:
    path = tmp_path / "ambient" / "config.yaml"
    assert update_device(path, "lines", {"driver": "nanoleaf", "token": "t"}) is None
    assert load_config(path).devices["lines"].driver == "nanoleaf"
    assert path.stat().st_mode & 0o777 == 0o600


def test_update_device_refuses_invalid_result(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\n")
    with pytest.raises(ConfigError):
        update_device(path, "lines", {"baseline": {"color": "nope"}})
    assert path.read_text() == "version: 1\n"
    assert not (tmp_path / "config.yaml.bak").exists()


COMMENTED = """\
version: 1
# my devices
devices:
  desk:  # the lamp
    driver: console
    baseline: {color: '#FFF', brightness: 60}
  old: {driver: console}
events:
  Stop:
    - device: desk  # green
      effect: {type: flash, color: '#00ff60', times: 3}
"""


def test_save_config_keeps_comments_and_quoting(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(COMMENTED)
    new = {
        "version": 1,
        "devices": {"desk": {"driver": "console", "baseline": {"color": "#FFF", "brightness": 80}}},
        "events": {
            "Stop": [{"device": "desk", "effect": {"type": "pulse", "color": "#00ff60"}}],
            "UserPromptSubmit": [
                {"device": ["desk"], "effect": {"type": "solid", "color": "#00f"}}
            ],
        },
    }

    backup = save_config(path, new)

    text = path.read_text()
    assert backup is not None
    assert backup.read_text() == COMMENTED
    assert "# my devices" in text
    assert "# the lamp" in text
    assert "# green" in text
    assert "'#FFF'" in text  # unchanged scalars keep their quoting
    assert "old" not in text
    assert "times" not in text
    assert load_raw(path) == new


def test_save_config_validates_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(COMMENTED)
    with pytest.raises(ConfigError, match="unknown device"):
        save_config(
            path,
            {
                "events": {
                    "Stop": [{"device": "nope", "effect": {"type": "solid", "color": "#fff"}}]
                }
            },
        )
    assert path.read_text() == COMMENTED
    assert not (tmp_path / "config.yaml.bak").exists()


def test_save_config_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.yaml"
    assert save_config(path, {"devices": {"d": {"driver": "console"}}}) is None
    assert load_raw(path) == {"devices": {"d": {"driver": "console"}}}


def test_save_config_does_not_confuse_bools_and_ints(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("devices:\n  d: {driver: console, flag: 1}\n")
    save_config(path, {"devices": {"d": {"driver": "console", "flag": True}}})
    assert load_raw(path)["devices"]["d"]["flag"] is True


def test_load_raw_keeps_the_file_as_written(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(COMMENTED)
    raw = load_raw(path)
    assert raw["events"]["Stop"][0]["device"] == "desk"
    assert "brightness" not in raw["events"]["Stop"][0]["effect"]
    assert load_raw(tmp_path / "missing.yaml") == {}
    path.write_text("devices: [1]")
    with pytest.raises(ConfigError):
        load_raw(path)
