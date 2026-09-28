"""hidapi plumbing shared by the USB drivers."""

import ctypes
import logging
import sys
import weakref
from types import ModuleType
from typing import Any, Protocol

from ambient.drivers.base import DriverError

log = logging.getLogger(__name__)


class HidTransport(Protocol):
    def write(self, data: bytes) -> None:
        """Send an output report; ``data`` starts with the report ID."""
        ...

    def read(self, timeout: float) -> bytes:
        """The next input report, or ``b""`` if none arrives within ``timeout`` seconds."""
        ...

    def close(self) -> None: ...


class HidapiTransport:
    def __init__(self, path: bytes, *, report_size: int, device: str) -> None:
        hid = import_hid()
        _allow_shared_open(hid.__file__)
        self._report_size = report_size
        self._name = device
        self._device = hid.device()
        try:
            self._device.open_path(path)
        except OSError as exc:
            raise DriverError(f"can't open the {device} ({exc})") from exc

    def write(self, data: bytes) -> None:
        if self._device.write(data) < 0:
            raise DriverError(f"HID write failed; was the {self._name} unplugged?")

    def read(self, timeout: float) -> bytes:
        return bytes(self._device.read(self._report_size, max(1, round(timeout * 1000))))

    def close(self) -> None:
        self._device.close()


def _allow_shared_open(library: str) -> None:
    """On macOS, stop hidapi from opening devices exclusively.

    The exclusive default fails whenever another app (G HUB, NuPhyIO) or macOS itself already
    has the device open. The Python binding doesn't wrap the setting, so it's called through
    ctypes. ``hid_init`` resets it, which has already run by the time a device path was enumerated.
    """
    if sys.platform != "darwin":
        return
    try:
        ctypes.CDLL(library).hid_darwin_set_open_exclusive(0)
    except (OSError, AttributeError):
        log.debug("hid_darwin_set_open_exclusive unavailable; opening exclusively")


def import_hid() -> Any:  # hidapi ships no type information
    try:
        import hid
    except ImportError as exc:
        raise DriverError(f"hidapi isn't available: {exc}") from exc
    _skip_exit_cleanup(hid)
    return hid


def _skip_exit_cleanup(module: ModuleType) -> None:
    """Drop the ``hid_exit`` call the hidapi module registers for interpreter exit.

    On macOS, ``hid_init`` attaches to the run loop of whichever thread first used hidapi (an
    engine worker thread), and ``hid_exit`` on the main thread at exit then aborts the process
    while detaching from that thread's run loop. The OS releases the devices on exit anyway.
    """
    registry: dict[weakref.finalize[Any, Any], Any] = getattr(weakref.finalize, "_registry", {})
    for finalizer in list(registry):
        info = finalizer.peek()
        if info is not None and info[0] is module:
            finalizer.detach()
