"""Console-script entry point.

``ambient fire`` runs on every Claude Code hook, so it bypasses the typer CLI (and with it
pydantic and the drivers), which would cost more than the whole latency budget to import.
"""

import sys


def main() -> None:
    if sys.argv[1:] == ["fire"]:
        from ambient.fire import main as fire_main

        fire_main()
        return

    from ambient.cli import app

    app()
