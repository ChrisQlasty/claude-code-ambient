## Project

`claude-code-ambient` turns Claude Code hook events (`UserPromptSubmit`, `Notification`, `Stop`, ...) into
ambient light effects on physical devices (eg. Nanoleaf Lines, NuPhy Air75 V3, Logitech G102, Philips Hue), and always
restores each device's previous state afterwards.

## Commands

```sh
uv sync                                   # install (uses the managed Python pinned in .python-version)
uv run ambient --help
uv run ambient test console flash '#f00'  # try an effect without hardware
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict
```

All four checks (pytest, ruff check, ruff format, mypy) must pass before any commit.

## Layout

- `src/ambient/main.py`: console-script entry point. Routes `ambient fire` to `fire.py` without importing the typer app.
- `src/ambient/cli.py`: typer app (`ambient`).
- `src/ambient/fire.py`: hook entrypoint (stdlib-only). Reads stdin JSON and spawns `worker.py` detached.
- `src/ambient/worker.py`, `src/ambient/engine.py`: load config, then per device lock → snapshot or persisted
  baseline → play → restore.
- `src/ambient/hooks_install.py`: merge/remove `ambient fire` hooks in Claude Code's `settings.json`.
- `src/ambient/events.py`: hook events handled (`UserPromptSubmit`, `Stop`). `log.py`: file logging.
- `src/ambient/config.py`: pydantic models for `~/.config/ambient/config.yaml`, YAML loading, and JSON Schema export.
- `src/ambient/effects.py`: effect models (`solid`/`flash`/`pulse`) and the frame/timeline generator.
- `src/ambient/drivers/`: `base.py` (the `Driver` ABC and `Capability`) and one module per device. Drivers are
  registered through the `ambient.drivers` entry point group in `pyproject.toml`.
- `src/ambient/paths.py`: XDG-style paths on every OS, overridable with `XDG_*_HOME` and `AMBIENT_CONFIG`.
- `tests/`: pytest. Tests must never touch real devices, the real config, or `~/.claude`.
- `spikes/`: throwaway hardware exploration scripts. Not imported by the package.

## Hard rules

- `ambient fire` (the hook entrypoint) must return in well under 100 ms, print nothing to stdout, and always exit 0.
  Log errors to a file instead.
- Effects always restore the device's previous state (snapshot, then play, then restore), including when playback fails.
- No hardcoded personal values (IPs, tokens, device names, paths). The repo is public.
- Never write to `~/.claude/settings.json` or the user's real config outside an explicit `install-hooks`/`pair`-style
  command, and always back the file up first.
- Hardware-writing code must not persist changes to device memory or EEPROM (for example, no VIA `id_custom_save`).

## Code style

- Python 3.12+, fully type-annotated (mypy strict).
- Fix any lint errors obtained from PostToolUse hook instead of working around them.
- Keep modules small and dependency-light. Add a dependency only when the plan calls for it or after asking.
- Comments explain *why*, not *what*. Match the density of surrounding code.
- Every new behaviour gets a test. Use the `console` driver or injected `sleep`/`clock` instead of real time or hardware.
