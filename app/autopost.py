from __future__ import annotations

import datetime as dt
import hashlib
import json
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


def _legacy_group_fingerprint(replacements: list[dict[str, str]]) -> str:
    content = json.dumps(
        replacements, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "group:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _schedule_fingerprint(message: str) -> str:
    return "schedule:" + hashlib.sha256(message.encode("utf-8")).hexdigest()


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
        if target_date.weekday() == 6:
            return
        replacement_item = self.replacements.find_for_date(target_date)
        if replacement_item is None:
            return

        replacements_by_group = self.replacements.replacements_for_item(
            replacement_item
        )
        legacy_fingerprint = "replacement:" + file_fingerprint(replacement_item)
        for binding in bindings:
            self._send_binding(
                binding, target_date, replacements_by_group, legacy_fingerprint
            )

    def _send_binding(
        self,
        binding: Any,
        target_date: dt.date,
        replacements_by_group: dict[str, list[dict[str, str]]],
        legacy_fingerprint: str,
    ) -> None:
        chat_id = int(binding["chat_id"])
        thread_id = int(binding["thread_id"])
        group = str(binding["group_name"])
        try:
            base = self.schedules.schedule_for(
                group, target_date, self.config.numerator_week_start
            )
            group_replacements = replacements_by_group.get(group, [])
            schedule = apply_replacements(base, group_replacements)
            message = format_schedule(schedule)
            fingerprint = _schedule_fingerprint(message)
            old_group_fingerprint = _legacy_group_fingerprint(group_replacements)
            date_key = target_date.isoformat()
            previous = self.storage.autopost_fingerprint(
                chat_id, thread_id, group, date_key
            )
            if previous in {
                fingerprint,
                old_group_fingerprint,
                legacy_fingerprint,
            }:
                if previous != fingerprint:
                    self.storage.mark_autopost(
                        chat_id, thread_id, group, date_key, fingerprint
                    )
                self.storage.record_autopost_success(chat_id, thread_id)
                return
            self.sender.send_message(
                chat_id,
                message,
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
