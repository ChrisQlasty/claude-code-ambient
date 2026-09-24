"""Nanoleaf (Lines and other panels) over the local REST "OpenAPI" on port 16021.

Effects are played as a series of state PUTs (hue/sat once, then brightness per frame), and the
previous state is restored from a snapshot: the named scene, or the solid hue/sat or color
temperature, plus brightness and on/off.
"""

import colorsys
import time
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ambient.config import DeviceConfig
from ambient.drivers.base import Capability, DeviceState, DiscoveredDevice, Driver, DriverError
from ambient.effects import RGB, Frame

DEFAULT_PORT = 16021
SERVICE_TYPE = "_nanoleafapi._tcp.local."
# Effect names wrapped in '*' are pseudo-effects (a solid color, external control, ...), not
# scenes that can be re-selected by name.
_PSEUDO_EFFECT_PREFIX = "*"


class NanoleafOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = DEFAULT_PORT
    token: str | None = None
    timeout: float = 2.0


def rgb_to_hsb(color: RGB, brightness: int) -> tuple[int, int, int]:
    """Nanoleaf hue (0-360), sat (0-100) and brightness. The color's value scales brightness."""
    h, s, v = colorsys.rgb_to_hsv(color.r / 255, color.g / 255, color.b / 255)
    return round(h * 360) % 360, round(s * 100), round(brightness * v)


class NanoleafDriver(Driver):
    name: ClassVar[str] = "nanoleaf"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.COLOR, Capability.BRIGHTNESS, Capability.READ_STATE}
    )
    # Each frame is an HTTP request (typically 20-60 ms over Wi-Fi), so keep the rate modest.
    fps: ClassVar[int] = 10
    pairing_hint: ClassVar[str] = "hold the power button for 5-7 s until the lights flash"

    def __init__(
        self,
        device_id: str,
        config: DeviceConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(device_id, config, **kwargs)
        try:
            self.options = NanoleafOptions.model_validate(config.options)
        except ValidationError as exc:
            raise DriverError(
                f"{device_id}: invalid nanoleaf options ({_brief(exc)}); "
                f"set 'host' or run `ambient pair {device_id}`"
            ) from exc
        self._client = httpx.Client(
            base_url=f"http://{self.options.host}:{self.options.port}/api/v1",
            timeout=self.options.timeout,
            transport=transport,
        )
        self._color: RGB | None = None

    def close(self) -> None:
        self._client.close()

    # --- state ---------------------------------------------------------------------------------

    def snapshot(self) -> DeviceState:
        info = self._request("GET", "/")
        try:
            state = info["state"]
            snapshot: DeviceState = {
                "on": bool(state["on"]["value"]),
                "brightness": int(state["brightness"]["value"]),
                "color_mode": str(state.get("colorMode", "effect")),
                "hue": int(state["hue"]["value"]),
                "sat": int(state["sat"]["value"]),
                "ct": int(state["ct"]["value"]),
                "effect": str(info.get("effects", {}).get("select", "")),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError(f"{self.device_id}: unexpected state response: {exc!r}") from exc
        return snapshot

    def restore(self, state: DeviceState) -> None:
        effect = str(state.get("effect", ""))
        if (
            state["color_mode"] == "effect"
            and effect
            and not effect.startswith(_PSEUDO_EFFECT_PREFIX)
        ):
            self._request("PUT", "/effects", {"select": effect})
            body: dict[str, Any] = {}
        elif state["color_mode"] == "ct":
            body = {"ct": {"value": state["ct"]}}
        else:
            # A solid color, or a pseudo-effect we can't re-select: hue/sat is the closest match.
            body = {"hue": {"value": state["hue"]}, "sat": {"value": state["sat"]}}
        # Brightness and on/off go last, so lights that were off are switched off again after the
        # scene or color is back in place.
        body["brightness"] = {"value": state["brightness"], "duration": 0}
        body["on"] = {"value": state["on"]}
        self._request("PUT", "/state", body)
        self._color = None

    def apply_frame(self, frame: Frame) -> None:
        hue, sat, brightness = rgb_to_hsb(frame.color, frame.brightness)
        body: dict[str, Any] = {"brightness": {"value": brightness, "duration": 0}}
        if frame.color != self._color:
            # Setting hue/sat is what switches the panels from a scene to a solid color, so it's
            # only sent when the color changes. Turning on is included for lights that were off.
            body |= {"on": {"value": True}, "hue": {"value": hue}, "sat": {"value": sat}}
            self._color = frame.color
        self._request("PUT", "/state", body)

    # --- discovery and pairing -----------------------------------------------------------------

    @classmethod
    def discover(cls, timeout: float) -> list[DiscoveredDevice]:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        names: list[str] = []

        class Listener(ServiceListener):
            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                names.append(name)

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

        try:
            zc = Zeroconf()
        except OSError as exc:
            raise DriverError(f"mDNS discovery unavailable: {exc}") from exc
        try:
            ServiceBrowser(zc, SERVICE_TYPE, Listener())
            time.sleep(timeout)
            found = []
            for name in dict.fromkeys(names):
                info = zc.get_service_info(SERVICE_TYPE, name, timeout=int(timeout * 1000))
                if info is None or not info.parsed_addresses():
                    continue
                details = {
                    k.decode(): v.decode()
                    for k, v in info.properties.items()
                    if isinstance(k, bytes) and isinstance(v, bytes)
                }
                found.append(
                    DiscoveredDevice(
                        driver=cls.name,
                        name=name.removesuffix("." + SERVICE_TYPE),
                        host=_prefer_ipv4(info.parsed_addresses()),
                        port=info.port or DEFAULT_PORT,
                        details=details,
                    )
                )
            return found
        finally:
            zc.close()

    def pair(self, timeout: float) -> dict[str, Any]:
        """Poll for a token until the power button is held (the device answers 403 until then)."""
        deadline = self._clock() + timeout
        while True:
            try:
                response = self._client.post("/new")
            except httpx.HTTPError as exc:
                raise DriverError(f"{self.device_id}: {self._describe_error(exc)}") from exc
            if response.status_code == 200:
                token = response.json().get("auth_token")
                if not token:
                    raise DriverError(f"{self.device_id}: pairing response has no auth_token")
                return {"host": self.options.host, "port": self.options.port, "token": token}
            if response.status_code != 403:
                raise DriverError(f"{self.device_id}: pairing failed: HTTP {response.status_code}")
            if self._clock() >= deadline:
                raise DriverError(
                    f"{self.device_id}: timed out waiting for pairing mode "
                    "(hold the power button until the lights flash)"
                )
            self._sleep(1.0)

    # --- HTTP ----------------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.options.token:
            raise DriverError(f"{self.device_id}: no token; run `ambient pair {self.device_id}`")
        try:
            response = self._client.request(method, f"/{self.options.token}{path}", json=body)
        except httpx.HTTPError as exc:
            raise DriverError(f"{self.device_id}: {self._describe_error(exc)}") from exc
        if response.status_code in (401, 403):
            raise DriverError(
                f"{self.device_id}: token rejected; run `ambient pair {self.device_id}`"
            )
        if response.is_error:
            raise DriverError(
                f"{self.device_id}: {method} {path} failed: HTTP {response.status_code}"
            )
        return response.json() if response.content else None

    def _describe_error(self, exc: httpx.HTTPError) -> str:
        return f"can't reach {self.options.host}:{self.options.port} ({type(exc).__name__})"


def _prefer_ipv4(addresses: list[str]) -> str:
    return next((a for a in addresses if ":" not in a), addresses[0])


def _brief(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
