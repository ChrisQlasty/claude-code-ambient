"""Unix socket listener for hook events sent by ``ambient fire``.

The protocol is one JSON line per connection, ``{"event": "Stop"}``, and no reply: ``fire`` must
not wait on the daemon.
"""

import json
import logging
import os
import socket
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path

from ambient.events import HookEvent, is_handled

log = logging.getLogger(__name__)

_READ_TIMEOUT = 1.0
_MAX_LINE = 4096


class AlreadyRunningError(Exception):
    pass


class _Handler(socketserver.StreamRequestHandler):
    timeout = _READ_TIMEOUT
    server: "_Server"

    def handle(self) -> None:
        try:
            line = self.rfile.readline(_MAX_LINE)
            if not line.strip():
                # A liveness probe (see is_listening) connects and closes without sending.
                return
            data = json.loads(line)
        except (OSError, ValueError) as exc:
            log.warning("socket: ignoring unreadable message: %s", exc)
            return
        event = data.get("event") if isinstance(data, dict) else None
        if not is_handled(event):
            log.warning("socket: ignoring unknown event %r", event)
            return
        try:
            self.server.on_event(event)
        except Exception:
            log.exception("socket: %s failed", event)


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, on_event: Callable[[HookEvent], None]) -> None:
        self.on_event = on_event
        super().__init__(str(path), _Handler)


class EventListener:
    """Serves ``path`` on a background thread until :meth:`close`."""

    def __init__(self, path: Path, on_event: Callable[[HookEvent], None]) -> None:
        self.path = path
        _clear_stale_socket(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only the user may send events.
        old_umask = os.umask(0o077)
        try:
            self._server = _Server(path, on_event)
        finally:
            os.umask(old_umask)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="event-listener", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self.path.unlink(missing_ok=True)


def is_listening(path: Path, timeout: float = 0.2) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def _clear_stale_socket(path: Path) -> None:
    """Remove a socket left behind by a daemon that died, refusing to steal a live one."""
    if not path.exists():
        return
    if is_listening(path):
        raise AlreadyRunningError(f"another daemon is already listening on {path}")
    path.unlink()
