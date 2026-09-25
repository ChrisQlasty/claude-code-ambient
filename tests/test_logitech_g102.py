import itertools
from typing import Any

import pytest

from ambient.config import Baseline, DeviceConfig
from ambient.drivers import get_driver, logitech_g102
from ambient.drivers.base import DriverError
from ambient.drivers.logitech_g102 import (
    FEATURE_ONBOARD_PROFILES,
    FEATURE_RGB_EFFECTS,
    Hidpp,
    HidppError,
    LogitechG102Driver,
)
from ambient.effects import RGB, Flash, Pulse
from tests.test_drivers import FakeClock

RGB_INDEX = 14
PROFILES_INDEX = 15
# Effect ids in list order, as reported by a real G102 LIGHTSYNC: off, fixed, cycle, wave, ...
EFFECTS = [0x0000, 0x0001, 0x0003, 0x0004, 0x000A]


class FakeG102:
    """Just enough HID++ 2.0 to exercise the driver: root, 0x8071 and 0x8100."""

    def __init__(self, *, mode: int = 1, profiles: bool = True) -> None:
        self.features = {FEATURE_RGB_EFFECTS: RGB_INDEX}
        if profiles:
            self.features[FEATURE_ONBOARD_PROFILES] = PROFILES_INDEX
        self.mode = mode
        self.profile = b"\x00\x01"
        self.dpi_index = 3
        self.color: tuple[int, ...] | None = None  # None: the onboard profile's own lighting
        self.effect_writes: list[bytes] = []
        self.profile_selects: list[bytes] = []
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

    def handle(self, feature: int, function: int, p: bytes) -> bytes:
        if feature == 0 and function == 0:
            return bytes([self.features.get(int.from_bytes(p[:2], "big"), 0)])
        if feature == RGB_INDEX and function == 0:
            cluster, effect = p[0], p[1]
            if cluster == 0xFF:
                return bytes([0xFF, 0xFF, 1])
            if effect == 0xFF:
                return bytes([cluster, 0xFF, 0, 2, len(EFFECTS), 0])
            return bytes([cluster, effect]) + EFFECTS[effect].to_bytes(2, "big")
        if feature == RGB_INDEX and function == 1:
            self.effect_writes.append(bytes(p[:13]))
            assert EFFECTS[p[1]] == 0x0001
            self.color = tuple(p[2:5])
            return b""
        if feature == PROFILES_INDEX:
            match function:
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


def test_onboard_mode_restores_by_reselecting_the_profile_and_dpi() -> None:
    device = FakeG102()
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(0, 255, 96), duration=1, times=2))
    assert device.color is not None
    driver.restore(state)

    assert state == {"profile": "0001", "dpi_index": 3}
    assert device.profile_selects == [b"\x00\x01"]
    assert device.color is None
    assert device.dpi_index == 3


def test_frames_are_ram_only_fixed_effects_scaled_by_brightness() -> None:
    device = FakeG102()
    driver = make_driver(device)
    driver.play(Flash(color=RGB(255, 0, 100), brightness=50, duration=1, times=2))

    # One write per distinct frame: on, off, on, off.
    assert [w[2:5] for w in device.effect_writes] == [
        bytes([128, 0, 50]),
        bytes([0, 0, 0]),
        bytes([128, 0, 50]),
        bytes([0, 0, 0]),
    ]
    for write in device.effect_writes:
        assert write[0] == 0  # cluster
        assert write[1] == EFFECTS.index(0x0001)
        assert write[5:12] == bytes(7)
        assert write[12] == 0  # never persisted to flash


def test_repeated_colors_are_sent_once() -> None:
    device = FakeG102()
    driver = make_driver(device)
    # A long, dim pulse renders many frames that round to the same color.
    driver.play(Pulse(color=RGB(4, 0, 0), duration=2))
    colors = [w[2:5] for w in device.effect_writes]
    assert all(a != b for a, b in itertools.pairwise(colors))
    assert len(colors) < 40


def test_host_mode_uses_baseline() -> None:
    device = FakeG102(mode=2)
    driver = make_driver(device, baseline=Baseline(color=RGB(255, 255, 255), brightness=60))

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    driver.restore(state)

    assert state == {"color": "#ffffff", "brightness": 60}
    assert device.color == (153, 153, 153)
    assert device.profile_selects == []


@pytest.mark.parametrize("device", [FakeG102(mode=2), FakeG102(profiles=False)])
def test_without_onboard_mode_or_baseline_asks_for_one(device: FakeG102) -> None:
    driver = make_driver(device)
    with pytest.raises(DriverError, match="baseline"):
        driver.snapshot()
    assert device.effect_writes == []


def test_restore_after_a_baseline_left_by_another_process() -> None:
    # The engine may hand restore() a persisted state from an interrupted effect.
    device = FakeG102()
    driver = make_driver(device)
    driver.restore({"color": "#00ff00", "brightness": 100})
    assert device.color == (0, 255, 0)


def test_invalid_state() -> None:
    driver = make_driver(FakeG102())
    with pytest.raises(DriverError, match="invalid saved state"):
        driver.restore({"profile": "zz", "dpi_index": 1})


def test_close_closes_transport() -> None:
    device = FakeG102()
    make_driver(device).close()
    assert device.closed


def test_missing_rgb_feature() -> None:
    device = FakeG102()
    device.features = {}
    with pytest.raises(DriverError, match="0x8071"):
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
