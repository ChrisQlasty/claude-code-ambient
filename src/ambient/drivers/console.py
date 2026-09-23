"""A fake device that prints frames to stdout, for trying things out without hardware."""

import sys
from typing import Any, ClassVar, TextIO

from ambient.config import DeviceConfig
from ambient.drivers.base import Capability, DeviceState, Driver
from ambient.effects import RGB, Frame


class ConsoleDriver(Driver):
    name: ClassVar[str] = "console"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.COLOR, Capability.BRIGHTNESS, Capability.READ_STATE}
    )

    def __init__(
        self, device_id: str, config: DeviceConfig, *, out: TextIO | None = None, **kwargs: Any
    ) -> None:
        super().__init__(device_id, config, **kwargs)
        self._out = out or sys.stdout
        baseline = config.baseline
        self._state: DeviceState = {
            "color": baseline.color.to_hex() if baseline else "#000000",
            "brightness": baseline.brightness if baseline else 0,
        }

    def snapshot(self) -> DeviceState:
        self._print(f"snapshot  {self._describe(self._state)}")
        return dict(self._state)

    def apply_frame(self, frame: Frame) -> None:
        self._state = {"color": frame.color.to_hex(), "brightness": frame.brightness}
        self._print(f"+{frame.t:6.3f}s  {self._describe(self._state)}")

    def restore(self, state: DeviceState) -> None:
        self._state = dict(state)
        self._print(f"restore   {self._describe(self._state)}")

    def _print(self, message: str) -> None:
        print(f"[{self.device_id}] {message}", file=self._out, flush=True)

    def _describe(self, state: DeviceState) -> str:
        color, brightness = state["color"], state["brightness"]
        return f"{self._swatch(RGB.from_hex(color), brightness)}{color} {brightness:3d}%"

    def _swatch(self, color: RGB, brightness: int) -> str:
        if not self._out.isatty():
            return ""
        r, g, b = (round(c * brightness / 100) for c in color)
        return f"\x1b[48;2;{r};{g};{b}m    \x1b[0m "
