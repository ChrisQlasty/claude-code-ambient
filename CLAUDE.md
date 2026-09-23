# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`claude-code-ambient` turns Claude Code hook events (`UserPromptSubmit`, `Notification`, `Stop`, ...) into
ambient light effects on physical devices (Nanoleaf Lines, NuPhy Air75 V3, Logitech G102, Philips Hue), and always
restores each device's previous state afterwards.

The full plan (architecture, phases, device notes) lives in `PRs/PLAN.md`. That directory is **gitignored and
private**, so it exists only on the author's machine. Read it when present and the task mentions a phase. Never
commit it, and never copy private planning notes into tracked files.

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

- `src/ambient/cli.py`: typer app (`ambient`).
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

- Python 3.12+, fully type-annotated (mypy strict), ruff with line length 100.
- Keep modules small and dependency-light. Add a dependency only when the plan calls for it or after asking.
- Comments explain *why*, not *what*. Match the density of surrounding code.
- Every new behaviour gets a test. Use the `console` driver or injected `sleep`/`clock` instead of real time or hardware.

## Git and pull requests

### Naming (Conventional Commits)

- **Branches:** `<type>/<short-kebab-slug>`, e.g. `feat/nanoleaf-driver`, `fix/restore-when-off`, `chore/ci`.
- **Commits:** `<type>(<optional scope>): <imperative summary>`, lowercase, no trailing period, ≤ 72 chars.
  Examples: `feat(config): add pydantic models`, `test(effects): cover pulse timeline`.
- **PR titles:** same format as commits, describing the whole change, e.g. `feat: project skeleton`.
- **Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, `ci`, `perf`. Use `!` for breaking changes
  (`feat(config)!: ...`).
- **Scopes:** `cli`, `config`, `effects`, `engine`, `hooks`, `daemon`, `web`, or a driver name (`nanoleaf`, `nuphy`, ...).

### Behaviour

- **Never commit to `main`.** Start each task on a new branch named as above.
- **Local commits on a feature branch are fine**, in small logical steps, each passing all checks. Don't amend or
  rewrite commits that have already been pushed.
- **Never push, open a PR, merge, or delete branches unless explicitly asked** in the current conversation.
  Approval for one PR does not carry over to the next.
- **Never force-push**, skip hooks (`--no-verify`), or change git config.
- Never commit `PRs/`, `CLAUDE.local.md`, secrets, device tokens, or personal config files.
- When asked to open a PR: use `gh pr create` against `main`. The body has a **Summary** (what and why),
  **Changes** (bullets), and **Testing** (commands run and manual checks, including hardware tested if any).
  Keep one phase or concern per PR.
- At the end of a task, report what was committed (branch and commit list) and what was left uncommitted.
