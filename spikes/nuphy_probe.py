"""Probe a NuPhy Air75 V3 over its USB configuration interface ("S4" protocol, as used by NuPhyIO).

    uv run python spikes/nuphy_probe.py            # read-only: firmware, LED count, lighting state
    uv run python spikes/nuphy_probe.py --write    # also show green keys for 3 s, then restore

Throwaway exploration, not part of the package. Protocol from NuPhyLab's HIDBridge (MIT), pda/kbvu
and ShallowRed/nuphy-leds. Only the A0, A1, D1, D2, D5, D6 and D8 opcodes are ever sent.

Report: 64 bytes, report ID 0. [0] 0x55 (0xAA in replies), [1] opcode, [2] 0, [3] checksum of
bytes 4..63, [4] length, [5..6] address LE, [7] handle, [8..] payload. Bytes 4.. are XOR'd with a
session key, derived from the A1 reply whose payload byte 3 is a constant 0xAA.

Findings on an Air75 V3 ANSI (19f5:1028), confirmed by eye and by D2 readback:
- Bluetooth (07d7:0000) and presumably the 2.4 GHz dongle expose only keyboard/mouse/consumer
  collections and standard GATT services. The configuration interface (usage 0x01/0x00,
  interface 3) exists only over the USB cable. Opening it needs no macOS permission.
- Firmware 1.0.9.6 has no per-key custom effect: D6 clamps effect 21 to 20 (a stock pattern), and
  no effect index shows D8 colors. Every write of the main color, including whole-state D6 writes,
  rotated its hue by -3/256; a brightness write rescales it to full value. After updating to
  1.0.16.6 in NuPhyIO, effect 21 shows the D8 table.
- A 1-byte D6 write of the effect field (address 0) is enough to enter effect 21; the RGB flag
  and brightness can stay as they are. A full 84-key D8 frame takes ~22 ms.
- D6 is saved to flash: effect 21 survived a power cycle, while the D8 table came back black.
"""

import sys
import time

from ambient.drivers import hid as h

VENDOR_ID, PRODUCT_ID = 0x19F5, 0x1028
SIZE = 64


def checksum(b: bytes | bytearray) -> int:
    return sum(b[4:]) & 0xFF


class S4:
    def __init__(self, path: bytes) -> None:
        hid = h.import_hid()
        h._allow_shared_open(hid.__file__)
        self.dev = hid.device()
        self.dev.open_path(path)
        self.key = 0

    def exchange(self, frame: bytes, timeout: float = 0.65) -> bytes:
        while self.dev.read(SIZE, 1):  # drop stale replies
            pass
        if self.dev.write(b"\0" + frame) < 0:
            raise RuntimeError("write failed")
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            raw = bytes(self.dev.read(SIZE, max(1, round(remaining * 1000))))
            if len(raw) == SIZE and raw[0] == 0xAA and raw[1] == frame[1]:
                assert raw[3] == checksum(raw), "bad checksum"
                return raw
        raise TimeoutError(f"no reply to {frame[1]:02x}")

    def frame(
        self, cmd: int, length: int, address: int = 0, payload: bytes = b"", handle: int = 0
    ) -> bytes:
        k = self.key
        b = bytearray(SIZE)
        b[0], b[1] = 0x55, cmd
        b[4:8] = bytes([length ^ k, (address & 0xFF) ^ k, (address >> 8) ^ k, handle ^ k])
        for i, v in enumerate(payload):
            b[8 + i] = v ^ k
        b[3] = checksum(b)
        return bytes(b)

    def request(
        self, cmd: int, length: int, address: int = 0, payload: bytes = b"", handle: int = 0
    ) -> bytes:
        raw = self.exchange(self.frame(cmd, length, address, payload, handle))
        route = bytes([length, address & 0xFF, address >> 8, handle])
        if raw[4:8] != route and bytes(x ^ self.key for x in raw[4:8]) != route:
            raise RuntimeError(f"route mismatch {raw[4:8].hex()}")
        return bytes(x ^ self.key for x in raw[8 : 8 + length])

    def identify(self) -> bytes:
        self.key = 0
        raw = self.exchange(self.frame(0xA1, 8))
        self.key = raw[11] ^ 0xAA
        info = bytes(x ^ self.key for x in raw[8:16])
        assert info[3] == 0xAA, info.hex()
        return info


def version(info: bytes) -> str:
    return f"{info[2]}.{info[1]}.{info[0]}.{info[4] | info[5] << 8}"


def read_rgb(s: S4) -> bytes:
    out = b""
    while len(out) < 312:
        out += s.request(0xD2, min(54, 312 - len(out)), len(out))
        time.sleep(0.008)
    return out


def main(write: bool) -> None:
    hid = h.import_hid()
    found = [
        d for d in hid.enumerate(VENDOR_ID, PRODUCT_ID) if (d["usage_page"], d["usage"]) == (1, 0)
    ]
    for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        print(f"hid: if={d['interface_number']} usage={d['usage_page']:#06x}/{d['usage']:#04x}")
    if not found:
        sys.exit("no Air75 V3 configuration interface on USB (is it on Bluetooth/2.4G?)")
    s = S4(found[0]["path"])
    t = time.monotonic()
    info = s.identify()
    print(f"A1 firmware {info.hex(' ')} -> {version(info)}  key=0x{s.key:02x}")
    print("D1 LEDs:", s.request(0xD1, 1)[0])
    profile = s.request(0xA0, 8)
    print(f"A0 geometry {profile.hex(' ')} (profile {'Mac' if profile[0] == 0 else 'Win'})")
    state = s.request(0xD5, 17, handle=profile[0])
    print(f"D5 state {list(state)}")
    print(
        f"   main: effect={state[0]} bright={state[1]} speed={state[2]} dir={state[3]} "
        f"rgb={state[4]} colorIdx={state[5]} rgb=#{state[6:9].hex()}"
    )
    print(
        f"   side: effect={state[9]} bright={state[10]} speed={state[11]} rgb={state[12]} "
        f"colorIdx={state[13]} rgb=#{state[14:17].hex()}"
    )
    print(f"connect+read took {(time.monotonic() - t) * 1000:.0f} ms")
    rgb = read_rgb(s)
    print("D2 first keys:", " ".join(f"#{rgb[i : i + 3].hex()}" for i in range(0, 24, 3)))
    if write:
        try_write(s, profile[0], state)


def set_state(s: S4, handle: int, state: bytes) -> bytes:
    echo = s.request(0xD6, 17, payload=state, handle=handle)
    assert echo == state, (list(echo), list(state))
    time.sleep(0.12)
    return s.request(0xD5, 17, handle=handle)


def paint(s: S4, color: bytes) -> float:
    t = time.monotonic()
    for start in range(0, 84, 13):
        records = b"".join(bytes([i]) + color for i in range(start, min(start + 13, 84)))
        assert s.request(0xD8, len(records), payload=records) == records
    return time.monotonic() - t


def try_write(s: S4, handle: int, original: bytes) -> None:
    custom = bytearray(original)
    custom[0], custom[1], custom[4] = 21, 100, 1
    try:
        print("armed:", list(set_state(s, handle, bytes(custom))))
        print(f"paint green took {paint(s, bytes([0, 255, 0])) * 1000:.0f} ms")
        time.sleep(3)
        t = time.monotonic()
        for i in range(10):
            paint(s, bytes([0, 0, 255 if i % 2 else 0]))
        print(f"10 frames took {(time.monotonic() - t) * 1000:.0f} ms")
    finally:
        print("restored:", list(set_state(s, handle, original)))


if __name__ == "__main__":
    main("--write" in sys.argv[1:])
