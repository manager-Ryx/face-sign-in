"""Bounded, rotating log files shared by the service and the tray app."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3


def setup_logging(path: Path, *, level: int = logging.INFO, console: bool = True) -> None:
    """Send log records to ``path`` (rotated at 2 MB, 3 backups kept)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                            encoding="utf-8", delay=True)
    ]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    # Third-party libraries are chatty at INFO; their warnings still surface.
    for noisy in ("tensorflow", "absl", "h5py", "PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
