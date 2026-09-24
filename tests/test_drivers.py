import io

from ambient.config import Baseline, DeviceConfig
from ambient.drivers import available_drivers, get_driver
from ambient.drivers.console import ConsoleDriver
from ambient.effects import RGB, Flash, Frame, Pulse


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_console_driver_is_registered() -> None:
    assert "console" in available_drivers()
    assert get_driver("console") is ConsoleDriver


def test_play_follows_timeline_and_restore_returns_baseline() -> None:
    out = io.StringIO()
    clock = FakeClock()
    config = DeviceConfig(
        driver="console", baseline=Baseline(color=RGB(255, 255, 255), brightness=60)
    )
    driver = ConsoleDriver("desk", config, out=out, sleep=clock.sleep, clock=clock)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=2, times=2))
    driver.restore(state)

    assert state == {"color": "#ffffff", "brightness": 60}
    assert clock.now == 2.0
    lines = out.getvalue().splitlines()
    assert lines[0] == "[desk] snapshot  #ffffff  60%"
    assert lines[1:5] == [
        "[desk] + 0.000s  #ff0000 100%",
        "[desk] + 0.500s  #ff0000   0%",
        "[desk] + 1.000s  #ff0000 100%",
        "[desk] + 1.500s  #ff0000   0%",
    ]
    assert lines[5] == "[desk] restore   #ffffff  60%"


class SlowConsole(ConsoleDriver):
    """Each frame takes 0.3 s to apply, like a device behind a slow network."""

    def apply_frame(self, frame: Frame) -> None:
        super().apply_frame(frame)
        assert isinstance(self._clock, FakeClock)
        self._clock.now += 0.3


def test_play_skips_frames_when_behind_but_keeps_duration_and_last_frame() -> None:
    out = io.StringIO()
    clock = FakeClock()
    driver = SlowConsole(
        "desk", DeviceConfig(driver="console"), out=out, sleep=clock.sleep, clock=clock
    )

    driver.play(Pulse(color=RGB(255, 0, 0), duration=1))

    applied = out.getvalue().splitlines()
    assert 1 < len(applied) < 20  # 20 frames at 20 fps, but only ~1 s / 0.3 s fit
    assert applied[-1].startswith("[desk] + 0.950s")
    # Overruns by at most the last two (slow) frames instead of 20 x 0.3 s.
    assert clock.now <= 1.0 + 2 * 0.3
