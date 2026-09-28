import itertools
import json
from typing import Any

import pytest

from ambient import paths
from ambient.config import DeviceConfig
from ambient.drivers import get_driver, nuphy_air75_v3
from ambient.drivers.base import DriverError
from ambient.drivers.nuphy_air75_v3 import CUSTOM_EFFECT, S4, NuphyAir75V3Driver
from ambient.effects import RGB, Flash, Pulse
from tests.test_drivers import FakeClock

KEY = 0xE9
OFF = (0, 0, 0)
STOCK = 19
STATE = [STOCK, 100, 4, 0, 0, 0, 255, 0, 213, 0, 100, 3, 0, 0, 255, 255, 255]


class FakeKeyboard:
    """Just enough of the S4 protocol to exercise the driver: A0, A1, D2, D5, D6 and D8.

    Like firmware 1.0.16.6, the per-key RAM table only shows in the custom effect, and a state
    write takes effect a moment later. ``flash_writes`` counts state writes.
    """

    def __init__(self, *, effect: int = STOCK, key: int = KEY, custom: bool = True) -> None:
        self.key = key
        self.custom = custom  # older firmware clamps the custom effect to a stock one
        self.profile = 0
        self.states = {0: bytearray(STATE), 1: bytearray(STATE)}
        self.states[0][0] = effect
        self.table: list[tuple[int, ...]] = [(10, 20, 30)] * 84
        self.shown: list[tuple[int, ...]] = []
        self.flash_writes: list[tuple[int, int]] = []
        self.pending_state: tuple[int, int, bytes] | None = None
        self.plain_route = False
        self.foreign_route = False
        self.replies: list[bytes] = []
        self.noise: list[bytes] = []
        self.silent = False
        self.closed = False

    # --- HidTransport ---

    def write(self, data: bytes) -> None:
        assert data[0] == 0 and len(data) == 65
        frame = data[1:]
        assert frame[0] == 0x55 and frame[3] == sum(frame[4:]) & 0xFF
        # The A1 request is always sent with key 0.
        key = 0 if frame[1] == 0xA1 else self.key
        body = bytes(b ^ key for b in frame[4:])
        length, address, handle = body[0], body[1] | body[2] << 8, body[3]
        self.replies.extend(self.noise)
        if self.silent:
            return
        payload = self.handle(frame[1], length, address, handle, body[4 : 4 + length])
        route = bytes([length, address & 0xFF, address >> 8, handle])
        if self.foreign_route:
            route = bytes([1, 2, 3, 4])
        elif not self.plain_route:
            route = bytes(b ^ self.key for b in route)
        encoded = bytes(b ^ self.key for b in payload.ljust(length, b"\0"))
        reply = (route + encoded).ljust(60, b"\0")
        self.replies.append(bytes([0xAA, frame[1], 0, sum(reply) & 0xFF]) + reply)

    def read(self, timeout: float) -> bytes:
        return self.replies.pop(0) if self.replies else b""

    def close(self) -> None:
        self.closed = True

    # --- device ---

    @property
    def effect(self) -> int:
        return self.states[self.profile][0]

    def settle(self) -> None:
        if self.pending_state is not None:
            handle, address, values = self.pending_state
            self.states[handle][address : address + len(values)] = values
            self.pending_state = None
            if self.effect == CUSTOM_EFFECT:
                self.shown.append(self.table[0])

    def handle(self, cmd: int, length: int, address: int, handle: int, payload: bytes) -> bytes:
        match cmd:
            case 0xA1:
                return bytes([0x10, 0, 1, 0xAA, 6, 0, 0, 0])
            case 0xA0:
                return bytes([self.profile, 8, 4, 6, 16, 0x82, 0, 0])
            case 0xD5:
                # The first read after a write still returns the old state.
                state = bytes(self.states[handle])
                self.settle()
                return state
            case 0xD6:
                value = payload[0]
                if address == 0 and value == CUSTOM_EFFECT and not self.custom:
                    value = 20
                self.pending_state = (handle, address, bytes([value]) + payload[1:])
                self.flash_writes.append((address, payload[0]))
                return payload
            case 0xD8:
                for i in range(0, length, 4):
                    self.table[payload[i]] = tuple(payload[i + 1 : i + 4])
                if self.effect == CUSTOM_EFFECT and payload[0] == 78:  # the frame's last write
                    self.shown.append(self.table[0])
                return payload
            case 0xD2:
                table = b"".join(bytes(c) for c in self.table) + bytes(60)
                return table[address : address + length]
        raise AssertionError(f"unexpected command 0x{cmd:02x}")


def make_driver(device: FakeKeyboard, **options: Any) -> NuphyAir75V3Driver:
    clock = FakeClock()
    config = DeviceConfig.model_validate(
        # Exact frames by default; fading has its own test.
        {"driver": "nuphy_air75_v3", "transition": 0, **options}
    )
    driver = NuphyAir75V3Driver(
        "keyboard", config, transport=device, sleep=clock.sleep, clock=clock
    )
    driver.connect()
    return driver


def marker_path() -> Any:
    return paths.cache_dir() / "nuphy" / "keyboard.json"


def test_registered() -> None:
    assert get_driver("nuphy_air75_v3") is NuphyAir75V3Driver


def test_flash_enters_custom_effect_once_then_switches_back() -> None:
    device = FakeKeyboard()
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(0, 255, 96), duration=1, times=2))
    driver.restore(state)

    assert state == {"profile": 0, "effect": STOCK}
    assert device.shown == [(0, 255, 96), OFF, (0, 255, 96), OFF]
    # Two flash writes per effect, and only the effect byte.
    assert device.flash_writes == [(0, CUSTOM_EFFECT), (0, STOCK)]
    assert bytes(device.states[0]) == bytes(STATE)
    assert not marker_path().exists()


def test_frames_scale_all_keys_by_brightness() -> None:
    device = FakeKeyboard()
    driver = make_driver(device)
    driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 100), brightness=50, duration=1))

    assert device.shown == [(128, 0, 50), OFF]
    assert set(device.table) == {OFF}


def test_fades_between_frames_by_default() -> None:
    device = FakeKeyboard()
    driver = make_driver(device, transition=0.25)
    driver.snapshot()
    driver.play(Flash(color=RGB(0, 200, 0), duration=2))

    greens = [g for _, g, _ in device.shown]
    peak = greens.index(200)
    assert greens[0] < 50 and peak > 3
    assert greens[peak:] == sorted(greens[peak:], reverse=True)
    assert greens[-1] == 0


def test_repeated_colors_are_sent_once() -> None:
    device = FakeKeyboard()
    driver = make_driver(device)
    driver.snapshot()
    driver.play(Pulse(color=RGB(4, 0, 0), duration=2))
    assert all(a != b for a, b in itertools.pairwise(device.shown))


def test_custom_effect_already_active_is_repainted_without_flash_writes() -> None:
    device = FakeKeyboard(effect=CUSTOM_EFFECT)
    device.table = [(i, 0, 255 - i) for i in range(84)]
    original = list(device.table)
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    driver.restore(state)

    assert state["effect"] == CUSTOM_EFFECT
    assert device.table == original
    assert device.flash_writes == []


def test_uses_the_active_mac_or_win_profile() -> None:
    device = FakeKeyboard()
    device.profile = 1
    device.states[1][0] = 7
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(0, 0, 255), duration=1))
    driver.restore(state)

    assert state == {"profile": 1, "effect": 7}
    assert device.states[1][0] == 7
    assert device.states[0][0] == STOCK
    assert device.flash_writes == [(0, CUSTOM_EFFECT), (0, 7)]


def test_reused_baseline_plays_on_the_active_profile() -> None:
    # A worker killed on the Win profile left its custom effect and baseline behind; the next
    # one reuses that baseline, so snapshot() is skipped.
    device = FakeKeyboard()
    device.profile = 1
    device.states[1][0] = CUSTOM_EFFECT
    baseline = {"profile": 1, "effect": 7}

    driver = make_driver(device)
    driver.play(Flash(color=RGB(0, 0, 255), duration=1))
    driver.restore(baseline)

    assert device.states[0][0] == STOCK
    assert device.states[1][0] == 7
    assert device.flash_writes == [(0, 7)]


def test_reused_baseline_from_the_other_profile_restores_both() -> None:
    # A worker killed on the Mac profile, then the user switched to Win.
    device = FakeKeyboard(effect=CUSTOM_EFFECT)
    device.profile = 1
    device.states[1][0] = 7

    driver = make_driver(device)
    driver.play(Flash(color=RGB(0, 0, 255), duration=1))
    driver.restore({"profile": 0, "effect": STOCK})

    assert device.states[0][0] == STOCK
    assert device.states[1][0] == 7
    assert device.flash_writes == [(0, CUSTOM_EFFECT), (0, 7), (0, STOCK)]


def test_restore_from_a_new_process_skips_unneeded_writes() -> None:
    device = FakeKeyboard()
    make_driver(device).restore({"profile": 0, "effect": STOCK})
    assert device.flash_writes == []


def test_restore_a_state_persisted_by_an_interrupted_effect() -> None:
    # A new process restores the engine's saved baseline while the custom effect is still on.
    device = FakeKeyboard(effect=CUSTOM_EFFECT)
    marker_path().parent.mkdir(parents=True)
    marker_path().write_text(json.dumps({"profile": 0, "effect": STOCK}))

    make_driver(device).restore({"profile": 0, "effect": STOCK})

    assert device.effect == STOCK
    assert not marker_path().exists()


def test_snapshot_undoes_the_custom_effect_left_by_an_interrupted_effect() -> None:
    # The engine's baseline expired, so the keyboard is found as the killed worker left it.
    device = FakeKeyboard()
    driver = make_driver(device)
    driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    assert device.effect == CUSTOM_EFFECT
    assert json.loads(marker_path().read_text()) == {"profile": 0, "effect": STOCK}

    fresh = make_driver(device)
    state = fresh.snapshot()

    assert state == {"profile": 0, "effect": STOCK}
    assert device.effect == STOCK
    assert not marker_path().exists()


def test_marker_for_the_other_profile_is_kept() -> None:
    device = FakeKeyboard(effect=CUSTOM_EFFECT)
    marker_path().parent.mkdir(parents=True)
    marker_path().write_text(json.dumps({"profile": 1, "effect": STOCK}))

    state = make_driver(device).snapshot()

    assert state["effect"] == CUSTOM_EFFECT
    assert marker_path().exists()


def test_unreadable_marker_is_removed() -> None:
    marker_path().parent.mkdir(parents=True)
    marker_path().write_text("{")
    state = make_driver(FakeKeyboard()).snapshot()
    assert state == {"profile": 0, "effect": STOCK}
    assert not marker_path().exists()


def test_firmware_without_custom_effect() -> None:
    device = FakeKeyboard(custom=False)
    driver = make_driver(device)
    driver.snapshot()
    with pytest.raises(DriverError, match="update its firmware"):
        driver.play(Flash(color=RGB(255, 0, 0), duration=1))
    assert not marker_path().exists()


@pytest.mark.parametrize(
    "state",
    [
        {"effect": STOCK},
        {"profile": 0, "effect": "x"},
        {"profile": 0, "effect": CUSTOM_EFFECT, "colors": "zz"},
        {"profile": 0, "effect": CUSTOM_EFFECT, "colors": "00"},
    ],
)
def test_invalid_state(state: dict[str, Any]) -> None:
    device = FakeKeyboard()
    with pytest.raises(DriverError, match="invalid saved state"):
        make_driver(device).restore(state)
    assert device.flash_writes == []


def test_close_closes_transport() -> None:
    device = FakeKeyboard()
    make_driver(device).close()
    assert device.closed


def test_invalid_options() -> None:
    with pytest.raises(DriverError, match="invalid nuphy_air75_v3 options"):
        NuphyAir75V3Driver("keyboard", DeviceConfig(driver="nuphy_air75_v3", host="x"))


def test_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"product_id": 0x1028, "product_string": "NuPhy Air75 V3"}
    monkeypatch.setattr(nuphy_air75_v3, "_enumerate", lambda product_id: [entry])
    [found] = NuphyAir75V3Driver.discover(timeout=0)
    assert (found.name, found.host, found.port) == ("NuPhy Air75 V3", "usb", 0)
    assert found.details == {"product_id": "0x1028"}


def test_connect_without_a_keyboard_on_usb() -> None:
    driver = NuphyAir75V3Driver("keyboard", DeviceConfig(driver="nuphy_air75_v3"))
    with pytest.raises(DriverError, match="over the cable"):
        driver.connect()


# --- S4 transport ---


@pytest.mark.parametrize("key", [0, KEY])
@pytest.mark.parametrize("plain_route", [False, True])
def test_s4_session_key_and_route_variants(key: int, plain_route: bool) -> None:
    device = FakeKeyboard(key=key)
    device.plain_route = plain_route
    s4 = S4(device, timeout=1.0, clock=FakeClock())
    assert s4.identify()[:6] == bytes([0x10, 0, 1, 0xAA, 6, 0])
    assert s4.key == key
    assert s4.request(0xD5, 17) == bytes(STATE)


def test_s4_skips_unrelated_reports() -> None:
    device = FakeKeyboard()
    device.noise = [b"\x01\x02", bytes([0xAA, 0x77]) + bytes(62)]
    s4 = S4(device, timeout=1.0, clock=FakeClock())
    s4.identify()
    assert s4.request(0xA0, 8)[0] == 0


def test_s4_rejects_bad_checksum() -> None:
    device = FakeKeyboard()
    s4 = S4(device, timeout=1.0, clock=FakeClock())
    s4.identify()

    original = device.write

    def corrupt(data: bytes) -> None:
        original(data)
        reply = bytearray(device.replies.pop())
        reply[3] ^= 1
        device.replies.append(bytes(reply))

    device.write = corrupt  # type: ignore[method-assign]
    with pytest.raises(DriverError, match="bad checksum"):
        s4.request(0xD5, 17)


def test_s4_rejects_another_route() -> None:
    device = FakeKeyboard()
    s4 = S4(device, timeout=1.0, clock=FakeClock())
    s4.identify()
    device.foreign_route = True
    with pytest.raises(DriverError, match="another app"):
        s4.request(0xD5, 17)


def test_s4_timeout() -> None:
    clock = FakeClock()

    class Silent(FakeKeyboard):
        def read(self, timeout: float) -> bytes:
            clock.now += timeout
            return b""

    device = Silent()
    device.silent = True
    with pytest.raises(DriverError, match="no reply to 0xa1"):
        S4(device, timeout=0.5, clock=clock).identify()
