from __future__ import annotations

import json
import logging
import re
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def safe_traceback(tb: TracebackType | None) -> str:
    """Serialize stack locations without reading source, locals or exception text."""
    return json.dumps(
        [
            {
                "file": frame.f_code.co_filename,
                "function": frame.f_code.co_name,
                "line": line,
            }
            for frame, line in traceback.walk_tb(tb)
        ],
        ensure_ascii=False,
    )


class RedactingFormatter(logging.Formatter):
    def formatException(
        self,
        ei: tuple[
            type[BaseException] | None, BaseException | None, TracebackType | None
        ],
    ) -> str:
        return f"{type(ei[1]).__name__} traceback={safe_traceback(ei[2])}"

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            # Another handler may have cached an unsafe exception rendering.
            record.exc_text = self.formatException(record.exc_info)
        return TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", super().format(record))


def configure_logging(data_dir: Path, level: str = "INFO") -> None:
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
