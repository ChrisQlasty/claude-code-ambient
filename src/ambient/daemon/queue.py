"""Per-device effect queues: each device plays its effects in order on its own thread."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from ambient import engine
from ambient.config import Config, DeviceConfig, EventName
from ambient.effects import AnyEffect

log = logging.getLogger(__name__)

# Plays one batch of effects on a device; ``engine.play_on_device`` in production.
Player = Callable[[str, DeviceConfig, list[AnyEffect]], bool]


@dataclass
class DeviceStatus:
    queued: int = 0
    playing: bool = False
    # "ok", or "failed" when the effect was dropped or errored (details are in the log).
    last_result: str | None = None
    last_at: float | None = None


@dataclass(frozen=True)
class _Job:
    device_config: DeviceConfig
    effects: list[AnyEffect]


def _play(device_id: str, device_config: DeviceConfig, effects: list[AnyEffect]) -> bool:
    return engine.play_on_device(device_id, device_config, effects)


class DeviceQueues:
    """One FIFO and worker thread per device, created on first use.

    Devices play in parallel and each device's effects play back to back: the in-memory version
    of direct mode's lock-and-wait. The player opens and closes the device for every batch.
    """

    def __init__(self, player: Player = _play, clock: Callable[[], float] = time.time) -> None:
        self._player = player
        self._clock = clock
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[_Job | None]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._status: dict[str, DeviceStatus] = {}
        self._closed = False

    def submit(self, device_id: str, device_config: DeviceConfig, effects: list[AnyEffect]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("device queues are closed")
            if device_id not in self._queues:
                self._queues[device_id] = queue.Queue()
                self._status[device_id] = DeviceStatus()
                thread = threading.Thread(
                    target=self._run, args=(device_id,), name=f"device-{device_id}", daemon=True
                )
                self._threads[device_id] = thread
                thread.start()
            self._status[device_id].queued += 1
            self._queues[device_id].put(_Job(device_config, effects))

    def submit_event(self, config: Config, event: EventName) -> list[str]:
        """Queue the effects configured for ``event``. Returns the devices involved."""
        plan = engine.resolve(config, event)
        if not plan:
            log.info("%s: no effects configured", event)
        for device_id, effects in plan.items():
            self.submit(device_id, config.devices[device_id], effects)
        return list(plan)

    def status(self) -> dict[str, DeviceStatus]:
        with self._lock:
            return {k: replace(v) for k, v in self._status.items()}

    def close(self, timeout: float | None = None) -> None:
        """Let queued effects finish (so every device gets restored), then stop the threads."""
        with self._lock:
            self._closed = True
            for q in self._queues.values():
                q.put(None)
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout)

    def _run(self, device_id: str) -> None:
        q = self._queues[device_id]
        status = self._status[device_id]
        while (job := q.get()) is not None:
            with self._lock:
                status.queued -= 1
                status.playing = True
            ok = False
            try:
                ok = self._player(device_id, job.device_config, job.effects)
            except Exception:
                # play_on_device never raises, but a broken player mustn't kill the device thread.
                log.exception("%s: effect failed", device_id)
            with self._lock:
                status.playing = False
                status.last_result = "ok" if ok else "failed"
                status.last_at = self._clock()
