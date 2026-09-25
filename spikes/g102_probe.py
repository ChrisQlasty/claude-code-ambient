"""Probe a USB Logitech G102/G203 LIGHTSYNC over HID++ 2.0.

    uv run python spikes/g102_probe.py            # read-only: features, LED clusters, profiles
    uv run python spikes/g102_probe.py --write    # also set colors, then try ways to restore

Throwaway exploration, not part of the package. Writes use the RAM-only persistence flag.
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


def set_color(hidpp: g.Hidpp, rgb: int, index: int, r: int, gr: int, b: int) -> None:
    params = bytes([0, index, r, gr, b]).ljust(12, b"\0") + bytes([0])  # persist: RAM only
    hidpp.request(rgb, 1, params)


def try_write(hidpp: g.Hidpp, rgb: int, ob: int, profile: bytes) -> None:
    fixed = next(e for e in range(16) if u16(hidpp.request(rgb, 0, bytes([0, e, 0]))[2:]) == 0x0001)
    try:
        for color in ((255, 0, 0), (0, 255, 0), (0, 0, 0)):
            set_color(hidpp, rgb, fixed, *color)
            print("set", color)
            time.sleep(1.5)
        t = time.monotonic()
        for i in range(20):
            set_color(hidpp, rgb, fixed, 0, 0, 255 if i % 2 else 0)
        print(f"20 writes took {(time.monotonic() - t) * 1000:.0f} ms")
        set_color(hidpp, rgb, fixed, 255, 0, 255)
        time.sleep(1.5)
    finally:
        hidpp.request(ob, 3, profile)
        print("re-selected profile", profile.hex(), "- did the original lighting come back?")


def is_ghub_running() -> bool:
    result = subprocess.run(["pgrep", "-if", "lghub_agent"], capture_output=True, check=False)
    return result.returncode == 0


if __name__ == "__main__":
    main("--write" in sys.argv[1:])
