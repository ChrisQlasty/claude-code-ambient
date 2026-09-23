"""Probe a paired Nanoleaf device: does ``displayTemp`` work, and does brightness 0 go dark?

    uv run python spikes/nanoleaf_probe.py <device-name-in-config>

Throwaway exploration, not part of the package. Snapshots first and always restores at the end.
"""

import json
import sys
import time

from ambient import paths
from ambient.config import load_config
from ambient.drivers.nanoleaf import NanoleafDriver


def main(device: str) -> None:
    config = load_config(paths.config_file()).devices[device]
    driver = NanoleafDriver(device, config)
    info = driver._request("GET", "/")
    print("model:", info.get("model"), "firmware:", info.get("firmwareVersion"))
    state = driver.snapshot()
    print("snapshot:", json.dumps(state))
    try:
        layout = driver._request("GET", "/panelLayout/layout")
        panels = [p["panelId"] for p in layout["positionData"]]
        print(f"{len(panels)} panel ids:", panels)

        # Static red on every panel for 3 s; the device should revert to the previous scene itself.
        anim_data = f"{len(panels)} " + " ".join(f"{p} 1 255 0 0 0 1" for p in panels)
        body = {
            "write": {
                "command": "displayTemp",
                "duration": 3,
                "animType": "static",
                "animData": anim_data,
                "loop": False,
                "palette": [],
            }
        }
        print("displayTemp:", driver._client.put(f"/{driver.options.token}/effects", json=body))
        time.sleep(4)
        print("after displayTemp:", json.dumps(driver.snapshot()), "(did it revert by itself?)")

        input("Press Enter to set brightness 0 (watch whether the lights go fully dark)...")
        driver._request("PUT", "/state", {"on": {"value": True}, "brightness": {"value": 0}})
        time.sleep(2)
    finally:
        driver.restore(state)
        driver.close()
        print("restored")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "lines")
