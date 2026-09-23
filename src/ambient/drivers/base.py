"""Driver interface shared by all devices."""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum, auto
from typing import Any, ClassVar

from ambient.config import DeviceConfig
from ambient.effects import AnyEffect, Frame, render

# Opaque, JSON-serializable per-driver state, so it can be persisted as a baseline.
DeviceState = dict[str, Any]


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

    def play(self, effect: AnyEffect) -> None:
        """Play ``effect``, blocking until it finishes."""
        timeline = render(effect)
        start = self._clock()
        for frame in timeline.frames:
            self._wait_until(start + frame.t)
            self.apply_frame(frame)
        self._wait_until(start + timeline.duration)

    def _wait_until(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining > 0:
            self._sleep(remaining)
