# claude-code-ambient

Turn [Claude Code](https://claude.com/claude-code) hook events into ambient light effects on your devices.
For example, your lights blink green when Claude finishes, then return to exactly what they showed before.

> **Status:** early development. The project skeleton and a `console` test driver work. Real devices (Nanoleaf Lines,
> NuPhy Air75 V3, Logitech G102, Philips Hue) and the Claude Code hook integration are still to come.

## Requirements

- macOS
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Try it

```sh
uv sync
uv run ambient test console flash '#f00' --duration 2 --times 3
uv run ambient config validate --config examples/config.yaml
```

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
