"""Probe a USB Logitech G102/G203 LIGHTSYNC over HID++ 2.0.

    uv run python spikes/g102_probe.py            # read-only: features, LED clusters, profiles
    uv run python spikes/g102_probe.py --write    # also show red/green, then restore

Throwaway exploration, not part of the package. Nothing it writes is saved to flash.

Findings on a G102 LIGHTSYNC (0xc092, HID++ 4.2), confirmed by eye:
- No 0x8070. Lighting is 0x8071 (RGB Effects), 0x8081 (per-LED) and 0x8100 (onboard profiles).
- hidapi opens exclusively on macOS by default, which fails while G HUB's agent runs.
- In onboard mode every lighting write is accepted but ignored, even with software control on.
- The 0x8071 fixed effect is ignored even in host mode (the LEDs just go dark).
- Host mode + 0x8071 software control [3, 7] + 0x8081 LEDs 1-3 + frame end works.
- Restoring software control, onboard mode and the current profile brings the lighting back.
"""

import subprocess
import sys
import time

from ambient.drivers import logitech_g102 as g

FEATURE_SET = 0x0001
FEATURE_COLOR_LED_EFFECTS = 0x8070
FEATURE_RGB_EFFECTS = 0x8071
FEATURE_ONBOARD_PROFILES = 0x8100


def u16(b: bytes) -> int:
    return int.from_bytes(b[:2], "big")


def main(write: bool) -> None:
    found = g._enumerate(None)
    for d in found:
        print(f"hid: pid=0x{d['product_id']:04x} usage={d['usage_page']:#06x}/{d['usage']:#04x}")
    if not found:
        sys.exit("no G102 found")
    transport = g.HidapiTransport(found[0]["path"])
    hidpp = g.Hidpp(transport, timeout=1.0, clock=time.monotonic)
    try:
        ping = hidpp.request(0, 1, bytes([0, 0, 0x5A]))
        print(f"HID++ protocol {ping[0]}.{ping[1]}")
        fs = hidpp.feature_index(FEATURE_SET)
        assert fs is not None
        for i in range(hidpp.request(fs, 0)[0] + 1):
            reply = hidpp.request(fs, 1, bytes([i]))
            print(f"  feature [{i:2}] 0x{u16(reply):04x} v{reply[3]}")
        print("0x8070 index:", hidpp.feature_index(FEATURE_COLOR_LED_EFFECTS))

        rgb = hidpp.feature_index(FEATURE_RGB_EFFECTS)
        assert rgb is not None
        info = hidpp.request(rgb, 0, bytes([0xFF, 0xFF, 0]))
        print(f"0x8071 [{rgb}] device info: {info[:8].hex()} clusters={info[2]}")
        for c in range(info[2]):
            ci = hidpp.request(rgb, 0, bytes([c, 0xFF, 0]))
            print(f"  cluster {c}: {ci[:8].hex()} location={u16(ci[2:])} effects={ci[4]}")
            for e in range(ci[4]):
                ei = hidpp.request(rgb, 0, bytes([c, e, 0]))
                print(
                    f"    [{e}] id=0x{u16(ei[2:]):04x} caps=0x{u16(ei[4:]):04x} "
                    f"period={u16(ei[6:])} raw={ei[:10].hex()}"
                )
        for label, params in (("get sw control", b"\x00"), ("get power mode", b"\x00")):
            fn = 5 if "sw" in label else 8
            try:
                print(f"  {label}: {hidpp.request(rgb, fn, params)[:8].hex()}")
            except g.HidppError as exc:
                print(f"  {label}: {exc}")

        ob = hidpp.feature_index(FEATURE_ONBOARD_PROFILES)
        assert ob is not None
        print(f"0x8100 [{ob}] info: {hidpp.request(ob, 0)[:16].hex()}")
        print("  mode (1 onboard, 2 host):", hidpp.request(ob, 2)[0])
        current = hidpp.request(ob, 4)
        print(f"  current profile: {current[:4].hex()}")
        print("G HUB running:", is_ghub_running())

        if write:
            try_write(hidpp, rgb, ob, current[:2])
    finally:
        transport.close()


def show(hidpp: g.Hidpp, leds: int, r: int, gr: int, b: int) -> None:
    zones = b"".join(bytes([led, r, gr, b]) for led in (1, 2, 3)) + b"\xff"
    hidpp.request(leds, 1, zones)
    hidpp.request(leds, 7)


def try_write(hidpp: g.Hidpp, rgb: int, ob: int, profile: bytes) -> None:
    leds = hidpp.feature_index(0x8081)
    assert leds is not None
    mode = hidpp.request(ob, 2)[0]
    sw_control = hidpp.request(rgb, 5, b"\x00")[1:3]
    try:
        hidpp.request(ob, 1, b"\x02")
        hidpp.request(rgb, 5, b"\x01\x03\x07")
        for color in ((255, 0, 0), (0, 255, 0)):
            show(hidpp, leds, *color)
            print("showing", color)
            time.sleep(3)
        t = time.monotonic()
        for i in range(20):
            show(hidpp, leds, 0, 0, 255 if i % 2 else 0)
        print(f"20 frames took {(time.monotonic() - t) * 1000:.0f} ms")
    finally:
        hidpp.request(rgb, 5, b"\x01" + sw_control)
        hidpp.request(ob, 1, bytes([mode]))
        hidpp.request(ob, 3, profile)
        print("restored mode", mode, "and profile", profile.hex())


def is_ghub_running() -> bool:
    result = subprocess.run(["pgrep", "-if", "lghub_agent"], capture_output=True, check=False)
    return result.returncode == 0


if __name__ == "__main__":
    main("--write" in sys.argv[1:])
