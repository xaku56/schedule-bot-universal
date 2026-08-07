from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from .config import Config
from .replacement_service import ReplacementRepository, apply_replacements
from .schedule_formatter import format_schedule
from .schedule_service import ScheduleRepository
from .storage import Storage
from .telegram_queue import TelegramSendQueue
from .yandex_disk import file_fingerprint

logger = logging.getLogger(__name__)


class AutopostService:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        schedules: ScheduleRepository,
        replacements: ReplacementRepository,
        sender: TelegramSendQueue,
        validate_semester: Callable[[], None],
    ) -> None:
        self.config = config
        self.storage = storage
        self.schedules = schedules
        self.replacements = replacements
        self.sender = sender
        self.validate_semester = validate_semester

    def run(self, now: dt.datetime) -> None:
        self.validate_semester()
        bindings = self.storage.autopost_bindings()
        if not bindings:
            return

        target_date = now.date() + dt.timedelta(days=1)
        if target_date.weekday() >= 5:
            return
        replacement_item = self.replacements.find_for_date(target_date)
        if replacement_item is None and now.time() < self.config.autopost_time:
            return

        replacements_by_group = (
            self.replacements.replacements_for_item(replacement_item)
            if replacement_item
            else {}
        )
        for binding in bindings:
            self._send_binding(
                binding, target_date, replacement_item, replacements_by_group
            )

    def _send_binding(
        self,
        binding: Any,
        target_date: dt.date,
        replacement_item: dict[str, object] | None,
        replacements_by_group: dict[str, list[dict[str, str]]],
    ) -> None:
        chat_id = int(binding["chat_id"])
        thread_id = int(binding["thread_id"])
        group = str(binding["group_name"])
        try:
            base = self.schedules.schedule_for(
                group, target_date, self.config.numerator_week_start
            )
            if replacement_item:
                schedule = apply_replacements(
                    base, replacements_by_group.get(group, [])
                )
                fingerprint = "replacement:" + file_fingerprint(replacement_item)
                note = "Автоотправка: опубликованы замены на завтра."
            else:
                schedule = base
                fingerprint = "base:" + str(self.schedules.cache["source_fingerprint"])
                note = (
                    "Автоотправка: замены пока не опубликованы, "
                    "показано базовое расписание."
                )
            date_key = target_date.isoformat()
            if (
                self.storage.autopost_fingerprint(chat_id, thread_id, group, date_key)
                == fingerprint
            ):
                self.storage.record_autopost_success(chat_id, thread_id)
                return
            self.sender.send_message(
                chat_id,
                format_schedule(schedule, note),
                thread_id or None,
                priority=10,
            )
            self.storage.mark_autopost(chat_id, thread_id, group, date_key, fingerprint)
            self.storage.record_autopost_success(chat_id, thread_id)
            logger.info(
                "Autopost sent chat_id=%s thread_id=%s group=%s date=%s",
                chat_id,
                thread_id,
                group,
                date_key,
            )
        except Exception as error:
            disabled = self.storage.record_autopost_failure(
                chat_id, thread_id, str(error)
            )
            logger.exception(
                "Autopost failed chat_id=%s thread_id=%s group=%s disabled=%s",
                chat_id,
                thread_id,
                group,
                disabled,
            )
