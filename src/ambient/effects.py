"""Device-agnostic effect models and the frame generator that renders them.

An effect is rendered into a :class:`Timeline`: a list of frames, each held from
its ``t`` until the next frame's ``t`` (the last one until ``duration``). Drivers
that have no native equivalent just apply the frames in order.
"""

import math
import re
from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema

DEFAULT_BRIGHTNESS = 100
DEFAULT_FPS = 20

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class RGB(NamedTuple):
    r: int
    g: int
    b: int

    @classmethod
    def from_hex(cls, value: str) -> "RGB":
        match = _HEX_RE.match(value.strip())
        if not match:
            raise ValueError(f"invalid color {value!r}, expected '#rgb' or '#rrggbb'")
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return cls(int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))

    def to_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"


def _parse_color(value: object) -> RGB:
    if isinstance(value, str):
        return RGB.from_hex(value)
    # Code may pass RGB directly, but a bare list/tuple from YAML would bypass the hex check,
    # and NamedTuple construction doesn't bound the channels, so both are checked here.
    if isinstance(value, RGB) and all(isinstance(c, int) and 0 <= c <= 255 for c in value):
        return value
    raise ValueError(f"invalid color {value!r}, expected '#rgb' or '#rrggbb'")


Color = Annotated[
    RGB,
    BeforeValidator(_parse_color),
    PlainSerializer(RGB.to_hex, return_type=str),
    WithJsonSchema({"type": "string", "pattern": _HEX_RE.pattern, "examples": ["#00ff60"]}),
]
Brightness = Annotated[int, Field(ge=0, le=100)]


class _EffectBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    color: Color
    brightness: Brightness | None = None
    duration: float = Field(default=1.0, gt=0, le=60)

    @property
    def level(self) -> int:
        return DEFAULT_BRIGHTNESS if self.brightness is None else self.brightness


class Solid(_EffectBase):
    """Show a color for ``duration``."""

    type: Literal["solid"] = "solid"


class Flash(_EffectBase):
    """Blink on/off ``times`` within ``duration``."""

    type: Literal["flash"] = "flash"
    times: int = Field(default=1, ge=1, le=50)


class Pulse(_EffectBase):
    """Smoothly fade in and out ``times`` within ``duration``."""

    type: Literal["pulse"] = "pulse"
    times: int = Field(default=1, ge=1, le=50)


AnyEffect = Solid | Flash | Pulse
Effect = Annotated[AnyEffect, Field(discriminator="type")]


@dataclass(frozen=True, slots=True)
class Frame:
    """A color at ``brightness`` (0-100, 0 meaning dark) starting at ``t`` seconds."""

    t: float
    color: RGB
    brightness: int


@dataclass(frozen=True, slots=True)
class Timeline:
    frames: list[Frame]
    duration: float


def render(effect: AnyEffect, fps: int = DEFAULT_FPS) -> Timeline:
    match effect:
        case Solid():
            frames = [Frame(0.0, effect.color, effect.level)]
        case Flash():
            frames = _render_flash(effect)
        case Pulse():
            frames = _render_pulse(effect, fps)
    return Timeline(frames, effect.duration)


def _render_flash(effect: Flash) -> list[Frame]:
    period = effect.duration / effect.times
    frames = []
    for i in range(effect.times):
        start = i * period
        frames.append(Frame(start, effect.color, effect.level))
        frames.append(Frame(start + period / 2, effect.color, 0))
    return frames


def _render_pulse(effect: Pulse, fps: int) -> list[Frame]:
    period = effect.duration / effect.times
    steps = max(2, round(period * fps))
    frames = []
    for i in range(effect.times):
        for step in range(steps):
            phase = step / steps
            # Raised cosine: 0 → 1 → 0 over one period, smooth at both ends.
            intensity = (1 - math.cos(2 * math.pi * phase)) / 2
            frames.append(
                Frame(i * period + phase * period, effect.color, round(effect.level * intensity))
            )
    return frames
