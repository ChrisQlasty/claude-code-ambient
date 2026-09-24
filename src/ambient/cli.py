"""The ``ambient`` command line."""

import difflib
import logging
from collections import deque
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import TypeAdapter, ValidationError

from ambient import engine, fire, hooks_install, log, paths
from ambient.config import Config, ConfigError, DeviceConfig, load_config, update_device
from ambient.drivers import UnknownDriverError, available_drivers, get_driver
from ambient.drivers.base import Driver, DriverError
from ambient.effects import AnyEffect, Effect
from ambient.events import HookEvent

app = typer.Typer(no_args_is_help=True, help="Ambient light effects for Claude Code events.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate the config file.")
app.add_typer(config_app, name="config")

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Config file (default: $AMBIENT_CONFIG or XDG path)."),
]


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _load(path: Path | None, *, required: bool) -> Config:
    # A missing default config is fine for `test`, but a path the user named (via --config or
    # $AMBIENT_CONFIG) that doesn't exist is almost certainly a typo, so it's an error.
    explicit = path is not None or paths.config_file_overridden()
    path = path or paths.config_file()
    if not required and not explicit and not path.exists():
        return Config()
    try:
        return load_config(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _resolve_device(config: Config, name: str) -> DeviceConfig:
    """A configured device, or a driver name used directly (e.g. ``console``) with no options."""
    if name in config.devices:
        return config.devices[name]
    if name in available_drivers():
        return DeviceConfig(driver=name)
    configured = ", ".join(config.devices) or "none"
    raise _fail(f"unknown device {name!r} (configured: {configured})")


@app.command()
def test(
    device: Annotated[str, typer.Argument(help="Configured device name, or a driver name.")],
    effect_type: Annotated[Literal["solid", "flash", "pulse"], typer.Argument(metavar="EFFECT")],
    color: Annotated[str, typer.Argument(help="Hex color, e.g. '#f00' or '#00ff60'.")],
    duration: Annotated[float, typer.Option(help="Seconds.")] = 1.0,
    times: Annotated[int, typer.Option(help="Repetitions (flash, pulse).")] = 1,
    brightness: Annotated[int | None, typer.Option(help="0-100.")] = None,
    config_path: ConfigOption = None,
) -> None:
    """Play one effect on a device, then restore its previous state."""
    fields: dict[str, object] = {"type": effect_type, "color": color, "duration": duration}
    if effect_type != "solid":
        fields["times"] = times
    if brightness is not None:
        fields["brightness"] = brightness
    try:
        effect: AnyEffect = TypeAdapter(Effect).validate_python(fields)
    except ValidationError as exc:
        raise _fail(_format_errors(exc)) from exc

    device_config = _resolve_device(_load(config_path, required=False), device)
    try:
        driver = _driver_cls(device_config.driver)(device, device_config)
        driver.connect()
        try:
            state = driver.snapshot()
            try:
                driver.play(effect)
            finally:
                driver.restore(state)
        finally:
            driver.close()
    except DriverError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def discover(
    driver: Annotated[str | None, typer.Argument(help="Only this driver (default: all).")] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to listen.")] = 3.0,
) -> None:
    """Find devices on the local network."""
    names = [driver] if driver else available_drivers()
    try:
        found = [d for name in names for d in _driver_cls(name).discover(timeout)]
    except DriverError as exc:
        raise _fail(str(exc)) from exc
    if not found:
        typer.echo("no devices found")
        return
    for d in found:
        details = " ".join(f"{k}={v}" for k, v in sorted(d.details.items()))
        typer.echo(f"{d.driver}\t{d.name}\t{d.host}:{d.port}\t{details}".rstrip())


@app.command()
def pair(
    device: Annotated[str, typer.Argument(help="Device name for the config, e.g. 'lines'.")],
    driver: Annotated[
        str | None, typer.Option(help="Driver, if the device isn't configured yet.")
    ] = None,
    host: Annotated[str | None, typer.Option(help="Device address (default: discover).")] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for pairing mode.")] = 60.0,
    config_path: ConfigOption = None,
) -> None:
    """Pair with a device and save its credentials to the config file."""
    path = config_path or paths.config_file()
    existing = _load(config_path, required=False).devices.get(device)
    driver_name = driver or (existing.driver if existing else None)
    if driver_name is None:
        raise _fail(f"{device!r} isn't configured; pass --driver (e.g. --driver nanoleaf)")
    driver_cls = _driver_cls(driver_name)
    if driver_cls.pair is Driver.pair:
        raise _fail(f"the {driver_name} driver doesn't need pairing")
    options = existing.options if existing and existing.driver == driver_name else {}
    if host:
        options["host"] = host
    if "host" not in options:
        options["host"] = _discover_one(driver_cls)

    try:
        device_config = DeviceConfig.model_validate({"driver": driver_name, **options})
        instance = driver_cls(device, device_config)
        typer.echo(
            f"Pairing with {options['host']}: {driver_cls.pairing_hint} "
            f"(waiting up to {timeout:.0f} s)..."
        )
        try:
            fields = instance.pair(timeout)
        finally:
            instance.close()
        backup = update_device(path, device, {"driver": driver_name, **fields})
    except (DriverError, ConfigError) as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"paired {device!r}, saved to {path}", fg=typer.colors.GREEN)
    if backup:
        typer.echo(f"previous config backed up to {backup}")


def _driver_cls(name: str) -> type[Driver]:
    try:
        return get_driver(name)
    except UnknownDriverError as exc:
        raise _fail(str(exc)) from exc


def _discover_one(driver_cls: type[Driver]) -> str:
    typer.echo(f"No host given, discovering {driver_cls.name} devices...")
    try:
        found = driver_cls.discover(3.0)
    except DriverError as exc:
        raise _fail(str(exc)) from exc
    if len(found) == 1:
        return found[0].host
    if not found:
        raise _fail("no devices found; pass --host")
    listing = ", ".join(f"{d.name} ({d.host})" for d in found)
    raise _fail(f"several devices found, pass --host: {listing}")


@app.command("fire")
def fire_cmd() -> None:
    """Hook entrypoint: read the hook JSON from stdin and play its effects in the background."""
    fire.main()


@app.command()
def simulate(
    event: Annotated[HookEvent, typer.Argument(help="Hook event to simulate.")],
    config_path: ConfigOption = None,
) -> None:
    """Play the effects configured for EVENT in the foreground, as if its hook had fired."""
    config = _load(config_path, required=True)
    if not engine.resolve(config, event):
        typer.echo(f"no effects configured for {event}")
        return
    logger = log.setup()
    # Also surface dropped or failed effects in the terminal, not just the log file.
    stderr = logging.StreamHandler()
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(stderr)
    try:
        engine.run_event(config, event)
    finally:
        logger.removeHandler(stderr)


SettingsOption = Annotated[
    Path | None,
    typer.Option(
        "--settings", help="Claude Code settings file (default: ~/.claude/settings.json)."
    ),
]
DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Show the change without writing.")]


@app.command("install-hooks")
def install_hooks(settings: SettingsOption = None, dry_run: DryRunOption = False) -> None:
    """Add `ambient fire` hooks to Claude Code's settings (backs the file up first)."""
    try:
        command = hooks_install.fire_command(hooks_install.ambient_executable())
        change = hooks_install.plan_install(settings or hooks_install.settings_file(), command)
    except hooks_install.HooksError as exc:
        raise _fail(str(exc)) from exc
    _apply_hooks_change(change, dry_run, done="installed hooks")


@app.command("uninstall-hooks")
def uninstall_hooks(settings: SettingsOption = None, dry_run: DryRunOption = False) -> None:
    """Remove `ambient fire` hooks from Claude Code's settings (backs the file up first)."""
    try:
        change = hooks_install.plan_uninstall(settings or hooks_install.settings_file())
    except hooks_install.HooksError as exc:
        raise _fail(str(exc)) from exc
    _apply_hooks_change(change, dry_run, done="removed hooks")


def _apply_hooks_change(change: hooks_install.Change, dry_run: bool, *, done: str) -> None:
    if not change.changed:
        typer.echo(f"{change.path}: already up to date")
        return
    if dry_run:
        diff = difflib.unified_diff(
            (change.before or "").splitlines(keepends=True),
            (change.after or "").splitlines(keepends=True),
            fromfile=str(change.path),
            tofile=f"{change.path} (new)",
        )
        typer.echo("".join(diff), nl=False)
        return
    backup = hooks_install.apply(change)
    typer.secho(f"{done} in {change.path}", fg=typer.colors.GREEN)
    if backup:
        typer.echo(f"backup: {backup}")


@app.command()
def logs(lines: Annotated[int, typer.Option("--lines", "-n", min=1)] = 50) -> None:
    """Show the end of the log file."""
    path = paths.log_file()
    typer.echo(f"# {path}", err=True)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=lines)
    except FileNotFoundError:
        return
    typer.echo("".join(tail), nl=False)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    typer.echo(paths.config_file())


@config_app.command("validate")
def config_validate(config_path: ConfigOption = None) -> None:
    """Validate the config file."""
    config = _load(config_path, required=True)
    known = set(available_drivers())
    unknown = {
        f"devices.{name}: unknown driver {dev.driver!r}"
        for name, dev in config.devices.items()
        if dev.driver not in known
    }
    if unknown:
        raise _fail("\n".join(sorted(unknown)))
    typer.secho(
        f"ok: {len(config.devices)} device(s), {len(config.events)} event(s)",
        fg=typer.colors.GREEN,
    )


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'effect'}: {err['msg']}"
        for err in exc.errors()
    )
