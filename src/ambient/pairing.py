"""Pair a device and save its credentials to the config file (shared by the CLI and the daemon)."""

from collections.abc import Callable
from pathlib import Path

from ambient.config import Config, ConfigError, DeviceConfig, update_device
from ambient.drivers import UnknownDriverError, get_driver
from ambient.drivers.base import Driver, DriverError


class PairingError(Exception):
    pass


def pair_device(
    path: Path,
    config: Config,
    device: str,
    *,
    driver: str | None = None,
    host: str | None = None,
    timeout: float = 60.0,
    notify: Callable[[str], None] = lambda message: None,
) -> Path | None:
    """Pair ``device`` and merge its credentials into ``path``. Returns the config backup path."""
    existing = config.devices.get(device)
    driver_name = driver or (existing.driver if existing else None)
    if driver_name is None:
        raise PairingError(f"{device!r} isn't configured; pass --driver (e.g. --driver nanoleaf)")
    try:
        driver_cls = get_driver(driver_name)
    except UnknownDriverError as exc:
        raise PairingError(str(exc)) from exc
    if driver_cls.pair is Driver.pair:
        raise PairingError(f"the {driver_name} driver doesn't need pairing")
    options = existing.options if existing and existing.driver == driver_name else {}
    if host:
        options["host"] = host
    try:
        if "host" not in options:
            notify(f"No host given, discovering {driver_cls.name} devices...")
            options["host"] = _discover_one(driver_cls)
        device_config = DeviceConfig.model_validate({"driver": driver_name, **options})
        instance = driver_cls(device, device_config)
        notify(
            f"Pairing with {options['host']}: {driver_cls.pairing_hint} "
            f"(waiting up to {timeout:.0f} s)..."
        )
        try:
            fields = instance.pair(timeout)
        finally:
            instance.close()
        return update_device(path, device, {"driver": driver_name, **fields})
    except (DriverError, ConfigError) as exc:
        raise PairingError(str(exc)) from exc


def _discover_one(driver_cls: type[Driver]) -> str:
    found = driver_cls.discover(3.0)
    if len(found) == 1:
        return found[0].host
    if not found:
        raise PairingError("no devices found; pass --host")
    listing = ", ".join(f"{d.name} ({d.host})" for d in found)
    raise PairingError(f"several devices found, pass --host: {listing}")
