"""Event → effects resolution and the per-device snapshot/play/restore cycle (direct mode).

Each device is guarded by a file lock, so overlapping hooks play one after another instead of
one snapshotting another's mid-flash state. The snapshot is also persisted as a *baseline*
before playing and removed only after a successful restore: if a worker dies mid-effect, the
next effect restores to that baseline rather than to whatever the dead worker left behind.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ambient import paths
from ambient.config import Config, DeviceConfig, EventName
from ambient.drivers import get_driver
from ambient.drivers.base import DeviceState, Driver
from ambient.effects import AnyEffect

log = logging.getLogger(__name__)

LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.05

DriverFactory = Callable[[str, DeviceConfig], Driver]


def default_driver_factory(device_id: str, config: DeviceConfig) -> Driver:
    return get_driver(config.driver)(device_id, config)


def resolve(config: Config, event: EventName) -> dict[str, list[AnyEffect]]:
    """Effects to play per device for ``event``, in config order."""
    plan: dict[str, list[AnyEffect]] = {}
    for action in config.events.get(event, []):
        for device in action.device:
            plan.setdefault(device, []).append(action.effect)
    return plan


def run_event(
    config: Config,
    event: EventName,
    *,
    driver_factory: DriverFactory = default_driver_factory,
    lock_timeout: float = LOCK_TIMEOUT,
) -> None:
    """Play the effects configured for ``event``, devices in parallel. Never raises."""
    plan = resolve(config, event)
    if not plan:
        log.info("%s: no effects configured", event)
        return
    log.info("%s: playing on %s", event, ", ".join(plan))
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        for device_id, effects in plan.items():
            pool.submit(
                play_on_device,
                device_id,
                config.devices[device_id],
                effects,
                driver_factory=driver_factory,
                lock_timeout=lock_timeout,
            )


def play_on_device(
    device_id: str,
    device_config: DeviceConfig,
    effects: list[AnyEffect],
    *,
    driver_factory: DriverFactory = default_driver_factory,
    lock_timeout: float = LOCK_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Lock, snapshot (or reuse the baseline), play, restore. False if dropped or failed."""
    try:
        with device_lock(device_id, lock_timeout, sleep=sleep, clock=clock) as acquired:
            if not acquired:
                log.warning("%s: busy for %.1fs, dropping effect", device_id, lock_timeout)
                return False
            _play_locked(device_id, driver_factory(device_id, device_config), effects)
            return True
    except Exception:
        log.exception("%s: effect failed", device_id)
        return False


def _play_locked(device_id: str, driver: Driver, effects: list[AnyEffect]) -> None:
    driver.connect()
    try:
        state = load_baseline(device_id)
        if state is None:
            state = driver.snapshot()
            save_baseline(device_id, state)
        else:
            log.warning("%s: restoring to a baseline left by an interrupted effect", device_id)
        try:
            for effect in effects:
                driver.play(effect)
        finally:
            driver.restore(state)
            # Only after a successful restore; otherwise the next effect retries restoring it.
            clear_baseline(device_id)
    finally:
        driver.close()


def _safe_name(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", device_id)


@contextlib.contextmanager
def device_lock(
    device_id: str,
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[bool]:
    """Hold an exclusive per-device lock, waiting up to ``timeout``. Yields whether it was taken.

    ``flock`` locks are released by the OS when the holder dies, so a crashed worker can't
    leave a device locked.
    """
    path = paths.locks_dir() / f"{_safe_name(device_id)}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    yield False
                    return
                sleep(_LOCK_POLL)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _baseline_path(device_id: str) -> Path:
    return paths.baseline_dir() / f"{_safe_name(device_id)}.json"


def load_baseline(device_id: str) -> DeviceState | None:
    path = _baseline_path(device_id)
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("%s: ignoring unreadable baseline %s", device_id, path)
        return None
    return state if isinstance(state, dict) else None


def save_baseline(device_id: str, state: DeviceState) -> None:
    path = _baseline_path(device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def clear_baseline(device_id: str) -> None:
    _baseline_path(device_id).unlink(missing_ok=True)
