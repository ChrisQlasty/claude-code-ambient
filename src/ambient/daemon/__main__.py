"""``python -m ambient.daemon``: what ``ambient ui`` spawns in the background."""

import argparse
import logging
import os
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


def run(argv: list[str]) -> None:
    code = main(argv)
    # Request threads aren't daemon threads, so one still pairing (for up to minutes) would keep
    # the process alive after uvicorn gave up on it. Devices are restored by now and config writes
    # are atomic, so nothing is lost by not waiting for them.
    logging.shutdown()
    os._exit(code)


if __name__ == "__main__":
    run(sys.argv[1:])
