"""Logging configuration — stderr + optional rotating file handler."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def configure_logging(level: str, log_file: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    # Don't double-register handlers on repeated calls (e.g. during tests).
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_h.setFormatter(formatter)
        root.addHandler(file_h)

    # yt-dlp and googleapiclient are chatty at INFO; keep them at WARNING unless we're debugging.
    if level != "DEBUG":
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("google.auth").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
