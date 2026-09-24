"""Hook entrypoint (``ambient fire``): read the hook JSON, hand off to a detached worker, exit.

This runs on every hook, so it must return in well under 100 ms, print nothing and always
exit 0. It deliberately imports only the stdlib (no typer, pydantic or drivers): loading the
config and driving devices happens in :mod:`ambient.worker`.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from typing import BinaryIO

from ambient.events import is_handled


def event_name(payload: bytes) -> str | None:
    """The handled ``hook_event_name`` in a hook's stdin payload, or ``None``."""
    data = json.loads(payload or b"{}")
    name = data.get("hook_event_name") if isinstance(data, dict) else None
    return name if is_handled(name) else None


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


def fire(stdin: BinaryIO, spawn: Callable[[str], None] = spawn_worker) -> None:
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
