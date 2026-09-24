import json
from typing import Any

import httpx
import pytest

from ambient.config import DeviceConfig
from ambient.drivers import get_driver
from ambient.drivers.base import DriverError
from ambient.drivers.nanoleaf import NanoleafDriver, rgb_to_hsb
from ambient.effects import RGB, Flash, Solid
from tests.test_drivers import FakeClock

TOKEN = "tok123"


class FakeNanoleaf:
    """Just enough of the Nanoleaf OpenAPI to exercise the driver."""

    def __init__(self, *, on: bool = True, color_mode: str = "effect", effect: str = "Nemo"):
        self.state: dict[str, Any] = {
            "on": on,
            "brightness": 40,
            "hue": 10,
            "sat": 20,
            "ct": 3000,
            "colorMode": color_mode,
        }
        self.effect = effect
        self.requests: list[tuple[str, str, Any]] = []
        self.pair_attempts_before_ready = 0
        self.status_override: int | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        if self.status_override:
            return httpx.Response(self.status_override)
        if path == "/api/v1/new" and request.method == "POST":
            if self.pair_attempts_before_ready > 0:
                self.pair_attempts_before_ready -= 1
                return httpx.Response(403)
            return httpx.Response(200, json={"auth_token": TOKEN})
        prefix = f"/api/v1/{TOKEN}"
        if not path.startswith(prefix):
            return httpx.Response(401)
        path = path.removeprefix(prefix)
        if request.method == "GET" and path == "/":
            return httpx.Response(200, json=self.info())
        if request.method == "PUT" and path == "/state":
            for key, value in body.items():
                self.state[key] = value["value"]
                if key in ("hue", "sat"):
                    self.state["colorMode"] = "hs"
                    self.effect = "*Solid*"
                if key == "ct":
                    self.state["colorMode"] = "ct"
            return httpx.Response(204)
        if request.method == "PUT" and path == "/effects":
            self.effect = body["select"]
            self.state["colorMode"] = "effect"
            return httpx.Response(204)
        return httpx.Response(404)

    def info(self) -> dict[str, Any]:
        s = self.state
        return {
            "name": "Lines",
            "state": {
                "on": {"value": s["on"]},
                "brightness": {"value": s["brightness"], "max": 100, "min": 0},
                "hue": {"value": s["hue"], "max": 360, "min": 0},
                "sat": {"value": s["sat"], "max": 100, "min": 0},
                "ct": {"value": s["ct"], "max": 6500, "min": 1200},
                "colorMode": s["colorMode"],
            },
            "effects": {"select": self.effect, "effectsList": ["Nemo"]},
        }

    def visible(self) -> tuple[Any, ...]:
        return (self.state["on"], self.state["brightness"], self.state["colorMode"], self.effect)


def make_driver(
    device: FakeNanoleaf, clock: FakeClock | None = None, **options: Any
) -> NanoleafDriver:
    clock = clock or FakeClock()
    config = DeviceConfig.model_validate(
        {"driver": "nanoleaf", "host": "10.0.0.2", "token": TOKEN, **options}
    )
    return NanoleafDriver(
        "lines", config, transport=device.transport(), sleep=clock.sleep, clock=clock
    )


def test_registered() -> None:
    assert get_driver("nanoleaf") is NanoleafDriver


@pytest.mark.parametrize(
    ("color", "brightness", "expected"),
    [
        (RGB(255, 0, 0), 100, (0, 100, 100)),
        (RGB(0, 255, 96), 80, (143, 100, 80)),
        (RGB(128, 0, 0), 100, (0, 100, 50)),
        (RGB(255, 255, 255), 0, (0, 0, 0)),
    ],
)
def test_rgb_to_hsb(color: RGB, brightness: int, expected: tuple[int, int, int]) -> None:
    assert rgb_to_hsb(color, brightness) == expected


@pytest.mark.parametrize(
    ("on", "color_mode", "effect"),
    [
        (True, "effect", "Nemo"),
        (False, "effect", "Nemo"),
        (True, "hs", "*Solid*"),
        (False, "hs", "*Solid*"),
        (True, "ct", "*Solid*"),
        (True, "effect", "*Dynamic*"),
    ],
)
def test_flash_then_restore_returns_previous_state(on: bool, color_mode: str, effect: str) -> None:
    device = FakeNanoleaf(on=on, color_mode=color_mode, effect=effect)
    before = device.visible()
    driver = make_driver(device)

    state = driver.snapshot()
    driver.play(Flash(color=RGB(255, 0, 0), duration=2, times=2))
    assert device.state["on"] is True
    assert device.state["colorMode"] == "hs"
    driver.restore(state)

    after = device.visible()
    if effect == "*Dynamic*":
        # Can't re-select a pseudo-effect; hue/sat is the closest we get.
        assert after == (True, 40, "hs", "*Solid*")
    else:
        assert after == before
    if device.state["colorMode"] == "hs":
        assert (device.state["hue"], device.state["sat"]) == (10, 20)


def test_restore_turns_off_last() -> None:
    device = FakeNanoleaf(on=False)
    driver = make_driver(device)
    state = driver.snapshot()
    driver.play(Solid(color=RGB(0, 0, 255), duration=0.5))
    device.requests.clear()
    driver.restore(state)

    assert device.requests[0][1:] == (f"/api/v1/{TOKEN}/effects", {"select": "Nemo"})
    assert device.requests[-1][2] == {
        "brightness": {"value": 40, "duration": 0},
        "on": {"value": False},
    }


def test_frames_send_color_once_then_brightness() -> None:
    device = FakeNanoleaf()
    driver = make_driver(device)
    driver.play(Flash(color=RGB(255, 0, 0), brightness=70, duration=1, times=2))

    bodies = [body for method, _, body in device.requests if method == "PUT"]
    assert bodies == [
        {
            "brightness": {"value": 70, "duration": 0},
            "on": {"value": True},
            "hue": {"value": 0},
            "sat": {"value": 100},
        },
        {"brightness": {"value": 0, "duration": 0}},
        {"brightness": {"value": 70, "duration": 0}},
        {"brightness": {"value": 0, "duration": 0}},
    ]


def test_missing_token_asks_to_pair() -> None:
    device = FakeNanoleaf()
    config = DeviceConfig.model_validate({"driver": "nanoleaf", "host": "10.0.0.2"})
    driver = NanoleafDriver("lines", config, transport=device.transport())
    with pytest.raises(DriverError, match="ambient pair lines"):
        driver.snapshot()
    assert device.requests == []


def test_missing_host_is_a_driver_error() -> None:
    with pytest.raises(DriverError, match="host"):
        NanoleafDriver("lines", DeviceConfig(driver="nanoleaf"))


@pytest.mark.parametrize(("status", "message"), [(401, "token rejected"), (500, "HTTP 500")])
def test_http_errors(status: int, message: str) -> None:
    device = FakeNanoleaf()
    device.status_override = status
    with pytest.raises(DriverError, match=message):
        make_driver(device).snapshot()


def test_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    config = DeviceConfig.model_validate({"driver": "nanoleaf", "host": "10.0.0.2", "token": "t"})
    driver = NanoleafDriver("lines", config, transport=httpx.MockTransport(refuse))
    with pytest.raises(DriverError, match=r"can't reach 10\.0\.0\.2:16021"):
        driver.snapshot()


def test_pair_polls_until_pairing_mode() -> None:
    device = FakeNanoleaf()
    device.pair_attempts_before_ready = 3
    clock = FakeClock()
    driver = make_driver(device, clock, token=None)

    assert driver.pair(timeout=30) == {"host": "10.0.0.2", "port": 16021, "token": TOKEN}
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_pair_times_out() -> None:
    device = FakeNanoleaf()
    device.pair_attempts_before_ready = 100
    clock = FakeClock()
    with pytest.raises(DriverError, match="timed out"):
        make_driver(device, clock, token=None).pair(timeout=5)
    assert clock.now == 5.0
