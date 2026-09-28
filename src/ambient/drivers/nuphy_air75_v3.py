"""NuPhy Air75 V3 over its USB configuration interface (NuPhy's "S4" protocol, used by NuPhyIO).

Only the USB cable exposes that interface; over Bluetooth or the 2.4 GHz dongle the keyboard
can't be found. Needs firmware 1.0.16.6 or later: older firmware has no per-key custom mode.

Per-key colors (``D8``) live in RAM, but they only show in the custom effect, and switching
effects (``D6``) is saved to the keyboard's flash. So an effect costs exactly two small flash
writes, one to enter the custom effect and one to switch back, and only the effect field is
ever written: whole-state writes rotate the stored color's hue on some firmware, and brightness
writes rescale it. If the keyboard was already in the custom effect, nothing is written to flash
and its key colors are repainted afterwards. The side lights are never touched.

A marker file records each profile's original effect while it's in the custom effect, so an
effect cut short (the worker was killed) is undone by the next snapshot on that profile even
after the engine's baseline has expired, instead of the keyboard staying dark after its next
power cycle.
"""

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ambient import paths
from ambient.config import DeviceConfig
from ambient.drivers.base import Capability, DeviceState, DiscoveredDevice, Driver, DriverError
from ambient.drivers.hid import HidapiTransport, HidTransport, import_hid
from ambient.effects import RGB, AnyEffect, Frame, Timeline, render, smooth

log = logging.getLogger(__name__)

VENDOR_ID = 0x19F5
PRODUCT_IDS = {0x1028: "Air75 V3"}
# The configuration interface is the one with a generic-desktop usage of 0 (undefined).
CONFIG_USAGE = (0x01, 0x00)

REPORT_SIZE = 64
REQUEST = 0x55
REPLY = 0xAA
MAX_PAYLOAD = REPORT_SIZE - 8
FIRMWARE_MARKER = 0xAA  # constant byte 3 of the A1 payload, from which the session key follows

CMD_GEOMETRY = 0xA0  # byte 0 is the active Mac (0) / Win (1) lighting profile
CMD_FIRMWARE = 0xA1
CMD_RENDERED = 0xD2  # the RGB currently shown, 3 bytes per LED
CMD_GET_STATE = 0xD5
CMD_SET_STATE = 0xD6  # saved to flash
CMD_SET_LEDS = 0xD8  # RAM only

STATE_SIZE = 17
EFFECT = 0  # offset of the main backlight's effect in the state
CUSTOM_EFFECT = 21
KEY_LEDS = 84
LEDS_PER_WRITE = 13  # [index, r, g, b] records per report
RENDERED_PER_READ = 54
# The firmware applies a state write after a moment; reading back earlier returns the old one.
SETTLE = 0.12
SETTLE_READS = 5


class S4:
    """Request/response over the S4 protocol.

    Report layout: ``[0x55, command, 0, checksum, length, address lo, address hi, handle,
    payload...]``, 64 bytes. Bytes from 4 on are XORed with a session key, and the checksum is
    the byte sum of bytes 4-63 as sent. Replies start with ``0xAA`` and echo the command.
    """

    def __init__(
        self, transport: HidTransport, *, timeout: float, clock: Callable[[], float]
    ) -> None:
        self._transport = transport
        self._timeout = timeout
        self._clock = clock
        self.key = 0

    def identify(self) -> bytes:
        """Read the firmware info and derive the session key from it."""
        self.key = 0
        raw = self._exchange(self._frame(CMD_FIRMWARE, 8, 0, b"", 0))
        # Payload byte 3 is a constant, so XORing it back gives the key.
        self.key = raw[11] ^ FIRMWARE_MARKER
        info = self._decode(raw, 8, 0, 0)
        if info[3] != FIRMWARE_MARKER:
            raise DriverError(f"unrecognized firmware info {info.hex()}")
        return info

    def request(
        self, command: int, length: int, *, address: int = 0, payload: bytes = b"", handle: int = 0
    ) -> bytes:
        """Send a request and return the reply's ``length`` payload bytes."""
        raw = self._exchange(self._frame(command, length, address, payload, handle))
        return self._decode(raw, length, address, handle)

    def _frame(self, command: int, length: int, address: int, payload: bytes, handle: int) -> bytes:
        if not 0 <= length <= MAX_PAYLOAD or len(payload) > length:
            raise ValueError(f"invalid S4 request length {length}")
        body = bytes([length, address & 0xFF, address >> 8, handle]) + payload
        body = bytes(b ^ self.key for b in body.ljust(REPORT_SIZE - 4, b"\0"))
        return bytes([REQUEST, command, 0, sum(body) & 0xFF]) + body

    def _exchange(self, frame: bytes) -> bytes:
        # Drop replies left over from an earlier, timed-out request.
        while self._transport.read(0.001):
            pass
        self._transport.write(b"\0" + frame)  # report ID 0
        deadline = self._clock() + self._timeout
        while (remaining := deadline - self._clock()) > 0:
            reply = self._transport.read(remaining)
            if len(reply) == REPORT_SIZE and reply[0] == REPLY and reply[1] == frame[1]:
                if reply[3] != sum(reply[4:]) & 0xFF:
                    raise DriverError(f"bad checksum in reply to 0x{frame[1]:02x}")
                return reply
        raise DriverError(f"no reply to 0x{frame[1]:02x} within {self._timeout:.1f}s")

    def _decode(self, raw: bytes, length: int, address: int, handle: int) -> bytes:
        route = bytes([length, address & 0xFF, address >> 8, handle])
        # Firmware echoes the route either as sent or in plain text.
        if raw[4:8] != route and bytes(b ^ self.key for b in raw[4:8]) != route:
            raise DriverError(
                f"unexpected reply to 0x{raw[1]:02x}; is another app (NuPhyIO) using the keyboard?"
            )
        return bytes(b ^ self.key for b in raw[8 : 8 + length])


def _enumerate(product_id: int | None) -> list[dict[str, Any]]:
    hid = import_hid()
    return [
        d
        for d in hid.enumerate(VENDOR_ID, product_id or 0)
        if (d["usage_page"], d["usage"]) == CONFIG_USAGE
        and (d["product_id"] == product_id if product_id else d["product_id"] in PRODUCT_IDS)
    ]


def open_hidapi(options: "NuphyOptions") -> HidTransport:
    found = _enumerate(options.product_id)
    if not found:
        raise DriverError(
            "no NuPhy Air75 V3 found on USB (lighting can only be controlled over the cable)"
        )
    return HidapiTransport(found[0]["path"], report_size=REPORT_SIZE, device="keyboard")


class NuphyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Another USB product ID that speaks the same protocol; by default any known model is used.
    product_id: int | None = None
    timeout: float = 1.0
    # Seconds to fade into each frame; the keys themselves switch colors instantly. 0 disables.
    transition: float = Field(default=0.25, ge=0, le=5)


def scale(color: RGB, brightness: int) -> RGB:
    return RGB(*(round(c * brightness / 100) for c in color))


class NuphyAir75V3Driver(Driver):
    name: ClassVar[str] = "nuphy_air75_v3"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.COLOR, Capability.BRIGHTNESS, Capability.READ_STATE}
    )
    # A frame is seven ~3 ms USB requests.
    fps: ClassVar[int] = 30

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
            self.options = NuphyOptions.model_validate(config.options)
        except ValidationError as exc:
            raise DriverError(f"{device_id}: invalid nuphy_air75_v3 options: {exc}") from exc
        self._transport = transport
        self._s4: S4 | None = None
        self._profile = 0
        self._original_effect: int | None = None
        self._last: RGB | None = None

    def connect(self) -> None:
        if self._transport is None:
            self._transport = open_hidapi(self.options)
        self._s4 = S4(self._transport, timeout=self.options.timeout, clock=self._clock)
        self._s4.identify()
        # Read here rather than in snapshot(): a reused baseline skips it, and play() must still
        # switch the profile that's active now.
        self._profile = self._s4.request(CMD_GEOMETRY, 8)[0]

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._s4 = None

    def snapshot(self) -> DeviceState:
        effect = self._read_effect(self._profile)
        # A marker for the other Mac/Win profile waits until that profile is active again.
        original = self._read_markers().get(self._profile)
        if original is not None:
            if effect == CUSTOM_EFFECT:
                log.warning(
                    "%s: undoing the custom effect left by an interrupted effect", self.device_id
                )
                effect = self._write_effect(self._profile, original)
            self._clear_marker(self._profile)
        state: DeviceState = {"profile": self._profile, "effect": effect}
        if effect == CUSTOM_EFFECT:
            # Already showing the user's own per-key colors: keep the effect, repaint them after.
            state["colors"] = self._read_colors().hex()
        return state

    def timeline(self, effect: AnyEffect) -> Timeline:
        return smooth(render(effect, self.fps), self.options.transition, self.fps)

    def play(self, effect: AnyEffect) -> None:
        if self._original_effect is None:
            # Switching effects takes a moment to verify, so it's done before the effect's clock
            # starts. The RAM table is painted first, so a stale one never shows.
            first = self.timeline(effect).frames[0]
            self._show(scale(first.color, first.brightness))
            self._enter_custom_effect()
        super().play(effect)

    def apply_frame(self, frame: Frame) -> None:
        self._show(scale(frame.color, frame.brightness))

    def restore(self, state: DeviceState) -> None:
        try:
            profile, effect = int(state["profile"]), int(state["effect"])
            colors = bytes.fromhex(state["colors"]) if "colors" in state else None
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError(f"{self.device_id}: invalid saved state: {exc!r}") from exc
        if colors is not None and len(colors) != KEY_LEDS * 3:
            raise DriverError(f"{self.device_id}: invalid saved state: {len(colors)} color bytes")
        self._connected()
        if self._original_effect is not None and profile != self._profile:
            # A baseline reused from another profile doesn't cover the one play() switched.
            self._set_effect(self._profile, self._original_effect)
            self._clear_marker(self._profile)
        if colors is not None:
            self._paint(colors)
        self._set_effect(profile, effect)
        self._clear_marker(profile)
        self._original_effect = None
        self._last = None

    @classmethod
    def discover(cls, timeout: float) -> list[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                driver=cls.name,
                name=d.get("product_string") or PRODUCT_IDS.get(d["product_id"], "NuPhy"),
                host="usb",
                port=0,
                details={"product_id": f"0x{d['product_id']:04x}"},
            )
            for d in _enumerate(None)
        ]

    def _show(self, color: RGB) -> None:
        if color != self._last:
            self._paint(bytes(color) * KEY_LEDS)
            self._last = color

    def _enter_custom_effect(self) -> None:
        effect = self._read_effect(self._profile)
        if effect != CUSTOM_EFFECT:
            # Written before the effect changes, so a killed worker can always be undone.
            self._write_marker(self._profile, effect)
            if self._write_effect(self._profile, CUSTOM_EFFECT) != CUSTOM_EFFECT:
                self._clear_marker(self._profile)
                raise DriverError(
                    f"{self.device_id}: the keyboard didn't switch to its custom effect; update "
                    "its firmware in NuPhyIO (1.0.16.6 or later) and check the lighting is on"
                )
        self._original_effect = effect

    def _read_effect(self, profile: int) -> int:
        return self._connected().request(CMD_GET_STATE, STATE_SIZE, handle=profile)[EFFECT]

    def _set_effect(self, profile: int, effect: int) -> None:
        # Writing the effect costs a flash write, so skip it when nothing changed.
        if self._read_effect(profile) != effect:
            self._write_effect(profile, effect)

    def _write_effect(self, profile: int, effect: int) -> int:
        """Set the main backlight's effect (a flash write) and return what the keyboard shows."""
        s4 = self._connected()
        s4.request(CMD_SET_STATE, 1, address=EFFECT, payload=bytes([effect]), handle=profile)
        current = effect
        for _ in range(SETTLE_READS):
            self._sleep(SETTLE)
            current = self._read_effect(profile)
            if current == effect:
                break
        return current

    def _paint(self, colors: bytes) -> None:
        s4 = self._connected()
        for start in range(0, KEY_LEDS, LEDS_PER_WRITE):
            records = b"".join(
                bytes([i]) + colors[i * 3 : i * 3 + 3]
                for i in range(start, min(start + LEDS_PER_WRITE, KEY_LEDS))
            )
            s4.request(CMD_SET_LEDS, len(records), payload=records)

    def _read_colors(self) -> bytes:
        s4 = self._connected()
        size = KEY_LEDS * 3
        colors = b""
        while len(colors) < size:
            length = min(RENDERED_PER_READ, size - len(colors))
            colors += s4.request(CMD_RENDERED, length, address=len(colors))
        return colors

    def _marker_path(self) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_id)
        return paths.cache_dir() / "nuphy" / f"{safe}.json"

    def _write_marker(self, profile: int, effect: int) -> None:
        # One entry per profile: each can be left in the custom effect by a different worker.
        self._save_markers({**self._read_markers(), profile: effect})

    def _clear_marker(self, profile: int) -> None:
        markers = self._read_markers()
        if markers.pop(profile, None) is not None:
            self._save_markers(markers)

    def _save_markers(self, markers: dict[int, int]) -> None:
        path = self._marker_path()
        if not markers:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({str(p): e for p, e in markers.items()}))

    def _read_markers(self) -> dict[int, int]:
        """The original effect of each profile left in the custom effect, by profile."""
        try:
            data = json.loads(self._marker_path().read_text())
            return {int(p): int(e) for p, e in data.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError, TypeError):
            log.warning("%s: removing unreadable marker %s", self.device_id, self._marker_path())
            self._marker_path().unlink(missing_ok=True)
            return {}

    def _connected(self) -> S4:
        if self._s4 is None:
            raise DriverError(f"{self.device_id}: not connected")
        return self._s4
