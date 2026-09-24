"""File logging. Hooks must stay silent, so errors from `fire` and the worker go here."""

import logging
from logging.handlers import RotatingFileHandler

from ambient import paths

LOGGER_NAME = "ambient"
_FORMAT = "%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s"


class _AmbientFileHandler(RotatingFileHandler):
    """Marker subclass, so :func:`setup` can replace its own handler without touching others."""


def setup(level: int = logging.INFO) -> logging.Logger:
    """Send the ``ambient`` loggers to the log file (replacing a previous :func:`setup`)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in [h for h in logger.handlers if isinstance(h, _AmbientFileHandler)]:
        logger.removeHandler(handler)
        handler.close()
    path = paths.log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _AmbientFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger
