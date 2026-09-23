import pytest

from ambient.effects import RGB, Flash, Pulse, Solid, render

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
