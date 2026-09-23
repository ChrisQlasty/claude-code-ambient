"""Driver registry, backed by the ``ambient.drivers`` entry point group."""

from importlib.metadata import entry_points

from ambient.drivers.base import Driver

ENTRY_POINT_GROUP = "ambient.drivers"


class UnknownDriverError(LookupError):
    pass


def available_drivers() -> list[str]:
    return sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))


def get_driver(name: str) -> type[Driver]:
    matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
    if not matches:
        known = ", ".join(available_drivers()) or "none"
        raise UnknownDriverError(f"unknown driver {name!r} (available: {known})")
    driver: type[Driver] = next(iter(matches)).load()
    return driver
