from __future__ import annotations

import datetime as dt
import html
import http.client
import logging
import re
import threading
import time
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .autopost import AutopostService
from .calendar_service import CalendarService
from .command_guard import CommandGuard
from .config import Config
from .logging_config import diagnostic_context, log_diagnostic, safe_traceback
from .replacement_service import ReplacementRepository
from .schedule_service import ScheduleRepository
from .storage import Storage
from .telegram_api import TelegramAPI
from .telegram_handlers import TelegramHandlers
from .telegram_queue import TelegramSendQueue

logger = logging.getLogger(__name__)


def _public_error(error: Exception) -> str:
    if isinstance(error, (ValueError, KeyError)):
        return str(error).strip("'")[:500]
    return "Внутренняя ошибка. Подробности записаны в журнал бота."


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.data_dir)
        self.schedules = ScheduleRepository(self.storage, config.schedule_url)
        self.replacements = ReplacementRepository(config.replacements_url)
        self.telegram = TelegramAPI(config.token)
        self.sender = TelegramSendQueue(
            self.telegram,
            messages_per_second=config.telegram_messages_per_second,
            max_size=config.telegram_queue_size,
        )
        try:
            self.timezone = ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Timezone %s is unavailable; using fallback", config.timezone
            )
            self.timezone = (
                dt.timezone(dt.timedelta(hours=4), name="UTC+04:00")
                if config.timezone == "Europe/Saratov"
                else dt.timezone.utc
            )
        self.offset: int | None = None
        self.last_schedule_refresh = 0.0
        self.last_autopost_check = 0.0
        self.command_guard = CommandGuard()
        self.executor = ThreadPoolExecutor(
            max_workers=config.worker_count,
            thread_name_prefix="schedule-bot",
        )
        self._task_slots = threading.BoundedSemaphore(config.worker_count * 8)
        self._background_lock = threading.Lock()
        self._refresh_future: Future[Any] | None = None
        self._autopost_future: Future[Any] | None = None
        self._closed = False
        self.handlers = TelegramHandlers(
            config=config,
            storage=self.storage,
            schedules=self.schedules,
            replacements=self.replacements,
            telegram=self.telegram,
            sender=self.sender,
            timezone=self.timezone,
            validate_semester=self._validate_semester_config,
        )
        self.autopost = AutopostService(
            config=config,
            storage=self.storage,
            schedules=self.schedules,
            replacements=self.replacements,
            sender=self.sender,
            validate_semester=self._validate_semester_config,
        )
        self.calendar = CalendarService(
            config,
            self.storage,
            self.schedules,
            self.replacements,
            self.timezone,
            self._validate_semester_config,
        )

    def initialize(self) -> None:
        try:
            changed, message = self.schedules.refresh(
                force=not self.schedules.has_cache,
                expected_semester=self._expected_semester_key(),
            )
            logger.info("Schedule refresh changed=%s: %s", changed, message)
        except Exception:
            if not self.schedules.has_cache:
                raise
            logger.exception(
                "Schedule refresh failed; starting with the last valid cache"
            )
        self._validate_semester_config()
        removed = self.storage.cleanup_autopost_history()
        if removed:
            logger.info("Removed %s old autopost records", removed)
        self.last_schedule_refresh = time.time()

    def _expected_semester_key(self) -> str:
        if self.config.expected_semester:
            return self.config.expected_semester
        week_start = self.config.numerator_week_start
        if week_start.month >= 8:
            return f"{week_start.year}-{week_start.year + 1}:semester-1"
        return f"{week_start.year - 1}-{week_start.year}:semester-2"

    def _validate_semester_config(self) -> None:
        semester = str(self.schedules.cache.get("semester_key") or "")
        expected_semester = self._expected_semester_key()
        if semester != expected_semester:
            raise ValueError(
                f"Expected semester {expected_semester}, but PDF contains {semester or 'unknown'}"
            )
        match = re.fullmatch(r"(20\d{2})-(20\d{2}):semester-([12])", semester)
        if not match:
            raise ValueError("Cannot validate semester from PDF filenames")
        start_year, end_year, semester_number = map(int, match.groups())
        week_start = self.config.numerator_week_start
        valid = (
            semester_number == 1
            and week_start.year == start_year
            and 8 <= week_start.month <= 12
        ) or (
            semester_number == 2
            and week_start.year == end_year
            and 1 <= week_start.month <= 7
        )
        if not valid:
            raise ValueError(
                f"NUMERATOR_WEEK_START={week_start} does not match PDF semester {semester}"
            )

    def _refresh_task(self) -> None:
        try:
            changed, status = self.schedules.refresh(
                expected_semester=self._expected_semester_key(),
            )
            self._validate_semester_config()
            logger.info("Background refresh changed=%s: %s", changed, status)
        except Exception:
            logger.exception("Background schedule refresh failed")

    def _request_refresh(self) -> bool:
        with self._background_lock:
            if self._refresh_future and not self._refresh_future.done():
                return False
            self._refresh_future = self.executor.submit(self._refresh_task)
            return True

    def _handle_update_safely(
        self, update: dict[str, Any], received_at: float | None = None
    ) -> None:
        started = time.perf_counter()
        update_id = update.get("update_id")
        context_token = diagnostic_context.set(
            {"update_id": update_id if isinstance(update_id, int) else None}
        )
        status = "ok"
        log_diagnostic(
            "update_handler_started",
            queue_wait_ms=round((started - received_at) * 1000, 1)
            if received_at is not None
            else None,
        )
        try:
            self.handlers.handle_update(update)
        except Exception as error:  # noqa: BLE001 - Do not log user-controlled exception text.
            status = "failed"
            logger.error(
                "Telegram update failed update_id=%s error_type=%s traceback=%s",
                (diagnostic_context.get() or {}).get("update_id"),
                type(error).__name__,
                safe_traceback(error.__traceback__),
            )
            message = (
                update.get("message")
                or (update.get("callback_query") or {}).get("message")
                or {}
            )
            chat = message.get("chat") or {}
            if chat.get("id"):
                try:
                    self.sender.send_message(
                        int(chat["id"]),
                        f"Не удалось выполнить команду: {html.escape(_public_error(error))}",
                        message.get("message_thread_id"),
                    )
                except Exception as send_error:  # noqa: BLE001 - Keep polling alive without payload logs.
                    logger.error(
                        "Failed to send update error message error_type=%s traceback=%s",
                        type(send_error).__name__,
                        safe_traceback(send_error.__traceback__),
                    )
        finally:
            log_diagnostic(
                "update_handler_finished",
                status=status,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            diagnostic_context.reset(context_token)

    def _submit_update(self, update: dict[str, Any]) -> None:
        received_at = time.perf_counter()
        update_id = update.get("update_id")
        metadata = {"update_id": update_id if isinstance(update_id, int) else None}
        log_diagnostic(
            "update_received",
            **metadata,
            kind="callback" if update.get("callback_query") else "message",
        )
        key = self.command_guard.admit(update, time.monotonic())
        if key is None:
            log_diagnostic("update_dropped", **metadata, reason="guard")
            return
        if not self._task_slots.acquire(blocking=False):
            log_diagnostic("update_dropped", **metadata, reason="workers_full")
            self.command_guard.finish(key)
            return

        def finished(_future: Future[Any]) -> None:
            self.command_guard.finish(key)
            self._task_slots.release()

        try:
            future = self.executor.submit(
                self._handle_update_safely, update, received_at
            )
        except Exception:
            self.command_guard.finish(key)
            self._task_slots.release()
            raise
        future.add_done_callback(finished)

    def _maybe_refresh_schedule(self, now_ts: float) -> None:
        interval = self.config.refresh_interval_minutes * 60
        if now_ts - self.last_schedule_refresh < interval:
            return
        if self._request_refresh():
            self.last_schedule_refresh = now_ts

    def _autopost_task_safely(self, now: dt.datetime) -> None:
        try:
            self.autopost.run(now)
        except Exception:
            logger.exception("Autopost background task failed")

    def _maybe_autopost(self, now: dt.datetime, now_ts: float) -> None:
        if not self.config.autopost_enabled:
            return
        interval = self.config.replacement_check_minutes * 60
        if now_ts - self.last_autopost_check < interval:
            return
        with self._background_lock:
            if self._autopost_future and not self._autopost_future.done():
                return
            self.last_autopost_check = now_ts
            self._autopost_future = self.executor.submit(
                self._autopost_task_safely, now
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info("Stopping bot")
        try:
            self.calendar.close()
        finally:
            try:
                self.executor.shutdown(wait=True, cancel_futures=False)
            finally:
                self.sender.close()

    def run(self) -> None:
        network_failures = 0
        try:
            self.initialize()
            logger.info("Bot started groups=%s", len(self.schedules.groups()))
            self.calendar.start()
            while True:
                retry_delay = 0
                try:
                    for update in self.telegram.updates(self.offset):
                        self.offset = int(update["update_id"]) + 1
                        self._submit_update(update)
                    network_failures = 0
                except (
                    urllib.error.URLError,
                    OSError,
                    http.client.HTTPException,
                ) as error:
                    network_failures = min(network_failures + 1, 4)
                    retry_delay = min(2 ** (network_failures - 1), 5)
                    logger.warning(
                        "Polling network error_type=%s; retrying in %s seconds traceback=%s",
                        type(error).__name__,
                        retry_delay,
                        safe_traceback(error.__traceback__),
                    )
                except Exception as error:  # noqa: BLE001 - Network metadata is logged by TelegramAPI.
                    logger.error(
                        "Runtime iteration failed error_type=%s; retrying in 5 seconds traceback=%s",
                        type(error).__name__,
                        safe_traceback(error.__traceback__),
                    )
                    retry_delay = 5

                # Polling failures must not postpone refreshes or automatic posts.
                try:
                    now_ts = time.time()
                    now = dt.datetime.now(self.timezone)
                    self._maybe_refresh_schedule(now_ts)
                    self._maybe_autopost(now, now_ts)
                except Exception:
                    logger.exception(
                        "Background scheduling failed; retrying in 5 seconds"
                    )
                    retry_delay = max(retry_delay, 5)
                if retry_delay:
                    time.sleep(retry_delay)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.close()
