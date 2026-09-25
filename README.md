# claude-code-ambient

Turn [Claude Code](https://claude.com/claude-code) hook events into ambient light effects on your devices.
For example, your lights blink green when Claude finishes, then return to exactly what they showed before.

> **Status:** early development. The `console` test driver, the Nanoleaf and Logitech G102 drivers and the Claude
> Code hook integration (`UserPromptSubmit` and `Stop`) work. More devices (NuPhy Air75 V3, Philips Hue) and the
> `Notification` event are still to come.

## Requirements

- macOS
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Try it

```sh
uv sync
uv run ambient test console flash '#f00' --duration 2 --times 3
uv run ambient config validate --config examples/config.yaml
```

## Devices

### Nanoleaf (Lines and other panels)

The device must be on the same network as your Mac.

```sh
uv run ambient discover nanoleaf                 # lists devices found via mDNS
uv run ambient pair lines --driver nanoleaf      # then hold the power button 5-7 s until the lights flash
uv run ambient test lines flash '#ff0000' --duration 2 --times 3
```

`pair` finds the device (or use `--host`), then adds `host`, `port` and `token` under `devices.lines` in your config.
It backs up the previous file to `config.yaml.bak` and keeps its comments. After an effect, the lights return to
their previous scene or color, brightness, and on/off state. A dynamic scene started from another app (screen mirroring,
rhythm, external control) can't be selected again by name, so it comes back as its last solid color.

### Logitech G102 / G203 LIGHTSYNC

Connect the mouse with its USB cable. No pairing or extra macOS permission is needed, and it works while G HUB is
running.

```sh
uv run ambient discover logitech_g102
uv run ambient test logitech_g102 flash '#00ff00' --duration 2 --times 3
```

The mouse ignores lighting changes while it runs its onboard profile, so during an effect it is switched to software
control, the way G HUB and OpenRGB drive it. Nothing is saved to the mouse's memory. The mouse can't report its
current color, so afterwards:

- In onboard memory mode (the default without G HUB), it is switched back and the lighting and DPI step of the active
  onboard profile come back exactly.
- When G HUB controls the mouse, it is set to the `baseline` color from your config. Without a `baseline`, the effect is
  skipped with an error in the log.

```yaml
devices:
  mouse:
    driver: logitech_g102
    baseline: { color: "#ffffff", brightness: 60 }   # only used when G HUB controls the mouse
    transition: 0.25   # seconds to fade into each frame, like Nanoleaf's built-in fading; 0 = sharp
```

## Claude Code hooks

```sh
uv run ambient install-hooks --dry-run   # show the change to ~/.claude/settings.json
uv run ambient install-hooks             # add the hooks (backs the file up first)
uv run ambient simulate Stop             # play what a hook would, in the foreground
uv run ambient logs                      # errors from hooks end up here, never in Claude Code
uv run ambient uninstall-hooks
```

`install-hooks` adds `<absolute path>/ambient fire` for `UserPromptSubmit` and `Stop`, next to any hooks you already have,
and is safe to re-run. `ambient fire` reads the hook's JSON from stdin, starts a detached worker that plays the effects,
and exits within a few tens of milliseconds, silently and always with status 0.

Each device is locked while an effect plays, so overlapping events queue up (for up to 5 s, then they're dropped) instead of
capturing each other's flashes. The device's state before the effect is saved to `~/.cache/ambient/baseline/` until it has
been restored, so an interrupted effect is still undone by the next one.

## Configuration

`ambient` reads `~/.config/ambient/config.yaml`. Set `AMBIENT_CONFIG` or pass `--config` to use a different file.
See [`examples/config.yaml`](examples/config.yaml) for an example.

Each hook event maps to one or more effects on one or more devices:

| Effect  | Params                                      | Meaning                                         |
|---------|---------------------------------------------|-------------------------------------------------|
| `solid` | `color`, `brightness?`, `duration`          | Show a color for `duration`, then restore       |
| `flash` | `color`, `brightness?`, `duration`, `times` | Blink `times` within `duration`, then restore   |
| `pulse` | `color`, `brightness?`, `duration`, `times` | Fade in and out `times` times, then restore     |

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

## License

MIT
