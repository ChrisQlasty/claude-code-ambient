"""Add and remove ``ambient fire`` hooks in Claude Code's ``settings.json``.

Edits are merges: other hooks and settings are left untouched, and re-running is a no-op.
The file is backed up before every write.
"""

import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ambient.events import HANDLED_EVENTS

# Our hook is any command that runs an executable named `ambient` with the single arg `fire`,
# whatever its path or quoting, so a reinstall from another location replaces it.
_OUR_COMMAND = re.compile(r"""^(?:'[^']*/ambient'|"[^"]*/ambient"|(?:\S*/)?ambient) fire$""")


class HooksError(Exception):
    """settings.json can't be read or has an unexpected shape."""


@dataclass(frozen=True)
class Change:
    path: Path
    before: str | None
    after: str | None  # None: the file doesn't exist and stays that way

    @property
    def changed(self) -> bool:
        return self.before != self.after


def settings_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / "settings.json"


def ambient_executable() -> Path:
    """Absolute path of the ``ambient`` script, since hooks may not get the shell's PATH."""
    # Next to the running interpreter covers venvs, `uv run` and `uv tool install`.
    beside = Path(sys.executable).parent / "ambient"
    if beside.exists():
        return beside
    found = shutil.which("ambient")
    if found is None:
        raise HooksError("can't find the `ambient` executable; install it or put it on PATH")
    return Path(found).absolute()


def fire_command(executable: Path) -> str:
    return f"{shlex.quote(str(executable))} fire"


def is_ambient_command(command: object) -> bool:
    return isinstance(command, str) and bool(_OUR_COMMAND.match(command.strip()))


def plan_install(path: Path, command: str) -> Change:
    before, settings = _read(path)
    hooks = _hooks_section(settings) or settings.setdefault("hooks", {})
    modified = False
    for event in HANDLED_EVENTS:
        groups = _event_groups(hooks, event)
        ours = [h.get("command") for h in _entries(groups) if is_ambient_command(h.get("command"))]
        if ours == [command]:
            continue
        _remove_ours(hooks, event)
        _event_groups(hooks, event).append({"hooks": [{"type": "command", "command": command}]})
        modified = True
    # Leave an up-to-date file byte-for-byte alone, whatever its formatting.
    return Change(path, before, _dump(settings) if modified else before)


def plan_uninstall(path: Path) -> Change:
    before, settings = _read(path)
    hooks = _hooks_section(settings)
    if hooks is None:
        return Change(path, before, before)
    if not any(is_ambient_command(h.get("command")) for h in _all_entries(hooks)):
        return Change(path, before, before)
    for event in list(hooks):
        _remove_ours(hooks, event)
    if not hooks:
        del settings["hooks"]
    return Change(path, before, _dump(settings))


def apply(change: Change) -> Path | None:
    """Write ``change`` if it changes anything. Returns the backup path, if one was made."""
    if not change.changed or change.after is None:
        return None
    backup = None
    if change.path.exists():
        backup = _backup_path(change.path)
        shutil.copy2(change.path, backup)
    # Write through a symlink (e.g. settings.json kept in a dotfiles repo) rather than
    # replacing the link itself with a plain file.
    target = change.path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.ambient-tmp")
    tmp.write_text(change.after)
    os.replace(tmp, target)
    return backup


def _backup_path(path: Path) -> Path:
    stem = f"{path.name}.ambient-backup-{datetime.now():%Y%m%d-%H%M%S}"
    backup, n = path.with_name(stem), 1
    while backup.exists():
        backup, n = path.with_name(f"{stem}-{n}"), n + 1
    return backup


def _read(path: Path) -> tuple[str | None, dict[str, Any]]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise HooksError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HooksError(f"{path}: expected a JSON object")
    return text, data


def _dump(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _hooks_section(settings: dict[str, Any]) -> dict[str, Any] | None:
    if "hooks" not in settings:
        return None
    hooks = settings["hooks"]
    if not isinstance(hooks, dict):
        raise HooksError('"hooks" in settings.json is not an object')
    return hooks


def _event_groups(hooks: dict[str, Any], event: str) -> list[dict[str, Any]]:
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
        raise HooksError(f'"hooks.{event}" in settings.json is not a list of objects')
    return groups


def _entries(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        h
        for g in groups
        if isinstance(entries := g.get("hooks"), list)
        for h in entries
        if isinstance(h, dict)
    ]


def _all_entries(hooks: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        h
        for groups in hooks.values()
        if isinstance(groups, list)
        for h in _entries([g for g in groups if isinstance(g, dict)])
    ]


def _remove_ours(hooks: dict[str, Any], event: str) -> None:
    """Drop our commands from ``event``, then any groups and event lists that become empty."""
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return
    kept_groups = []
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            kept_groups.append(group)
            continue
        kept = [
            h for h in entries if not (isinstance(h, dict) and is_ambient_command(h.get("command")))
        ]
        if len(kept) == len(entries):
            kept_groups.append(group)
        elif kept:
            kept_groups.append({**group, "hooks": kept})
    if kept_groups:
        hooks[event] = kept_groups
    else:
        del hooks[event]
