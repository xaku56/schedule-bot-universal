from __future__ import annotations

import logging
import re
import sys
import time
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
diagnostic_context: ContextVar[dict[str, int | None] | None] = ContextVar(
    "diagnostic_context", default=None
)
diagnostic_logger = logging.getLogger("app.diagnostics")


def log_diagnostic(event: str, **fields: object) -> None:
    """Only pass explicit metadata here, never payloads, URLs or exception text."""
    if diagnostic_logger.isEnabledFor(logging.INFO):
        metadata = {**(diagnostic_context.get() or {}), **fields}
        diagnostic_logger.info(
            "%s %s",
            event,
            " ".join(f"{key}={value}" for key, value in metadata.items()),
        )


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", super().format(record))


def configure_logging(
    data_dir: Path, level: str = "INFO", *, diagnostics_enabled: bool = True
) -> None:
    numeric_level = getattr(logging, level.upper(), None)
    if numeric_level is None:
        raise ValueError(f"Unsupported LOG_LEVEL: {level}")
    if not isinstance(numeric_level, int):
        raise TypeError(f"LOG_LEVEL resolved to an invalid type: {level}")

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(
        "%(asctime)sZ %(levelname)s %(name)s [%(threadName)s] %(message)s"
    )
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(
        RotatingFileHandler(
            log_dir / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    )
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    for handler in root.handlers:
        handler.close()
    root.handlers.clear()
    root.setLevel(numeric_level)
    for handler in handlers:
        root.addHandler(handler)
    diagnostic_logger.disabled = not diagnostics_enabled
