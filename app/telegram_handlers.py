from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
import time
from collections.abc import Callable
from typing import Any

from .command_guard import COMMANDS
from .config import Config
from .replacement_service import ReplacementRepository
from .schedule_formatter import (
    format_schedule,
    format_teacher_schedule,
    format_teacher_weekday_schedule,
    format_weekday_schedule,
)
from .schedule_service import ScheduleRepository, parse_flexible_date
from .storage import Storage
from .teacher_schedule import available_teachers, schedule_for_teacher, teacher_key
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
            "Менять группу или преподавателя и автоотправку могут только администраторы чата.",
            thread_id,
        )

    def _binding_group(self, chat_id: int, thread_id: int | None) -> str | None:
        row = self.storage.get_binding(chat_id, thread_id)
        return (
            str(row["target_name"]) if row and row["target_type"] == "group" else None
        )

    def _send_setup(self, chat_id: int, thread_id: int | None) -> None:
        self.sender.send_message(
            chat_id,
            "Выбери группу или преподавателя. В чате с темами настройку нужно выполнять прямо в нужной теме.",
            thread_id,
            {
                "inline_keyboard": [
                    [
                        {"text": "Группа", "callback_data": "setup:groups"},
                        {"text": "Преподаватель", "callback_data": "setup:teacher"},
                    ]
                ]
            },
        )

    def _send_teacher_search(
        self, chat_id: int, thread_id: int | None, query: str
    ) -> None:
        if not query:
            self.sender.send_message(
                chat_id,
                "Напиши /teacher Фамилия, например /teacher Иванова.",
                thread_id,
            )
            return
        needle = teacher_key(query)
        matches = [
            name
            for name in available_teachers(self.schedules)
            if needle in teacher_key(name)
        ]
        if not matches:
            self.sender.send_message(
                chat_id, "Преподаватель не найден в актуальном PDF.", thread_id
            )
            return
        if len(matches) > 20:
            self.sender.send_message(
                chat_id,
                "Найдено слишком много преподавателей. Уточни фамилию или инициалы.",
                thread_id,
            )
            return
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": name,
                        "callback_data": "teacher:"
                        + hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16],
                    }
                ]
                for name in matches
            ]
        }
        self.sender.send_message(chat_id, "Выбери преподавателя:", thread_id, keyboard)

    def _schedule(
        self,
        name: str,
        target_date: dt.date,
        *,
        target_type: str = "group",
        include_replacements: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        self.validate_semester()
        if target_type == "teacher":
            item = (
                self.replacements.find_for_date(target_date)
                if include_replacements
                else None
            )
            by_group = self.replacements.replacements_for_item(item) if item else None
            schedule = schedule_for_teacher(
                self.schedules,
                name,
                target_date,
                self.config.numerator_week_start,
                by_group,
            )
            note = (
                "Файл замен на эту дату не найден — показано базовое расписание."
                if include_replacements and not item
                else None
            )
            return schedule, note
        base = self.schedules.schedule_for(
            name, target_date, self.config.numerator_week_start
        )
        if not include_replacements:
            return base, None
        final, replacement_fingerprint = self.replacements.apply_for_date(
            base, target_date
        )
        note = None
        if replacement_fingerprint is None:
            note = "Файл замен на эту дату не найден — показано базовое расписание."
        return final, note

    def _send_date(
        self,
        chat_id: int,
        thread_id: int | None,
        name: str,
        target_date: dt.date,
        *,
        target_type: str = "group",
        include_replacements: bool = True,
    ) -> None:
        schedule, note = self._schedule(
            name,
            target_date,
            target_type=target_type,
            include_replacements=include_replacements,
        )
        message = (
            format_teacher_schedule(schedule, note)
            if target_type == "teacher"
            else format_schedule(schedule, note)
        )
        self.sender.send_message(chat_id, message, thread_id)

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
        if command not in COMMANDS:
            return

        if command in {"/help", "/start"}:
            self.sender.send_message(
                chat_id,
                "<b>Команды расписания</b>\n"
                "/setup — выбрать группу или преподавателя для чата или темы.\n"
                "/teacher Фамилия — найти и выбрать преподавателя.\n"
                "/today — расписание на сегодня с опубликованными заменами.\n"
                "/tomorrow — расписание на завтра с опубликованными заменами.\n"
                "/date DD.MM.YYYY — расписание на дату, например /date 16.09.2026.\n"
                "/week — основное расписание без замен: оба варианта недели, "
                "по сообщению на день. Ч — числитель, З — знаменатель; "
                "суббота показывается при наличии пар.\n\n"
                "<b>Настройки и состояние</b>\n"
                "/autopost_on — присылать расписание на завтра после появления "
                "файла замен, даже если для выбранной группы или преподавателя замен нет. "
                "Повторно — только при изменении итогового расписания.\n"
                "/autopost_off — отключить автоотправку.\n"
                "/status — сведения о загруженном расписании и кэше.\n"
                "/help — эта справка.\n\n"
                "Настройки действуют в текущем чате или теме. В групповых чатах "
                "менять их могут администраторы. Расписание обновляется автоматически.\n"
                "Частые повторы команд пропускаются. Дождитесь завершения ответа "
                "перед повторным запросом.",
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
        if command == "/teacher":
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            self._send_teacher_search(chat_id, thread_id, argument)
            return

        binding = self.storage.get_binding(chat_id, thread_id)
        if not binding:
            self._send_setup(chat_id, thread_id)
            return
        name = str(binding["target_name"])
        target_type = str(binding["target_type"])

        now = dt.datetime.now(self.timezone)
        if command == "/today":
            self._send_date(
                chat_id, thread_id, name, now.date(), target_type=target_type
            )
        elif command == "/tomorrow":
            self._send_date(
                chat_id,
                thread_id,
                name,
                now.date() + dt.timedelta(days=1),
                target_type=target_type,
            )
        elif command == "/date":
            self._send_date(
                chat_id,
                thread_id,
                name,
                parse_flexible_date(argument),
                target_type=target_type,
            )
        elif command == "/week":
            monday = now.date() - dt.timedelta(days=now.weekday())
            self.validate_semester()
            for day_offset in range(6):
                first, _ = self._schedule(
                    name,
                    monday + dt.timedelta(days=day_offset),
                    target_type=target_type,
                    include_replacements=False,
                )
                second, _ = self._schedule(
                    name,
                    monday + dt.timedelta(days=day_offset + 7),
                    target_type=target_type,
                    include_replacements=False,
                )
                schedules = {first["week_type"]: first, second["week_type"]: second}
                numerator_pairs = schedules["числитель"]["pairs"]
                denominator_pairs = schedules["знаменатель"]["pairs"]
                if day_offset == 5 and not (numerator_pairs or denominator_pairs):
                    continue
                self.sender.send_message(
                    chat_id,
                    (
                        format_teacher_weekday_schedule
                        if target_type == "teacher"
                        else format_weekday_schedule
                    )(name, first["weekday"], numerator_pairs, denominator_pairs),
                    thread_id,
                )
        elif command == "/autopost_on":
            if not self.can_manage(chat, user):
                self._deny_management(chat_id, thread_id)
                return
            self.storage.set_autopost(chat_id, thread_id, True)
            self.sender.send_message(
                chat_id,
                f"Автоотправка включена для <b>{html.escape(name)}</b> в этой теме.",
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
        if callback.get("data") == "refresh":
            return
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

        if data == "setup:teacher":
            self._send_teacher_search(chat_id, thread_id, "")
            return
        if data == "setup:groups":
            self.sender.send_message(
                chat_id, "Выбери курс, затем группу:", thread_id, _course_keyboard()
            )
            return
        if data == "courses":
            self.sender.send_message(
                chat_id, "Выбери курс, затем группу:", thread_id, _course_keyboard()
            )
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
            return
        if data.startswith("teacher:"):
            token = data.split(":", 1)[1]
            teacher = next(
                (
                    name
                    for name in available_teachers(self.schedules)
                    if hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16]
                    == token
                ),
                None,
            )
            if teacher is None:
                self.sender.send_message(
                    chat_id, "Преподаватель не найден в актуальном PDF.", thread_id
                )
                return
            self.storage.set_binding(chat_id, thread_id, teacher, "teacher")
            self.sender.send_message(
                chat_id,
                f"Преподаватель <b>{html.escape(teacher)}</b> привязан к этой теме.\n"
                "Проверь: /today или /tomorrow\nАвтоотправка: /autopost_on",
                thread_id,
            )

    def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("message"):
            self.handle_message(update["message"])
        elif update.get("callback_query"):
            self.handle_callback(update["callback_query"])
