"""Hook entrypoint (``ambient fire``): read the hook JSON, hand it off, exit.

The event goes to the daemon over its unix socket when one is listening, and otherwise to a
detached direct-mode worker. This runs on every hook, so it must return in well under 100 ms,
print nothing and always exit 0. It deliberately imports only the stdlib (no typer, pydantic or
drivers): loading the config and driving devices happens in the daemon or :mod:`ambient.worker`.
"""

import json
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from ambient import paths
from ambient.events import is_handled

# A local daemon accepts in microseconds; this only bounds a wedged one.
DAEMON_TIMEOUT = 0.05


def event_name(payload: bytes) -> str | None:
    """The handled ``hook_event_name`` in a hook's stdin payload, or ``None``."""
    data = json.loads(payload or b"{}")
    name = data.get("hook_event_name") if isinstance(data, dict) else None
    return name if is_handled(name) else None


def send_to_daemon(event: str, path: Path | None = None) -> bool:
    """Hand ``event`` to a running daemon. False if none is listening (or the socket is stale)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(DAEMON_TIMEOUT)
        try:
            sock.connect(str(path or paths.daemon_socket()))
        except OSError:
            return False
        # Once connected, the daemon owns the event: falling back after a failed send could
        # play it twice, so a send error propagates and is logged instead.
        sock.sendall(json.dumps({"event": event}).encode() + b"\n")
    return True


def dispatch(event: str) -> None:
    if not send_to_daemon(event):
        spawn_worker(event)


def spawn_worker(event: str) -> None:
    # A new session detaches the worker from Claude Code's process group, so the hook returns
    # right away and the effect survives the hook process exiting. ``-P`` keeps the cwd (the
    # user's project) off sys.path, so a project's own ``yaml.py`` or ``ambient/`` can't
    # shadow the worker's imports.
    subprocess.Popen(
        [sys.executable, "-P", "-m", "ambient.worker", event],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def fire(stdin: BinaryIO, spawn: Callable[[str], None] = dispatch) -> None:
    """Never raises: failures are logged to the log file."""
    try:
        if stdin.isatty():
            return
        event = event_name(stdin.read())
        if event is not None:
            spawn(event)
    except ValueError as exc:
        from ambient import log

        log.setup().warning("fire: ignoring invalid hook JSON: %s", exc)
    except Exception:
        # Imported lazily to keep the happy path lean.
        from ambient import log

        log.setup().exception("fire failed")


def main() -> None:
    fire(sys.stdin.buffer)
    sys.exit(0)
