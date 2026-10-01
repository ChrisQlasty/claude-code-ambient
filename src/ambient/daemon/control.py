"""Start, stop and inspect the daemon from the CLI, and its optional launchd login item."""

import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ambient import paths
from ambient.daemon.listener import is_listening

DEFAULT_PORT = 8765
LAUNCHD_LABEL = "io.github.claude-code-ambient.daemon"
START_TIMEOUT = 10.0
# Long enough for queued effects to finish and restore their devices on shutdown.
STOP_TIMEOUT = 35.0
_POLL = 0.1

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DaemonError(Exception):
    pass


@dataclass(frozen=True)
class DaemonInfo:
    pid: int
    url: str
    socket_ok: bool


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running() -> DaemonInfo | None:
    """The running daemon, from its state file, or ``None``."""
    try:
        state = json.loads(paths.daemon_state_file().read_text())
        pid, url = int(state["pid"]), str(state["url"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _pid_alive(pid):
        return None
    return DaemonInfo(pid, url, is_listening(paths.daemon_socket()))


def daemon_command(port: int) -> list[str]:
    # -P keeps the cwd off sys.path, as for the direct-mode worker.
    return [sys.executable, "-P", "-m", "ambient.daemon", "--port", str(port)]


def start(
    port: int,
    *,
    spawn: Callable[[list[str]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = START_TIMEOUT,
) -> DaemonInfo:
    """Start the daemon in the background (unless it runs already) and wait until it's up."""
    info = running()
    if info:
        return info
    (spawn or _spawn_detached)(daemon_command(port))
    deadline = clock() + timeout
    while clock() < deadline:
        info = running()
        if info and info.socket_ok:
            return info
        sleep(_POLL)
    raise DaemonError(f"the daemon didn't start; see {paths.log_file()}")


def _spawn_detached(args: list[str]) -> None:
    log_path = paths.log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Crashes before logging is set up (e.g. an import error) still land in the log file.
    with log_path.open("a") as out:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
            close_fds=True,
        )


def stop(
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = STOP_TIMEOUT,
) -> bool:
    """Stop the running daemon. False if none was running."""
    info = running()
    if info is None:
        return False
    os.kill(info.pid, signal.SIGTERM)
    deadline = clock() + timeout
    while clock() < deadline:
        if not _pid_alive(info.pid):
            return True
        sleep(_POLL)
    raise DaemonError(f"the daemon (pid {info.pid}) didn't stop within {timeout:.0f} s")


def launch_agent_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"


def launch_agent_plist(executable: Path, port: int) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(executable), "daemon", "run", "--port", str(port)],
            # Start at login only: `ambient daemon stop` should stay stopped, not respawn.
            "RunAtLoad": True,
            "ProcessType": "Background",
        }
    )


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def install_login_item(
    executable: Path, port: int, *, path: Path | None = None, runner: Runner = _run
) -> Path:
    """Write the LaunchAgent and load it, which starts the daemon now and at every login."""
    if sys.platform != "darwin" and runner is _run:
        raise DaemonError("login items use launchd, which is macOS only")
    path = path or launch_agent_path()
    domain = f"gui/{os.getuid()}"
    # Reload so a changed port or executable takes effect.
    runner(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launch_agent_plist(executable, port))
    result = runner(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise DaemonError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return path


def uninstall_login_item(*, path: Path | None = None, runner: Runner = _run) -> bool:
    """Unload and remove the LaunchAgent (stopping the daemon it started). False if absent."""
    path = path or launch_agent_path()
    if not path.exists():
        return False
    runner(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    path.unlink()
    return True
