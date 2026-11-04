from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import time

from robbi.configuration.settings import FULL_LOG_FILE, LOG_DIR, TRACE_LOG_FILE, settings


def setup_logging() -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    datefmt = "%d.%m.%Y - %H:%M:%S"
    file_path = LOG_DIR / "robot.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    file_handler = RotatingFileHandler(file_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)

    full_handler = RotatingFileHandler(FULL_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    full_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(full_handler)

    trace_logger = logging.getLogger("robot.trace")
    trace_logger.handlers.clear()
    trace_logger.setLevel(logging.INFO)
    trace_logger.propagate = False

    trace_handler = RotatingFileHandler(TRACE_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    trace_handler.setFormatter(logging.Formatter("%(message)s"))
    trace_logger.addHandler(trace_handler)


def trace_event(event: str, **payload) -> None:
    def compact(value, depth=0):
        if depth > 5:
            return "[nested data omitted]"
        if isinstance(value, dict):
            return {str(key): ("[redacted]" if any(secret in str(key).lower() for secret in ("key", "token", "secret", "authorization", "password")) else compact(item, depth + 1)) for key, item in list(value.items())[:32]}
        if isinstance(value, (list, tuple)):
            return [compact(item, depth + 1) for item in value[:16]]
        if isinstance(value, str):
            return value[:2000]
        if isinstance(value, bytes):
            return f"[{len(value)} bytes]"
        return value
    record = {
        "ts": int(time.time() * 1000),
        "event": event,
        **compact(payload),
    }
    logging.getLogger("robot.trace").info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
