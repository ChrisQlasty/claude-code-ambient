import io
import os
import time
from typing import Any

import pytest

from ambient import engine, paths
from ambient.config import Baseline, Config, DeviceConfig, parse_config
from ambient.drivers.base import DeviceState, Driver
from ambient.drivers.console import ConsoleDriver
from ambient.effects import RGB, Frame, Solid

RED = Solid(color=RGB(255, 0, 0), duration=0.1)
DESK = DeviceConfig(driver="console", baseline=Baseline(color=RGB(255, 255, 255), brightness=60))


def no_sleep(seconds: float) -> None:
    pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Outputs:
    """A driver factory making console drivers that print to per-device buffers."""

    def __init__(self) -> None:
        self.out: dict[str, io.StringIO] = {}

    def __call__(self, device_id: str, config: DeviceConfig) -> Driver:
        if config.driver != "console":
            raise LookupError(f"unknown driver {config.driver!r}")
        self.out[device_id] = io.StringIO()
        return ConsoleDriver(device_id, config, out=self.out[device_id], sleep=no_sleep)

    def lines(self, device_id: str) -> list[str]:
        return self.out[device_id].getvalue().splitlines()


class FailingDriver(ConsoleDriver):
    def __init__(self, *args: Any, fail_restore: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, out=io.StringIO(), sleep=no_sleep, **kwargs)
        self.fail_restore = fail_restore
        self.restored: list[DeviceState] = []
        self.closed = False

    def apply_frame(self, frame: Frame) -> None:
        raise RuntimeError("device unplugged")

    def restore(self, state: DeviceState) -> None:
        if self.fail_restore:
            raise RuntimeError("still unplugged")
        self.restored.append(state)

    def close(self) -> None:
        self.closed = True


def test_resolve_groups_effects_by_device_in_order() -> None:
    config = parse_config(
        "devices: {a: {driver: console}, b: {driver: console}}\n"
        "events:\n"
        "  Stop:\n"
        "    - {device: [a, b], effect: {type: solid, color: '#f00'}}\n"
        "    - {device: a, effect: {type: solid, color: '#0f0'}}\n"
    )
    plan = engine.resolve(config, "Stop")
    assert [e.color for e in plan["a"]] == [RGB(255, 0, 0), RGB(0, 255, 0)]
    assert [e.color for e in plan["b"]] == [RGB(255, 0, 0)]
    assert engine.resolve(config, "UserPromptSubmit") == {}


def test_play_snapshots_plays_restores_and_clears_baseline() -> None:
    outputs = Outputs()
    assert engine.play_on_device("desk", DESK, [RED, RED], driver_factory=outputs)
    assert outputs.lines("desk") == [
        "[desk] snapshot  #ffffff  60%",
        "[desk] + 0.000s  #ff0000 100%",
        "[desk] + 0.000s  #ff0000 100%",
        "[desk] restore   #ffffff  60%",
    ]
    assert engine.load_baseline("desk") is None
    assert not list(paths.baseline_dir().iterdir())


def test_baseline_left_by_a_crashed_effect_is_restored() -> None:
    engine.save_baseline("desk", {"color": "#123456", "brightness": 10})
    outputs = Outputs()
    assert engine.play_on_device("desk", DESK, [RED], driver_factory=outputs)
    lines = outputs.lines("desk")
    assert not any("snapshot" in line for line in lines)
    assert lines[-1] == "[desk] restore   #123456  10%"
    assert engine.load_baseline("desk") is None


def test_stale_baseline_is_ignored_and_replaced(caplog: pytest.LogCaptureFixture) -> None:
    engine.save_baseline("desk", {"color": "#123456", "brightness": 10})
    old = time.time() - engine.BASELINE_MAX_AGE - 60
    os.utime(engine._baseline_path("desk"), (old, old))
    outputs = Outputs()
    assert engine.play_on_device("desk", DESK, [RED], driver_factory=outputs)
    lines = outputs.lines("desk")
    assert lines[0] == "[desk] snapshot  #ffffff  60%"
    assert lines[-1] == "[desk] restore   #ffffff  60%"
    assert "ignoring baseline" in caplog.text


def test_baseline_is_persisted_while_playing() -> None:
    seen: list[DeviceState | None] = []

    class Spy(ConsoleDriver):
        def apply_frame(self, frame: Frame) -> None:
            seen.append(engine.load_baseline("desk"))

    def factory(device_id: str, config: DeviceConfig) -> Driver:
        return Spy(device_id, config, out=io.StringIO(), sleep=no_sleep)

    engine.play_on_device("desk", DESK, [RED], driver_factory=factory)
    assert seen == [{"color": "#ffffff", "brightness": 60}]


def test_failed_play_still_restores_and_closes(caplog: pytest.LogCaptureFixture) -> None:
    driver = FailingDriver("desk", DESK)
    assert not engine.play_on_device("desk", DESK, [RED], driver_factory=lambda *_: driver)
    assert driver.restored == [{"color": "#ffffff", "brightness": 60}]
    assert driver.closed
    assert engine.load_baseline("desk") is None
    assert "device unplugged" in caplog.text


def test_failed_restore_keeps_baseline_for_next_time() -> None:
    driver = FailingDriver("desk", DESK, fail_restore=True)
    assert not engine.play_on_device("desk", DESK, [RED], driver_factory=lambda *_: driver)
    assert engine.load_baseline("desk") == {"color": "#ffffff", "brightness": 60}


def test_busy_device_waits_then_drops(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    created: list[str] = []

    def factory(device_id: str, config: DeviceConfig) -> Driver:
        created.append(device_id)
        return ConsoleDriver(device_id, config, out=io.StringIO(), sleep=no_sleep)

    with engine.device_lock("desk", 0) as held:
        assert held
        played = engine.play_on_device(
            "desk",
            DESK,
            [RED],
            driver_factory=factory,
            lock_timeout=1.0,
            sleep=clock.sleep,
            clock=clock,
        )
    assert not played
    assert created == []
    assert clock.now == pytest.approx(1.0, abs=0.1)
    assert "busy" in caplog.text


def test_lock_is_released_after_playing() -> None:
    engine.play_on_device("desk", DESK, [RED], driver_factory=Outputs())
    with engine.device_lock("desk", 0) as held:
        assert held


def test_device_ids_are_safe_filenames() -> None:
    engine.save_baseline("../evil/name", {"x": 1})
    assert [p.name for p in paths.baseline_dir().iterdir()] == [".._evil_name.json"]


def test_unreadable_baseline_is_ignored() -> None:
    paths.baseline_dir().mkdir(parents=True)
    (paths.baseline_dir() / "desk.json").write_text("{not json")
    assert engine.load_baseline("desk") is None


def test_run_event_plays_every_device_and_isolates_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = Config.model_validate(
        {
            "devices": {"a": {"driver": "console"}, "b": {"driver": "nope"}},
            "events": {
                "Stop": [
                    {"device": ["a", "b"], "effect": {"type": "flash", "color": "#0f0"}},
                ]
            },
        }
    )
    outputs = Outputs()
    engine.run_event(config, "Stop", driver_factory=outputs)
    assert outputs.lines("a")[-1] == "[a] restore   #000000   0%"
    assert "b: effect failed" in caplog.text


def test_run_event_without_effects_is_a_noop() -> None:
    outputs = Outputs()
    engine.run_event(Config(), "Stop", driver_factory=outputs)
    assert outputs.out == {}
