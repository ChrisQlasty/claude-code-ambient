"""Driver interface shared by all devices."""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, ClassVar

from ambient.config import DeviceConfig
from ambient.effects import DEFAULT_FPS, AnyEffect, Frame, Timeline, render

# Opaque, JSON-serializable per-driver state, so it can be persisted as a baseline.
DeviceState = dict[str, Any]


class DriverError(Exception):
    """A device is misconfigured, unreachable, or rejected a request."""


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    driver: str
    name: str
    host: str
    port: int
    details: dict[str, str] = field(default_factory=dict)


class Capability(Enum):
    COLOR = auto()
    BRIGHTNESS = auto()
    READ_STATE = auto()
    PER_ZONE = auto()
    NATIVE_TEMP_EFFECT = auto()


class Driver(ABC):
    """Base class for device drivers, registered under the ``ambient.drivers`` entry point group.

    The default :meth:`play` renders the effect to frames and calls :meth:`apply_frame`
    in real time. Drivers with a native temporary-effect feature can override it.
    """

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    # Frame rate for rendered effects. Networked devices use less to stay within request latency.
    fps: ClassVar[int] = DEFAULT_FPS
    # What the user must do on the device while :meth:`pair` waits.
    pairing_hint: ClassVar[str] = ""

    def __init__(
        self,
        device_id: str,
        config: DeviceConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device_id = device_id
        self.config = config
        self._sleep = sleep
        self._clock = clock

    def connect(self) -> None:  # noqa: B027 - optional hook
        pass

    @abstractmethod
    def snapshot(self) -> DeviceState: ...

    @abstractmethod
    def apply_frame(self, frame: Frame) -> None: ...

    @abstractmethod
    def restore(self, state: DeviceState) -> None: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    @classmethod
    def discover(cls, timeout: float) -> list[DiscoveredDevice]:
        """Find devices on the network or bus. Drivers without discovery return nothing."""
        return []

    def pair(self, timeout: float) -> dict[str, Any]:
        """Obtain credentials, returned as config fields to store for this device."""
        raise DriverError(f"the {self.name} driver doesn't need pairing")

    def timeline(self, effect: AnyEffect) -> Timeline:
        """The frames :meth:`play` applies. Drivers can adjust them, e.g. to add fading."""
        return render(effect, self.fps)

    def play(self, effect: AnyEffect) -> None:
        """Play ``effect``, blocking until it finishes."""
        timeline = self.timeline(effect)
        start = self._clock()
        frames = timeline.frames
        for i, frame in enumerate(frames):
            # A slow device can fall behind; skip frames whose successor is already due so the
            # effect keeps its length instead of stretching. The last frame is always shown.
            if i + 1 < len(frames) and self._clock() >= start + frames[i + 1].t:
                continue
            self._wait_until(start + frame.t)
            self.apply_frame(frame)
        self._wait_until(start + timeline.duration)

    def _wait_until(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining > 0:
            self._sleep(remaining)
