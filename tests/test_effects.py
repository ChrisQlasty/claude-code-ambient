import pytest
from pydantic import ValidationError

from ambient.effects import RGB, Flash, Frame, Pulse, Solid, Timeline, render, smooth

RED = RGB(255, 0, 0)


def test_rgb_hex_roundtrip() -> None:
    assert RGB.from_hex("#00ff60").to_hex() == "#00ff60"
    assert RGB.from_hex("0F0") == RGB(0, 255, 0)


def test_solid_is_a_single_frame() -> None:
    timeline = render(Solid(color=RED, duration=2, brightness=40))
    assert timeline.duration == 2
    assert [(f.t, f.color, f.brightness) for f in timeline.frames] == [(0.0, RED, 40)]


def test_flash_alternates_on_and_off() -> None:
    timeline = render(Flash(color=RED, duration=2, times=2))
    assert [(f.t, f.brightness) for f in timeline.frames] == [
        (0.0, 100),
        (0.5, 0),
        (1.0, 100),
        (1.5, 0),
    ]


@pytest.mark.parametrize("times", [1, 3])
def test_pulse_fades_in_and_out(times: int) -> None:
    timeline = render(Pulse(color=RED, duration=3, times=times, brightness=80), fps=20)
    frames = timeline.frames
    per_pulse = len(frames) // times

    assert len(frames) == times * per_pulse
    assert all(0 <= f.t < timeline.duration for f in frames)
    assert [f.t for f in frames] == sorted(f.t for f in frames)
    for i in range(times):
        pulse = frames[i * per_pulse : (i + 1) * per_pulse]
        levels = [f.brightness for f in pulse]
        peak = levels.index(max(levels))
        assert levels[0] == 0
        assert max(levels) == 80
        assert levels[: peak + 1] == sorted(levels[: peak + 1])
        assert levels[peak:] == sorted(levels[peak:], reverse=True)


def test_pulse_has_at_least_two_frames() -> None:
    assert len(render(Pulse(color=RED, duration=0.01), fps=20).frames) == 2


def test_brightness_defaults_to_full() -> None:
    assert render(Solid(color=RED)).frames[0].brightness == 100


@pytest.mark.parametrize(
    "color", [[300, -5, 0], [255, 0, 0], (255, 0, 0), RGB(256, 0, 0), 0xFF0000]
)
def test_color_rejects_non_hex_and_out_of_range(color: object) -> None:
    with pytest.raises(ValidationError, match="invalid color"):
        Solid.model_validate({"color": color})


def test_smooth_ramps_into_each_frame() -> None:
    red = RGB(255, 0, 0)
    timeline = render(Flash(color=red, duration=2, times=1))
    smoothed = smooth(timeline, transition=0.5, fps=4)

    assert smoothed.duration == 2
    assert [(f.t, f.brightness) for f in smoothed.frames] == [
        (0.0, 50),
        (0.25, 100),
        (1.0, 50),
        (1.25, 0),
    ]
    assert all(f.color == red for f in smoothed.frames)


def test_smooth_never_runs_a_ramp_past_its_frame() -> None:
    timeline = render(Flash(color=RGB(0, 0, 255), duration=0.4, times=2))
    smoothed = smooth(timeline, transition=1.0, fps=20)
    # Each 0.1 s frame gets a 0.1 s ramp, so every frame's value is still reached in time.
    reached = [f for f in smoothed.frames if f.brightness in (0, 100)]
    assert [round(f.t, 3) for f in reached] == [0.05, 0.15, 0.25, 0.35]


def test_smooth_blends_colors() -> None:
    timeline = Timeline([Frame(0, RGB(0, 0, 0), 100), Frame(1, RGB(200, 100, 0), 100)], 2)
    smoothed = smooth(timeline, transition=0.5, fps=4)
    assert [f.color for f in smoothed.frames[2:]] == [RGB(100, 50, 0), RGB(200, 100, 0)]


def test_smooth_disabled() -> None:
    timeline = render(Flash(color=RGB(0, 0, 255), duration=1))
    assert smooth(timeline, transition=0) is timeline
