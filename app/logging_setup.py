from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, settings

LOG_FILE = LOG_DIR / "app.log"


class _Fmt(logging.Formatter):
    """Structured key=value lines: easy to read and to grep."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage().replace("\n", " | ")
        line = (
            f'time={self.formatTime(record, "%Y-%m-%dT%H:%M:%S")} level={record.levelname} '
            f'logger={record.name} msg="{msg}"'
        )
        if record.exc_info:
            line += ' exc="' + self.formatException(record.exc_info).replace("\n", " | ") + '"'
        return line


def setup() -> None:
    root = logging.getLogger()
    if getattr(root, "_ft_configured", False):
        return
    root.setLevel(settings.log_level)
    fmt = _Fmt()
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:  # read-only volume etc.: stdout logging still works
        pass
    root._ft_configured = True  # type: ignore[attr-defined]


def tail(lines: int = 300) -> list[str]:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-lines:]]
    except OSError:
        return []
