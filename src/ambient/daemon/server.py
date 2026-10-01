"""The daemon process: hook events from the unix socket, a REST API and the static web UI.

Everything is bound to loopback. Because any web page the user visits can still send requests to
``127.0.0.1``, requests must carry a loopback ``Host`` (against DNS rebinding) and a loopback
``Origin`` when they have one, and writes must be JSON, which browsers won't send cross-origin
without a CORS preflight that this server never approves.
"""

import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ambient import __version__, hooks_install, log, pairing, paths
from ambient.config import (
    Config,
    ConfigError,
    DeviceConfig,
    EventName,
    json_schema,
    load_config,
    load_raw,
    save_config,
)
from ambient.daemon.control import DEFAULT_PORT, DRAIN_TIMEOUT, HTTP_GRACE
from ambient.daemon.listener import EventListener
from ambient.daemon.queue import DeviceQueues
from ambient.drivers import UnknownDriverError, available_drivers, get_driver
from ambient.drivers.base import Driver, DriverError
from ambient.effects import Effect
from ambient.events import HANDLED_EVENTS, HookEvent

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"

WEB_DIR = Path(__file__).resolve().parent.parent / "web_dist"

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class PreviewRequest(BaseModel):
    device: Annotated[list[str], Field(min_length=1)]
    effect: Effect
    # Unsaved device settings from the editor; saved ones are used for devices not listed here.
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)


class EffectRequest(BaseModel):
    effect: Effect


class DiscoverRequest(BaseModel):
    driver: str | None = None
    timeout: float = Field(default=3.0, gt=0, le=30)


class PairRequest(BaseModel):
    driver: str | None = None
    host: str | None = None
    timeout: float = Field(default=60.0, gt=0, le=120)


class ConfigRequest(BaseModel):
    config: dict[str, Any]


def handle_event(config_path: Path, queues: DeviceQueues, event: HookEvent) -> None:
    """Queue ``event``'s effects, reading the config afresh so edits apply without a restart."""
    if not config_path.exists():
        logger.info("%s: no config at %s, nothing to do", event, config_path)
        return
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("%s: invalid config %s: %s", event, config_path, exc)
        return
    devices = queues.submit_event(config, event)
    if devices:
        logger.info("%s: queued on %s", event, ", ".join(devices))


def _is_loopback(host: str | None) -> bool:
    return host is not None and host.strip("[]") in _LOOPBACK


def create_app(
    queues: DeviceQueues,
    config_path: Path,
    *,
    settings_path: Path | None = None,
    web_dir: Path = WEB_DIR,
) -> FastAPI:
    app = FastAPI(title="ambient", version=__version__, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = urlsplit(f"//{request.headers.get('host', '')}").hostname
        origin = request.headers.get("origin")
        if not _is_loopback(host) or (origin and not _is_loopback(urlsplit(origin).hostname)):
            return PlainTextResponse("forbidden", status_code=403)
        content_type = request.headers.get("content-type", "")
        if request.method not in _SAFE_METHODS and not content_type.startswith("application/json"):
            return PlainTextResponse("expected application/json", status_code=415)
        return await call_next(request)

    def load() -> Config:
        if not config_path.exists():
            return Config()
        try:
            return load_config(config_path)
        except ConfigError as exc:
            raise HTTPException(409, f"the config file is invalid: {exc}") from exc

    def driver_cls(name: str) -> type[Driver]:
        try:
            return get_driver(name)
        except UnknownDriverError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        settings = settings_path or hooks_install.settings_file()
        try:
            installed: list[str] | None = hooks_install.installed_events(settings)
        except hooks_install.HooksError:
            installed = None
        return {
            "version": __version__,
            "pid": os.getpid(),
            "config_path": str(config_path),
            "hooks": {
                "settings_path": str(settings),
                "handled": list(HANDLED_EVENTS),
                "installed": installed,
            },
        }

    @app.get("/api/drivers")
    def drivers() -> list[dict[str, Any]]:
        result = []
        for name in available_drivers():
            try:
                cls = get_driver(name)
            except Exception:
                logger.exception("driver %s failed to load", name)
                continue
            result.append(
                {
                    "name": name,
                    "capabilities": sorted(c.name.lower() for c in cls.capabilities),
                    "pairable": cls.pair is not Driver.pair,
                    "pairing_hint": cls.pairing_hint,
                }
            )
        return result

    @app.get("/api/devices")
    def devices() -> list[dict[str, Any]]:
        config = load()
        states = queues.status()
        return [
            {"name": name, "driver": device.driver, "status": vars(states[name])}
            if name in states
            else {"name": name, "driver": device.driver, "status": None}
            for name, device in config.devices.items()
        ]

    @app.post("/api/discover")
    def discover(request: DiscoverRequest) -> list[dict[str, Any]]:
        if request.driver:
            try:
                found = driver_cls(request.driver).discover(request.timeout)
            except DriverError as exc:
                raise HTTPException(502, str(exc)) from exc
        else:
            found = []
            for name in available_drivers():
                # One broken driver (say, hidapi's native library is missing) mustn't hide the
                # devices the others find.
                try:
                    found += get_driver(name).discover(request.timeout)
                except Exception:
                    logger.exception("discover: driver %s failed", name)
        return [
            {"driver": d.driver, "name": d.name, "host": d.host, "port": d.port, **d.details}
            for d in found
        ]

    @app.post("/api/devices/{name}/pair")
    def pair(name: str, request: PairRequest) -> dict[str, Any]:
        try:
            backup = pairing.pair_device(
                config_path,
                load(),
                name,
                driver=request.driver,
                host=request.host,
                timeout=request.timeout,
            )
        except pairing.PairingError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"backup": str(backup) if backup else None}

    @app.post("/api/devices/{name}/test", status_code=202)
    def test(name: str, request: EffectRequest) -> dict[str, Any]:
        return preview(PreviewRequest(device=[name], effect=request.effect))

    @app.post("/api/preview", status_code=202)
    def preview(request: PreviewRequest) -> dict[str, Any]:
        saved = load().devices
        known = {**saved, **request.devices}
        unknown = [d for d in request.device if d not in known]
        if unknown:
            raise HTTPException(422, f"unknown device(s): {', '.join(unknown)}")
        # Check every device before queuing any, so a bad request plays nothing.
        for name in request.device:
            driver_cls(known[name].driver)
        for name in request.device:
            queues.submit(name, known[name], [request.effect])
        return {"queued": request.device}

    @app.post("/api/events/{event}/simulate", status_code=202)
    def simulate(event: EventName) -> dict[str, Any]:
        return {"queued": queues.submit_event(load(), event)}

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        result: dict[str, Any] = {"path": str(config_path), "exists": config_path.exists()}
        try:
            result["config"], result["error"] = load_raw(config_path), None
        except ConfigError as exc:
            result["config"], result["error"] = None, str(exc)
        return result

    @app.put("/api/config")
    def put_config(request: ConfigRequest) -> dict[str, Any]:
        known = set(available_drivers())
        devices = request.config.get("devices") or {}
        # A ``devices`` that isn't a mapping is left to save_config's validation to report.
        unknown = sorted(
            f"devices.{name}: unknown driver {device.get('driver')!r}"
            for name, device in (devices.items() if isinstance(devices, dict) else [])
            if isinstance(device, dict) and device.get("driver") not in known
        )
        if unknown:
            raise HTTPException(422, "; ".join(unknown))
        try:
            backup = save_config(config_path, request.config)
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"backup": str(backup) if backup else None}

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        return json_schema()

    @app.get("/api/logs")
    def logs(lines: int = 200) -> dict[str, Any]:
        return {"path": str(paths.log_file()), "text": log.tail(max(1, min(lines, 5000)))}

    @app.exception_handler(DriverError)
    async def driver_error(request: Request, exc: DriverError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    if (web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:

        @app.get("/", response_class=PlainTextResponse)
        def no_ui() -> str:
            return "The web UI isn't built. Run `npm ci && npm run build` in web/."

    return app


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config_path: Path | None = None,
    socket_path: Path | None = None,
) -> None:
    """Run the daemon in the foreground until SIGINT/SIGTERM."""
    file_logger = log.setup()
    # uvicorn's own messages (startup, bind errors) go to the same file.
    for handler in file_logger.handlers:
        logging.getLogger("uvicorn").addHandler(handler)
    config_path = config_path or paths.config_file()
    queues = DeviceQueues()
    listener = EventListener(
        socket_path or paths.daemon_socket(),
        lambda event: handle_event(config_path, queues, event),
    )
    try:
        # Bind before recording the state file, so it never names a port we failed to get.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        url = f"http://{host}:{sock.getsockname()[1]}"
        _write_state({"pid": os.getpid(), "url": url})
        logger.info("daemon listening on %s and %s", url, listener.path)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(queues, config_path),
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=HTTP_GRACE,
            )
        )
        server.run(sockets=[sock])
    finally:
        listener.close()
        # Queued effects still play, so devices end up restored.
        queues.close(timeout=DRAIN_TIMEOUT)
        _clear_state()
        logger.info("daemon stopped")


def _write_state(state: dict[str, Any]) -> None:
    path = paths.daemon_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _clear_state() -> None:
    path = paths.daemon_state_file()
    try:
        if json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass
