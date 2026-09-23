"""The ``ambient`` command line."""

from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import TypeAdapter, ValidationError

from ambient import paths
from ambient.config import Config, ConfigError, DeviceConfig, load_config
from ambient.drivers import UnknownDriverError, available_drivers, get_driver
from ambient.effects import AnyEffect, Effect

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
        driver_cls = get_driver(device_config.driver)
    except UnknownDriverError as exc:
        raise _fail(str(exc)) from exc

    driver = driver_cls(device, device_config)
    driver.connect()
    try:
        state = driver.snapshot()
        try:
            driver.play(effect)
        finally:
            driver.restore(state)
    finally:
        driver.close()


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
