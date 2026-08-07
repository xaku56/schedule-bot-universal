from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCHEDULE_URL = "https://disk.yandex.ru/d/tF4sAFicQhzBdA"
DEFAULT_REPLACEMENTS_URL = "https://disk.yandex.ru/d/F_GFm6_Qi9GYAQ"


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    token: str
    schedule_url: str
    replacements_url: str
    data_dir: Path
    timezone: str
    numerator_week_start: dt.date
    refresh_interval_minutes: int
    replacement_check_minutes: int
    autopost_time: dt.time
    autopost_enabled: bool
    worker_count: int
    expected_semester: str | None
    log_level: str = "INFO"
    telegram_messages_per_second: float = 20.0
    telegram_queue_size: int = 1000

    @classmethod
    def from_env(cls) -> Config:
        _load_env_file()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN")

        week_start_raw = os.getenv("NUMERATOR_WEEK_START", "").strip()
        if not week_start_raw:
            raise ValueError(
                "Set NUMERATOR_WEEK_START to a Monday of a numerator week (YYYY-MM-DD)"
            )
        numerator_week_start = dt.date.fromisoformat(week_start_raw)
        if numerator_week_start.weekday() != 0:
            raise ValueError("NUMERATOR_WEEK_START must be a Monday")
        autopost_time = dt.time.fromisoformat(
            os.getenv("AUTOPOST_TIME", "18:00").strip()
        )

        return cls(
            token=token,
            schedule_url=os.getenv("SCHEDULE_YANDEX_URL", DEFAULT_SCHEDULE_URL).strip(),
            replacements_url=os.getenv(
                "REPLACEMENTS_YANDEX_URL", DEFAULT_REPLACEMENTS_URL
            ).strip(),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            timezone=os.getenv("BOT_TIMEZONE", "Europe/Saratov").strip(),
            numerator_week_start=numerator_week_start,
            refresh_interval_minutes=max(
                int(os.getenv("SCHEDULE_REFRESH_MINUTES", "60")), 5
            ),
            replacement_check_minutes=max(
                int(os.getenv("REPLACEMENT_CHECK_MINUTES", "10")), 1
            ),
            autopost_time=autopost_time,
            autopost_enabled=_env_bool("AUTOPOST_ENABLED", True),
            worker_count=max(2, min(int(os.getenv("BOT_WORKERS", "4")), 16)),
            expected_semester=os.getenv("EXPECTED_SEMESTER", "").strip() or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            telegram_messages_per_second=max(
                1.0,
                min(float(os.getenv("TELEGRAM_MESSAGES_PER_SECOND", "20")), 25.0),
            ),
            telegram_queue_size=max(
                100, min(int(os.getenv("TELEGRAM_QUEUE_SIZE", "1000")), 10000)
            ),
        )
