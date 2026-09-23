# claude-code-ambient

Turn [Claude Code](https://claude.com/claude-code) hook events into ambient light effects on your devices.
For example, your lights blink green when Claude finishes, then return to exactly what they showed before.

> **Status:** early development. The `console` test driver and the Nanoleaf driver work. More devices (NuPhy Air75 V3,
> Logitech G102, Philips Hue) and the Claude Code hook integration are still to come.

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
