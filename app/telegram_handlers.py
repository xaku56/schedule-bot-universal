from __future__ import annotations

import datetime as dt
import html
import logging
import time
from collections.abc import Callable
from typing import Any

from .config import Config
from .replacement_service import ReplacementRepository
from .schedule_formatter import format_schedule
from .schedule_service import ScheduleRepository, parse_flexible_date
from .storage import Storage
from .telegram_api import TelegramAPI
from .telegram_queue import TelegramSendQueue

logger = logging.getLogger(__name__)


def _course_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "1 курс", "callback_data": "course:1"},
                {"text": "2 курс", "callback_data": "course:2"},
            ],
            [
                {"text": "3 курс", "callback_data": "course:3"},
                {"text": "4 курс", "callback_data": "course:4"},
            ],
            [{"text": "Обновить расписание", "callback_data": "refresh"}],
        ]
    }


def _groups_keyboard(groups: list[str]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for index in range(0, len(groups), 2):
        rows.append(
            [
                {"text": group.upper(), "callback_data": f"group:{group}"}
                for group in groups[index : index + 2]
            ]
        )
    rows.append([{"text": "← Курсы", "callback_data": "courses"}])
    return {"inline_keyboard": rows}


class TelegramHandlers:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        schedules: ScheduleRepository,
        replacements: ReplacementRepository,
        telegram: TelegramAPI,
        sender: TelegramSendQueue,
        timezone: dt.tzinfo,
        validate_semester: Callable[[], None],
        status_text: Callable[[], str],
        request_refresh: Callable[[int | None, int | None, bool], bool],
        claim_manual_refresh: Callable[[], bool],
    ) -> None:
        self.config = config
        self.storage = storage
        self.schedules = schedules
        self.replacements = replacements
        self.telegram = telegram
        self.sender = sender
        self.timezone = timezone
        self.validate_semester = validate_semester
        self.status_text = status_text
        self.request_refresh = request_refresh
        self.claim_manual_refresh = claim_manual_refresh
        self.admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    def can_manage(self, chat: dict[str, Any], user: dict[str, Any]) -> bool:
        chat_id = int(chat.get("id", 0))
        user_id = int(user.get("id", 0))
        if not chat_id or not user_id:
            return False
        if str(chat.get("type", "")) == "private":
            return chat_id == user_id
        key = (chat_id, user_id)
        now = time.time()
        cached = self.admin_cache.get(key)
        if cached and now - cached[1] < 300:
            return cached[0]
        member = self.telegram.get_chat_member(chat_id, user_id)
        allowed = str(member.get("status", "")) in {"administrator", "creator"}
        self.admin_cache[key] = (allowed, now)
        return allowed

    def _deny_management(self, chat_id: int, thread_id: int | None) -> None:
        self.sender.send_message(
            chat_id,
            "Менять группу, автоотправку и источники могут только администраторы чата.",
            thread_id,
        )

    def _binding_group(self, chat_id: int, thread_id: int | None) -> str | None:
        row = self.storage.get_binding(chat_id, thread_id)
        return str(row["group_name"]) if row else None

    def _send_setup(self, chat_id: int, thread_id: int | None) -> None:
        self.sender.send_message(
            chat_id,
            "Выбери курс, затем группу. В чате с темами настройку нужно выполнять прямо в нужной теме.",
            thread_id,
            _course_keyboard(),
        )

    def _schedule(
        self, group: str, target_date: dt.date
    ) -> tuple[dict[str, Any], str | None]:
        self.validate_semester()
        base = self.schedules.schedule_for(
            group, target_date, self.config.numerator_week_start
        )
        final, replacement_fingerprint = self.replacements.apply_for_date(
            base, target_date
        )
        note = None
        if replacement_fingerprint is None:
            note = "Файл замен на эту дату не найден — показано базовое расписание."
        return final, note

    def _send_date(
        self, chat_id: int, thread_id: int | None, group: str, target_date: dt.date
    ) -> None:
        schedule, note = self._schedule(group, target_date)
        self.sender.send_message(chat_id, format_schedule(schedule, note), thread_id)

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if "id" not in chat:
            return
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")
        text = str(message.get("text", "")).strip()
        if not text.startswith("/"):
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].casefold()

        if command in {"/help", "/start"}:
            self.sender.send_message(
                chat_id,
                "Команды:\n"
                "/setup — выбрать группу для этого чата или темы\n"
                "/today, /tomorrow — расписание\n"
                "/date DD.MM.YYYY — расписание на дату\n"
                "/week — текущая учебная неделя\n"
                "/status — состояние источников и кэша\n"
                "/autopost_on, /autopost_off — автоотправка\n"
                "/refresh — проверить обновления PDF",
                thread_id,
            )
            if command == "/help":
                return
        if command == "/status":
            self.sender.send_message(chat_id, self.status_text(), thread_id)
            return
        if command in {"/start", "/setup", "/group", "/groups"}:
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            self._send_setup(chat_id, thread_id)
            return
        if command == "/refresh":
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            if not self.claim_manual_refresh():
                self.sender.send_message(
                    chat_id,
                    "Повторную проверку можно запустить через 30 секунд.",
                    thread_id,
                )
                return
            force = argument.strip().casefold() == "force"
            if self.request_refresh(chat_id, thread_id, force):
                self.sender.send_message(chat_id, "Проверяю PDF в фоне…", thread_id)
            return

        group = self._binding_group(chat_id, thread_id)
        if not group:
            self._send_setup(chat_id, thread_id)
            return

        now = dt.datetime.now(self.timezone)
        if command == "/today":
            self._send_date(chat_id, thread_id, group, now.date())
        elif command == "/tomorrow":
            self._send_date(
                chat_id, thread_id, group, now.date() + dt.timedelta(days=1)
            )
        elif command == "/date":
            self._send_date(chat_id, thread_id, group, parse_flexible_date(argument))
        elif command == "/week":
            monday = now.date() - dt.timedelta(days=now.weekday())
            for day_offset in range(5):
                self._send_date(
                    chat_id,
                    thread_id,
                    group,
                    monday + dt.timedelta(days=day_offset),
                )
        elif command == "/autopost_on":
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            self.storage.set_autopost(chat_id, thread_id, True)
            self.sender.send_message(
                chat_id,
                f"Автоотправка включена для <b>{html.escape(group)}</b> в этой теме.",
                thread_id,
            )
        elif command == "/autopost_off":
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            self.storage.set_autopost(chat_id, thread_id, False)
            self.sender.send_message(chat_id, "Автоотправка выключена.", thread_id)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        if callback_id:
            self.telegram.answer_callback(callback_id)
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = int(chat["id"])
        user = callback.get("from") or {}
        thread_id = message.get("message_thread_id")
        data = str(callback.get("data", ""))

        if not self.can_manage(chat, user):
            self._deny_management(chat_id, thread_id)
            return

        if data in {"courses", "refresh"}:
            if data == "refresh":
                if not self.claim_manual_refresh():
                    self.sender.send_message(
                        chat_id,
                        "Повторную проверку можно запустить через 30 секунд.",
                        thread_id,
                    )
                elif self.request_refresh(chat_id, thread_id, False):
                    self.sender.send_message(chat_id, "Проверяю PDF в фоне…", thread_id)
            else:
                self._send_setup(chat_id, thread_id)
            return
        if data.startswith("course:"):
            try:
                course = int(data.split(":", 1)[1])
            except ValueError:
                course = 0
            if course not in {1, 2, 3, 4}:
                logger.warning("Rejected invalid course callback: %r", data[:80])
                self.sender.send_message(
                    chat_id, "Некорректный номер курса.", thread_id
                )
                return
            groups = self.schedules.groups(course)
            self.sender.send_message(
                chat_id,
                f"Выбери группу {course} курса:",
                thread_id,
                _groups_keyboard(groups),
            )
            return
        if data.startswith("group:"):
            group = data.split(":", 1)[1]
            if group not in self.schedules.groups():
                self.sender.send_message(
                    chat_id, "Этой группы уже нет в актуальном PDF.", thread_id
                )
                return
            self.storage.set_binding(chat_id, thread_id, group)
            self.sender.send_message(
                chat_id,
                f"Группа <b>{html.escape(group.upper())}</b> привязана к этой теме.\n"
                "Проверь: /today или /tomorrow\n"
                "Автоотправка: /autopost_on",
                thread_id,
            )

    def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("message"):
            self.handle_message(update["message"])
        elif update.get("callback_query"):
            self.handle_callback(update["callback_query"])
