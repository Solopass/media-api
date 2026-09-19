"""
Durable logging for the API.

The server runs under systemd socket activation inside WSL, and the WSL VM is
torn down whenever no Windows-side session holds it open — mid-request, without
warning. stdout goes to journald, which survives, but nothing lands on D: where
it can be read alongside the vault. This writes a rotating file as well, and
keeps a small in-memory ring of warnings and errors that /api/v1/diagnostics can
hand back for a quick look.
"""

import logging
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Deque, Dict, List

from app.config import settings

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
RING_SIZE = 200

# Warnings and errors from this process, newest last.
_RECENT: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)


class RingBufferHandler(logging.Handler):
    """Keeps recent problems queryable without reading the file off DrvFs."""

    def emit(self, record: logging.LogRecord):
        try:
            _RECENT.append({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "traceback": self.format(record).split("\n", 1)[1] if record.exc_info else None,
            })
        except Exception:
            pass


def log_file_path() -> Path:
    return settings.cache_dir / "logs" / "media-api.log"


def recent_events(limit: int = 50, level: str = "WARNING") -> List[Dict[str, Any]]:
    """Most recent buffered events, newest first."""
    wanted = {"WARNING": {"WARNING", "ERROR", "CRITICAL"}, "ERROR": {"ERROR", "CRITICAL"}}.get(
        level.upper(), {"WARNING", "ERROR", "CRITICAL"}
    )
    events = [e for e in _RECENT if e["level"] in wanted]
    return list(reversed(events))[:limit]


def setup_logging():
    """Attach the file and ring handlers to the root and uvicorn loggers."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)
    handlers: List[logging.Handler] = []

    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        handlers.append(file_handler)
    except Exception as e:  # never let logging setup break startup
        logging.getLogger("media_api").warning(f"[logging] File logging unavailable: {e}")

    ring = RingBufferHandler()
    ring.setFormatter(formatter)
    ring.setLevel(logging.WARNING)
    handlers.append(ring)

    # Replace our own handlers rather than skipping when one of the same type is
    # already attached: a stale handler would keep writing to the previous path.
    for handler in handlers:
        handler._media_api_handler = True

    targets = [root] + [
        # uvicorn keeps its own loggers with propagate=False, so request errors
        # and access lines would otherwise never reach the file.
        logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    for target in targets:
        for existing in list(target.handlers):
            if getattr(existing, "_media_api_handler", False):
                target.removeHandler(existing)
                try:
                    existing.close()
                except Exception:
                    pass
        for handler in handlers:
            target.addHandler(handler)

    return log_file_path()
