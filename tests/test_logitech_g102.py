import itertools
import types
import weakref
from typing import Any

import pytest

from ambient.config import Baseline, DeviceConfig
from ambient.drivers import get_driver, logitech_g102
from ambient.drivers.base import DriverError
from ambient.drivers.logitech_g102 import (
    FEATURE_ONBOARD_PROFILES,
    FEATURE_PER_KEY_LIGHTING,
    FEATURE_RGB_EFFECTS,
    Hidpp,
    HidppError,
    LogitechG102Driver,
)
from ambient.effects import RGB, Flash, Pulse
from tests.test_drivers import FakeClock

RGB_INDEX = 14
PROFILES_INDEX = 15
LEDS_INDEX = 18
SW_ON = b"\x03\x07"
OFF = (0, 0, 0)


class FakeG102:
    """Just enough HID++ 2.0 to exercise the driver: root, 0x8071, 0x8081 and 0x8100.

    Like the real mouse, LED writes only show in host mode with software control enabled.
    """

    def __init__(self, *, mode: int = 1, profiles: bool = True) -> None:
        self.features = {FEATURE_RGB_EFFECTS: RGB_INDEX, FEATURE_PER_KEY_LIGHTING: LEDS_INDEX}
        if profiles:
            self.features[FEATURE_ONBOARD_PROFILES] = PROFILES_INDEX
        self.has_profiles = profiles
        self.mode = mode
        self.sw_control = b"\x00\x00"
        self.profile = b"\x00\x01"
        self.dpi_index = 3
        self.color: tuple[int, ...] | None = None  # None: the onboard profile's own lighting
        self.shown: list[tuple[int, ...]] = []
        self.zone_writes: list[bytes] = []
        self.mode_changes: list[int] = []
        self.profile_selects: list[bytes] = []
        self.staged: tuple[int, ...] | None = None
        self.pending: list[bytes] = []
        self.noise: list[bytes] = []
        self.error: int | None = None
        self.silent = False
        self.closed = False

    # --- HidTransport ---

    def write(self, data: bytes) -> None:
        assert len(data) == 20 and data[0] == 0x11 and data[1] == 0xFF
        feature, function, params = data[2], data[3] >> 4, data[4:]
        self.pending.extend(self.noise)
        if self.silent:
            return
        if self.error is not None:
            self.pending.append(bytes([0x11, 0xFF, 0xFF, data[2], data[3], self.error]) + bytes(14))
            return
        reply = self.handle(feature, function, params)
        self.pending.append(data[:4] + reply.ljust(16, b"\0"))

    def read(self, timeout: float) -> bytes:
        return self.pending.pop(0) if self.pending else b""

    def close(self) -> None:
        self.closed = True

    # --- device ---

    @property
    def in_host_control(self) -> bool:
        return (self.mode == 2 or not self.has_profiles) and self.sw_control == SW_ON

    def handle(self, feature: int, function: int, p: bytes) -> bytes:
        if feature == 0 and function == 0:
            return bytes([self.features.get(int.from_bytes(p[:2], "big"), 0)])
        if feature == RGB_INDEX and function == 5:
            if p[0] == 1:
                self.sw_control = bytes(p[1:3])
            return b"\x00" + self.sw_control
        if feature == LEDS_INDEX and function == 1:
            self.zone_writes.append(bytes(p))
            leds = [p[i : i + 4] for i in range(0, 12, 4)]
            assert [led[0] for led in leds] == [1, 2, 3] and p[12] == 0xFF
            assert len({led[1:] for led in leds}) == 1
            self.staged = tuple(leds[0][1:])
            return b""
        if feature == LEDS_INDEX and function == 7:
            if self.in_host_control and self.staged is not None:
                self.color = self.staged
                self.shown.append(self.staged)
            return b""
        if feature == PROFILES_INDEX:
            match function:
                case 1:
                    self.mode = p[0]
                    self.mode_changes.append(p[0])
                    if self.mode == 1:
                        self.color = None
                    return b""
                case 2:
                    return bytes([self.mode])
                case 3:
                    self.profile_selects.append(bytes(p[:2]))
                    self.color = None
                    self.dpi_index = 0  # re-selecting a profile resets the DPI step
                    return b""
                case 4:
                    return self.profile + b"\x00\x00"
                case 11:
                    return bytes([self.dpi_index])
                case 12:
                    self.dpi_index = p[0]
                    return b""
        raise AssertionError(f"unexpected request {feature}/{function}")


def make_driver(
    device: FakeG102, *, baseline: Baseline | None = None, **options: Any
) -> LogitechG102Driver:
    clock = FakeClock()
    config = DeviceConfig.model_validate(
        {"driver": "logitech_g102", "baseline": baseline, **options}
    )
    driver = LogitechG102Driver("mouse", config, transport=device, sleep=clock.sleep, clock=clock)
    driver.connect()
    return driver


def test_registered() -> None:
    assert get_driver("logitech_g102") is LogitechG102Driver


def test_onboard_mode_takes_control_then_hands_it_back() -> None:
    device = FakeG102()
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(0, 255, 96), duration=1, times=2))
    assert device.shown == [(0, 255, 96), OFF, (0, 255, 96), OFF]
    driver.restore(state)

    assert state == {"sw_control": "0000", "mode": 1, "profile": "0001", "dpi_index": 3}
    assert device.mode_changes == [2, 1]  # control is taken once, not per frame
    assert (device.mode, device.sw_control) == (1, b"\x00\x00")
    assert device.profile_selects == [b"\x00\x01"]
    assert device.color is None
    assert device.dpi_index == 3


def test_frames_set_all_leds_scaled_by_brightness() -> None:
    device = FakeG102()
    driver = make_driver(device)
    driver.play(Flash(color=RGB(255, 0, 100), brightness=50, duration=1, times=1))

    assert device.zone_writes[0] == bytes(
        [1, 128, 0, 50, 2, 128, 0, 50, 3, 128, 0, 50, 0xFF, 0, 0, 0]
    )
    assert device.shown == [(128, 0, 50), OFF]


def test_repeated_colors_are_sent_once() -> None:
    device = FakeG102()
    driver = make_driver(device)
    # A long, dim pulse renders many frames that round to the same color.
    driver.play(Pulse(color=RGB(4, 0, 0), duration=2))
    assert all(a != b for a, b in itertools.pairwise(device.shown))
    assert len(device.zone_writes) < 40


def test_host_mode_uses_baseline() -> None:
    device = FakeG102(mode=2)
    driver = make_driver(device, baseline=Baseline(color=RGB(255, 255, 255), brightness=60))

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    driver.restore(state)

    assert state == {"sw_control": "0000", "mode": 2, "color": "#ffffff", "brightness": 60}
    assert device.shown[-1] == (153, 153, 153)
    assert (device.mode, device.sw_control) == (2, b"\x00\x00")
    assert device.profile_selects == []


def test_without_onboard_profiles_uses_baseline() -> None:
    device = FakeG102(profiles=False)
    driver = make_driver(device, baseline=Baseline(color=RGB(0, 0, 255)))

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    driver.restore(state)

    assert state["mode"] is None
    assert device.shown == [(255, 0, 0), OFF, (0, 0, 255)]
    assert device.mode_changes == []


@pytest.mark.parametrize("device", [FakeG102(mode=2), FakeG102(profiles=False)])
def test_without_onboard_mode_or_baseline_asks_for_one(device: FakeG102) -> None:
    driver = make_driver(device)
    with pytest.raises(DriverError, match="baseline"):
        driver.snapshot()
    assert device.zone_writes == []
    assert device.mode_changes == []


def test_restore_a_state_persisted_by_an_interrupted_effect() -> None:
    # A new process restores the engine's saved baseline while the mouse is still in host mode.
    device = FakeG102(mode=2)
    device.sw_control = SW_ON
    driver = make_driver(device)
    driver.restore({"sw_control": "0000", "mode": 1, "profile": "0001", "dpi_index": 2})

    assert (device.mode, device.sw_control, device.dpi_index) == (1, b"\x00\x00", 2)
    assert device.color is None


@pytest.mark.parametrize(
    "state",
    [
        {"sw_control": "0000", "mode": 1, "profile": "zz", "dpi_index": 1},
        {"mode": 1, "profile": "0001", "dpi_index": 1},
        {"sw_control": "0000", "mode": 2},
    ],
)
def test_invalid_state(state: dict[str, Any]) -> None:
    device = FakeG102()
    with pytest.raises(DriverError, match="invalid saved state"):
        make_driver(device).restore(state)
    assert device.mode_changes == []


def test_close_closes_transport() -> None:
    device = FakeG102()
    make_driver(device).close()
    assert device.closed


@pytest.mark.parametrize("feature", [FEATURE_RGB_EFFECTS, FEATURE_PER_KEY_LIGHTING])
def test_missing_feature(feature: int) -> None:
    device = FakeG102()
    del device.features[feature]
    with pytest.raises(DriverError, match=r"0x8071.*0x8081"):
        make_driver(device)


def test_invalid_options() -> None:
    with pytest.raises(DriverError, match="invalid logitech_g102 options"):
        LogitechG102Driver("mouse", DeviceConfig(driver="logitech_g102", host="x"))


def test_discover_lists_one_entry_per_mouse(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"product_id": 0xC092, "product_string": "G102 LIGHTSYNC Gaming Mouse"}
    monkeypatch.setattr(
        logitech_g102,
        "_enumerate",
        lambda product_id: [{**entry, "usage": 0x01}, {**entry, "usage": 0x02}],
    )
    [found] = LogitechG102Driver.discover(timeout=0)
    assert (found.name, found.host, found.port) == ("G102 LIGHTSYNC Gaming Mouse", "usb", 0)
    assert found.details == {"product_id": "0xc092"}


def test_connect_without_a_mouse() -> None:
    driver = LogitechG102Driver("mouse", DeviceConfig(driver="logitech_g102"))
    with pytest.raises(DriverError, match="no Logitech G102"):
        driver.connect()


def test_hidapi_exit_cleanup_is_skipped() -> None:
    # hidapi registers hid_exit like this; running it at exit can abort the process on macOS.
    module = types.ModuleType("hid")
    calls: list[str] = []
    finalizer = weakref.finalize(module, calls.append, "hid_exit")
    other_module = types.ModuleType("other")
    other = weakref.finalize(other_module, calls.append, "other")

    logitech_g102._skip_exit_cleanup(module)

    assert not finalizer.alive
    assert other.alive
    other()
    assert calls == ["other"]


# --- HID++ transport ---


def test_hidpp_skips_notifications_and_unrelated_reports() -> None:
    device = FakeG102()
    device.noise = [b"\x02\x00\x00", bytes([0x11, 0xFF, 0x04, 0x00]) + bytes(16)]
    hidpp = Hidpp(device, timeout=1.0, clock=FakeClock())
    assert hidpp.feature_index(FEATURE_RGB_EFFECTS) == RGB_INDEX
    assert hidpp.feature_index(0x1234) is None


def test_hidpp_error_reply() -> None:
    device = FakeG102()
    device.error = 7
    hidpp = Hidpp(device, timeout=1.0, clock=FakeClock())
    with pytest.raises(HidppError, match="invalid function") as info:
        hidpp.request(RGB_INDEX, 9)
    assert info.value.code == 7


def test_hidpp_timeout() -> None:
    clock = FakeClock()

    class Silent(FakeG102):
        def read(self, timeout: float) -> bytes:
            clock.now += timeout
            return b""

    device = Silent()
    device.silent = True
    with pytest.raises(DriverError, match="no HID\\+\\+ reply"):
        Hidpp(device, timeout=0.5, clock=clock).request(0, 0, b"\x80\x71")
