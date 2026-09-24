"""Detached direct-mode executor, spawned by `ambient fire` as `python -m ambient.worker Stop`."""

import sys

from ambient import log, paths
from ambient.config import ConfigError, load_config
from ambient.engine import run_event
from ambient.events import HANDLED_EVENTS, is_handled


def main(argv: list[str]) -> int:
    logger = log.setup()
    try:
        event = argv[0] if len(argv) == 1 else None
        if not is_handled(event):
            logger.error("worker: expected one of %s, got %r", ", ".join(HANDLED_EVENTS), argv)
            return 2
        path = paths.config_file()
        if not path.exists():
            logger.info("%s: no config at %s, nothing to do", event, path)
            return 0
        try:
            config = load_config(path)
        except ConfigError as exc:
            logger.error("%s: invalid config %s: %s", event, path, exc)
            return 1
        run_event(config, event)
        return 0
    except Exception:
        logger.exception("worker failed")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
