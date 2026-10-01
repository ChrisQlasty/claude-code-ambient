"""``python -m ambient.daemon``: what ``ambient daemon start`` spawns in the background."""

import argparse
import logging
import sys

from ambient.daemon.control import DEFAULT_PORT
from ambient.daemon.listener import AlreadyRunningError
from ambient.daemon.server import DEFAULT_HOST, serve


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ambient.daemon")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        serve(host=args.host, port=args.port)
    except AlreadyRunningError as exc:
        logging.getLogger("ambient").error("daemon: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
