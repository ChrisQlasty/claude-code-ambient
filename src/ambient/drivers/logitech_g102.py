"""Logitech G102/G203 LIGHTSYNC over HID++ 2.0 (USB).

Frames are sent as each LED cluster's "fixed" effect (feature ``0x8071`` RGB Effects) with the
color scaled by brightness. Every write uses the RAM-only persistence flag, so nothing reaches the
mouse's flash.

The mouse can't report its current lighting, so restoring depends on who drives it:

- Onboard mode (the default without G HUB, or with G HUB's "onboard memory mode"): the lighting
  comes from the active onboard profile (feature ``0x8100``), so re-selecting that profile brings
  it back exactly. Re-selecting also resets the DPI step, so that is saved and restored too.
- Host mode (G HUB in control) or no onboard profiles: the configured ``baseline`` color is set.
"""

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from ambient.config import DeviceConfig
from ambient.drivers.base import Capability, DeviceState, DiscoveredDevice, Driver, DriverError
from ambient.effects import RGB, Frame

log = logging.getLogger(__name__)

VENDOR_ID = 0x046D
PRODUCT_IDS = {0xC092: "G102/G203 LIGHTSYNC"}
# HID++ lives on the vendor-defined collection of the mouse's second interface.
HIDPP_USAGE_PAGE = 0xFF00

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
LONG_SIZE = 20
DEVICE_INDEX = 0xFF  # a device on its own USB cable rather than behind a receiver
SOFTWARE_ID = 0x0A  # tags our requests so replies can be told apart from notifications
HIDPP20_ERROR = 0xFF
HIDPP10_ERROR = 0x8F

FEATURE_ROOT = 0x0000
FEATURE_RGB_EFFECTS = 0x8071
FEATURE_ONBOARD_PROFILES = 0x8100

RGB_GET_INFO = 0
RGB_SET_CLUSTER_EFFECT = 1
INDEX_ALL = 0xFF
EFFECT_FIXED = 0x0001
# 1 would also write the effect to flash, which must never happen (see CLAUDE.md).
PERSIST_RAM_ONLY = 0x00

PROFILES_GET_MODE = 2
PROFILES_SET_CURRENT = 3
PROFILES_GET_CURRENT = 4
PROFILES_GET_DPI_INDEX = 11
PROFILES_SET_DPI_INDEX = 12
MODE_ONBOARD = 1

_ERRORS = {
    1: "unknown",
    2: "invalid argument",
    3: "out of range",
    4: "hardware error",
    5: "logitech internal",
    6: "invalid feature index",
    7: "invalid function",
    8: "busy",
    9: "unsupported",
}


class HidTransport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout: float) -> bytes:
        """The next input report, or ``b""`` if none arrives within ``timeout`` seconds."""
        ...

    def close(self) -> None: ...


class HidppError(DriverError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class Hidpp:
    """Request/response over HID++ 2.0 long reports."""

    def __init__(
        self, transport: HidTransport, *, timeout: float, clock: Callable[[], float]
    ) -> None:
        self._transport = transport
        self._timeout = timeout
        self._clock = clock
        self._features: dict[int, int] = {FEATURE_ROOT: 0}

    def request(self, feature_index: int, function: int, params: bytes = b"") -> bytes:
        """Send a request and return the reply's 16 parameter bytes."""
        header = bytes([REPORT_LONG, DEVICE_INDEX, feature_index, (function << 4) | SOFTWARE_ID])
        self._transport.write((header + params).ljust(LONG_SIZE, b"\0"))
        deadline = self._clock() + self._timeout
        while (remaining := deadline - self._clock()) > 0:
            reply = self._transport.read(remaining)
            if len(reply) < 7 or reply[0] not in (REPORT_SHORT, REPORT_LONG):
                continue
            if reply[2] in (HIDPP20_ERROR, HIDPP10_ERROR) and reply[3:5] == header[2:4]:
                code = reply[5]
                raise HidppError(
                    f"HID++ error {code} ({_ERRORS.get(code, 'unknown')}) "
                    f"for feature index {feature_index} function {function}",
                    code,
                )
            if reply[2:4] == header[2:4]:
                return reply[4:].ljust(LONG_SIZE - 4, b"\0")
        raise DriverError(f"no HID++ reply within {self._timeout:.1f}s")

    def feature_index(self, feature: int) -> int | None:
        if feature not in self._features:
            self._features[feature] = self.request(0, 0, feature.to_bytes(2, "big"))[0]
        index = self._features[feature]
        return index if index or feature == FEATURE_ROOT else None


class HidapiTransport:
    def __init__(self, path: bytes) -> None:
        import hid

        _allow_shared_open(hid.__file__)
        self._device = hid.device()
        try:
            self._device.open_path(path)
        except OSError as exc:
            raise DriverError(f"can't open the mouse ({exc})") from exc

    def write(self, data: bytes) -> None:
        if self._device.write(data) < 0:
            raise DriverError("HID write failed; was the mouse unplugged?")

    def read(self, timeout: float) -> bytes:
        return bytes(self._device.read(LONG_SIZE, max(1, round(timeout * 1000))))

    def close(self) -> None:
        self._device.close()


def _allow_shared_open(library: str) -> None:
    """On macOS, stop hidapi from opening devices exclusively.

    The exclusive default fails whenever G HUB (or macOS itself) already has the mouse open.
    The Python binding doesn't wrap the setting, so it's called through ctypes. ``hid_init``
    resets it, which has already run by the time a device path was enumerated.
    """
    if sys.platform != "darwin":
        return
    try:
        ctypes.CDLL(library).hid_darwin_set_open_exclusive(0)
    except (OSError, AttributeError):
        log.debug("hid_darwin_set_open_exclusive unavailable; opening exclusively")


def _enumerate(product_id: int | None) -> list[dict[str, Any]]:
    try:
        import hid
    except ImportError as exc:
        raise DriverError(f"hidapi isn't available: {exc}") from exc
    return [
        d
        for d in hid.enumerate(VENDOR_ID, product_id or 0)
        if d["usage_page"] == HIDPP_USAGE_PAGE
        and (d["product_id"] == product_id if product_id else d["product_id"] in PRODUCT_IDS)
    ]


def open_hidapi(options: "G102Options") -> HidTransport:
    found = _enumerate(options.product_id)
    if not found:
        raise DriverError("no Logitech G102/G203 LIGHTSYNC found on USB")
    return HidapiTransport(found[0]["path"])


class G102Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Another USB product ID that speaks the same protocol; by default any known model is used.
    product_id: int | None = None
    timeout: float = 1.0


def scale(color: RGB, brightness: int) -> RGB:
    return RGB(*(round(c * brightness / 100) for c in color))


class LogitechG102Driver(Driver):
    name: ClassVar[str] = "logitech_g102"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.COLOR, Capability.BRIGHTNESS}
    )

    def __init__(
        self,
        device_id: str,
        config: DeviceConfig,
        *,
        transport: HidTransport | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(device_id, config, **kwargs)
        try:
            self.options = G102Options.model_validate(config.options)
        except ValidationError as exc:
            raise DriverError(f"{device_id}: invalid logitech_g102 options: {exc}") from exc
        self._transport = transport
        self._hidpp: Hidpp | None = None
        self._rgb = 0
        self._profiles: int | None = None
        # Cluster index → list position of its fixed effect, which is what setClusterEffect takes.
        self._fixed: dict[int, int] = {}
        self._last: RGB | None = None

    def connect(self) -> None:
        if self._transport is None:
            self._transport = open_hidapi(self.options)
        hidpp = self._hidpp = Hidpp(
            self._transport, timeout=self.options.timeout, clock=self._clock
        )
        rgb = hidpp.feature_index(FEATURE_RGB_EFFECTS)
        if rgb is None:
            raise DriverError(f"{self.device_id}: the mouse doesn't support RGB Effects (0x8071)")
        self._rgb = rgb
        self._profiles = hidpp.feature_index(FEATURE_ONBOARD_PROFILES)
        clusters = hidpp.request(rgb, RGB_GET_INFO, bytes([INDEX_ALL, INDEX_ALL, 0]))[2]
        for cluster in range(clusters):
            count = hidpp.request(rgb, RGB_GET_INFO, bytes([cluster, INDEX_ALL, 0]))[4]
            for index in range(count):
                info = hidpp.request(rgb, RGB_GET_INFO, bytes([cluster, index, 0]))
                if int.from_bytes(info[2:4], "big") == EFFECT_FIXED:
                    self._fixed[cluster] = index
                    break
        if not self._fixed:
            raise DriverError(f"{self.device_id}: the mouse has no fixed-color LED effect")

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._hidpp = None

    def snapshot(self) -> DeviceState:
        hidpp = self._connected()
        if self._profiles is not None:
            mode = hidpp.request(self._profiles, PROFILES_GET_MODE)[0]
            if mode == MODE_ONBOARD:
                profile = hidpp.request(self._profiles, PROFILES_GET_CURRENT)[:2]
                dpi = hidpp.request(self._profiles, PROFILES_GET_DPI_INDEX)[0]
                return {"profile": profile.hex(), "dpi_index": dpi}
        baseline = self.config.baseline
        if baseline is None:
            raise DriverError(
                f"{self.device_id}: the mouse isn't in onboard mode (is G HUB controlling it?) "
                "and can't report its color; set a 'baseline' (color, brightness) in the config"
            )
        return {"color": baseline.color.to_hex(), "brightness": baseline.brightness}

    def apply_frame(self, frame: Frame) -> None:
        self._set_color(scale(frame.color, frame.brightness))

    def restore(self, state: DeviceState) -> None:
        try:
            if "profile" in state:
                profile, dpi = bytes.fromhex(state["profile"]), int(state["dpi_index"])
                color = None
            else:
                color = scale(RGB.from_hex(state["color"]), int(state["brightness"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError(f"{self.device_id}: invalid saved state: {exc!r}") from exc
        if color is not None:
            self._set_color(color)
            return
        assert self._profiles is not None, "a profile state implies onboard profiles"
        hidpp = self._connected()
        hidpp.request(self._profiles, PROFILES_SET_CURRENT, profile)
        hidpp.request(self._profiles, PROFILES_SET_DPI_INDEX, bytes([dpi]))
        self._last = None

    @classmethod
    def discover(cls, timeout: float) -> list[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                driver=cls.name,
                name=d.get("product_string") or PRODUCT_IDS.get(d["product_id"], "Logitech"),
                host="usb",
                port=0,
                details={"product_id": f"0x{d['product_id']:04x}"},
            )
            for d in _enumerate(None)
            # Each HID++ collection (short and long reports) is listed; keep one per mouse.
            if d["usage"] == 0x02
        ]

    def _set_color(self, color: RGB) -> None:
        if color == self._last:
            return
        hidpp = self._connected()
        for cluster, index in self._fixed.items():
            params = bytes([cluster, index, *color]).ljust(12, b"\0") + bytes([PERSIST_RAM_ONLY])
            hidpp.request(self._rgb, RGB_SET_CLUSTER_EFFECT, params)
        self._last = color

    def _connected(self) -> Hidpp:
        if self._hidpp is None:
            raise DriverError(f"{self.device_id}: not connected")
        return self._hidpp
