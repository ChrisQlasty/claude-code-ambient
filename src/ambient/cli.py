"""The ``ambient`` command line."""

import difflib
import logging
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import TypeAdapter, ValidationError

from ambient import engine, fire, hooks_install, log, pairing, paths
from ambient.config import Config, ConfigError, DeviceConfig, load_config
from ambient.daemon import control
from ambient.drivers import UnknownDriverError, available_drivers, get_driver
from ambient.drivers.base import Driver, DriverError
from ambient.effects import AnyEffect, Effect
from ambient.events import HookEvent

app = typer.Typer(no_args_is_help=True, help="Ambient light effects for Claude Code events.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate the config file.")
app.add_typer(config_app, name="config")
daemon_app = typer.Typer(no_args_is_help=True, help="Run the background daemon (web UI + hooks).")
app.add_typer(daemon_app, name="daemon")

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
        # USB devices have no port.
        address = f"{d.host}:{d.port}" if d.port else d.host
        typer.echo(f"{d.driver}\t{d.name}\t{address}\t{details}".rstrip())


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
    config = _load(config_path, required=False)
    try:
        backup = pairing.pair_device(
            path, config, device, driver=driver, host=host, timeout=timeout, notify=typer.echo
        )
    except pairing.PairingError as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"paired {device!r}, saved to {path}", fg=typer.colors.GREEN)
    if backup:
        typer.echo(f"previous config backed up to {backup}")


def _driver_cls(name: str) -> type[Driver]:
    try:
        return get_driver(name)
    except UnknownDriverError as exc:
        raise _fail(str(exc)) from exc


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
    typer.echo(f"# {paths.log_file()}", err=True)
    typer.echo(log.tail(lines), nl=False)


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


PortOption = Annotated[int, typer.Option(help="Web UI port on 127.0.0.1.")]


@daemon_app.command("run")
def daemon_run(port: PortOption = control.DEFAULT_PORT) -> None:
    """Run the daemon in the foreground (Ctrl-C to stop)."""
    # Imported here: FastAPI and uvicorn would slow down every other command.
    from ambient.daemon.listener import AlreadyRunningError
    from ambient.daemon.server import serve

    typer.echo(f"serving http://127.0.0.1:{port} (logs: {paths.log_file()})")
    try:
        serve(port=port)
    except (AlreadyRunningError, OSError) as exc:
        raise _fail(str(exc)) from exc


@daemon_app.command("start")
def daemon_start(port: PortOption = control.DEFAULT_PORT) -> None:
    """Start the daemon in the background."""
    info = _start_daemon(port)
    typer.secho(f"daemon running (pid {info.pid}) at {info.url}", fg=typer.colors.GREEN)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon. Hooks fall back to direct mode."""
    try:
        stopped = control.stop()
    except control.DaemonError as exc:
        raise _fail(str(exc)) from exc
    typer.echo("daemon stopped" if stopped else "daemon isn't running")


@daemon_app.command("status")
def daemon_status() -> None:
    """Show whether the daemon is running. Exits 1 if it isn't."""
    info = control.running()
    if info is None:
        typer.echo("daemon isn't running (hooks use direct mode)")
        raise typer.Exit(1)
    socket = "ok" if info.socket_ok else "not responding"
    typer.echo(f"daemon running (pid {info.pid}) at {info.url}, hook socket {socket}")


@daemon_app.command("install-login-item")
def daemon_install_login_item(port: PortOption = control.DEFAULT_PORT) -> None:
    """Start the daemon now and at every login (macOS LaunchAgent)."""
    if control.running():
        control.stop()
    try:
        path = control.install_login_item(hooks_install.ambient_executable(), port)
    except (control.DaemonError, hooks_install.HooksError) as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"installed {path}", fg=typer.colors.GREEN)


@daemon_app.command("uninstall-login-item")
def daemon_uninstall_login_item() -> None:
    """Remove the LaunchAgent and stop the daemon it started."""
    removed = control.uninstall_login_item()
    typer.echo(f"removed {control.launch_agent_path()}" if removed else "no login item installed")


@app.command()
def ui(
    port: PortOption = control.DEFAULT_PORT,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Open the web UI, starting the daemon if needed."""
    import webbrowser

    info = _start_daemon(port)
    typer.echo(info.url)
    if open_browser:
        webbrowser.open(info.url)


def _start_daemon(port: int) -> control.DaemonInfo:
    try:
        return control.start(port)
    except control.DaemonError as exc:
        raise _fail(str(exc)) from exc


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'effect'}: {err['msg']}"
        for err in exc.errors()
    )
