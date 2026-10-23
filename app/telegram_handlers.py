from __future__ import annotations

import datetime as dt
import hashlib
import html
import http.client
import logging
from collections.abc import Callable
from typing import Any

from .command_guard import COMMANDS, MOVED_COMMANDS, parse_command
from .config import Config
from .logging_config import safe_traceback
from .replacement_service import ReplacementRepository
from .schedule_calculator import schedule_for_target
from .schedule_formatter import (
    format_schedule,
    format_teacher_schedule,
    format_teacher_weekday_schedule,
    format_weekday_schedule,
)
from .schedule_service import ScheduleRepository, ScheduleSnapshot, parse_flexible_date
from .storage import Storage
from .teacher_schedule import (
    available_teachers,
    teacher_key,
    teacher_names,
)
from .telegram_api import TelegramAPI, TelegramAPIError
from .telegram_menu import TelegramMenu
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
    ) -> None:
        self.config = config
        self.storage = storage
        self.schedules = schedules
        self.replacements = replacements
        self.telegram = telegram
        self.sender = sender
        self.timezone = timezone
        self.validate_semester = validate_semester
        self.menu = TelegramMenu(self)

    def _teachers(self) -> list[str]:
        names = {teacher_key(name): name for name in available_teachers(self.schedules)}
        try:
            for value in self.replacements.recent_teachers():
                for name in teacher_names(value):
                    names.setdefault(teacher_key(name), name)
        except Exception:
            logger.exception("Could not list replacement teachers")
        return sorted(names.values(), key=lambda name: name.casefold())

    def _send_teacher_page(
        self, chat_id: int, thread_id: int | None, message_id: int | None, page: int
    ) -> None:
        teachers = self._teachers()
        page_size = 12
        page_count = (len(teachers) + page_size - 1) // page_size
        if not 0 <= page < page_count:
            self.sender.send_message(
                chat_id, "Список преподавателей недоступен.", thread_id
            )
            return
        rows = [
            [
                {
                    "text": name,
                    "callback_data": "teacher:"
                    + hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16],
                }
            ]
            for name in teachers[page * page_size : (page + 1) * page_size]
        ]
        navigation = []
        if page > 0:
            navigation.append(
                {"text": "← Назад", "callback_data": f"teachers:{page - 1}"}
            )
        if page + 1 < page_count:
            navigation.append(
                {"text": "Далее →", "callback_data": f"teachers:{page + 1}"}
            )
        if navigation:
            rows.append(navigation)
        self._update_setup_message(
            chat_id,
            thread_id,
            message_id,
            f"Выбери преподавателя · страница {page + 1}/{page_count}:",
            {"inline_keyboard": rows},
        )

    def _update_setup_message(
        self,
        chat_id: int,
        thread_id: int | None,
        message_id: int | None,
        text: str,
        keyboard: dict[str, Any] | None = None,
    ) -> None:
        if message_id is None:
            self.sender.send_message(chat_id, text, thread_id, keyboard)
        else:
            self.sender.edit_message(chat_id, message_id, text, keyboard)

    def _schedule(
        self,
        name: str,
        target_date: dt.date,
        *,
        target_type: str = "group",
        include_replacements: bool = True,
        snapshot: ScheduleSnapshot | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        self.validate_semester()
        schedules = snapshot if snapshot is not None else self.schedules
        item = (
            self.replacements.find_for_date(target_date)
            if include_replacements
            else None
        )
        by_group = self.replacements.replacements_for_item(item) if item else None
        schedule = schedule_for_target(
            schedules,
            target_type,
            name,
            target_date,
            self.config.numerator_week_start,
            by_group,
        )
        note = (
            "Файл замен на эту дату не найден — показано базовое расписание."
            if include_replacements and item is None
            else None
        )
        return schedule, note

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
        self.sender.send_message(
            chat_id,
            self._date_text(
                name,
                target_date,
                target_type=target_type,
                include_replacements=include_replacements,
            ),
            thread_id,
        )

    def _date_text(
        self,
        name: str,
        target_date: dt.date,
        *,
        target_type: str = "group",
        include_replacements: bool = True,
    ) -> str:
        schedule, note = self._schedule(
            name,
            target_date,
            target_type=target_type,
            include_replacements=include_replacements,
        )
        return (
            format_teacher_schedule(schedule, note)
            if target_type == "teacher"
            else format_schedule(schedule, note)
        )

    def _send_help(self, chat_id: int, thread_id: int | None) -> None:
        self.sender.send_message(
            chat_id,
            "<b>Команды расписания</b>\n"
            "/menu — главное меню.\n"
            "/setup — выбрать группу или преподавателя для чата или темы.\n"
            "/today — расписание на сегодня с опубликованными заменами.\n"
            "/tomorrow — расписание на завтра с опубликованными заменами.\n"
            "/date DD.MM.YYYY — расписание на дату, например /date 16.09.2026.\n"
            "Кнопка «Неделя» — основное расписание без замен: оба варианта недели, "
            "по сообщению на день. Ч — числитель, З — знаменатель; "
            "суббота показывается при наличии пар.\n\n"
            "<b>Главное меню</b>\n"
            "«Другое расписание» — разовый выбор группы или преподавателя и даты "
            "с заменами, без изменения основных настроек.\n"
            "«Автоотправка» — присылать расписание на завтра после появления "
            "файла замен, даже если для выбранной группы или преподавателя замен нет. "
            "Повторно — только при изменении итогового расписания.\n"
            "«Календарь» — личная ссылка, выбор полного или короткого формата "
            "и отзыв своих ссылок этого чата/темы. "
            "В коротком формате — только фамилия, без описания; место проведения сохраняется.\n"
            "/help — эта справка.\n\n"
            "Выбор расписания и автоотправка в личном чате — только для вас. "
            "В группах настройки общие для чата или темы. "
            "Менять их может любой участник. Формат календаря индивидуальный "
            "для всех ваших личных подписок. "
            "Расписание обновляется автоматически.\n"
            "Частые повторы команд пропускаются. Дождитесь завершения ответа "
            "перед повторным запросом.",
            thread_id,
        )

    def _set_calendar_format(
        self, user_id: int, argument: str, chat_id: int, thread_id: int | None
    ) -> None:
        title_format = argument.strip().casefold()
        if title_format not in {"short", "full"}:
            raise ValueError("Неизвестный формат календаря")
        self.storage.set_calendar_format(user_id, title_format)
        label = (
            "только фамилия, без описания"
            if title_format == "short"
            else "длинные заголовки"
        )
        self.sender.send_message(
            chat_id,
            f"Ваш формат календаря: {label}. "
            "Других пользователей это не затрагивает. "
            "Подключать вашу подписку заново не нужно: изменения появятся "
            "при обновлении календаря. Для старой общей ссылки получите "
            "личную через меню «Календарь» → «Получить ссылку».",
            thread_id,
        )

    def _send_calendar(self, user_id: int, chat_id: int, thread_id: int | None) -> None:
        if not self.config.calendar_public_url:
            self.sender.send_message(
                chat_id,
                "Календарь пока не настроен на сервере. Нужен CALENDAR_PUBLIC_URL с HTTPS.",
                thread_id,
            )
            return
        subscription = self.storage.calendar_subscription(chat_id, thread_id, user_id)
        if subscription is None:
            self.sender.send_message(
                chat_id,
                "Для подписки сначала нужен основной выбор расписания: /setup.",
                thread_id,
            )
            return
        name = str(subscription["target_name"])
        label = (
            "Преподаватель" if subscription["target_type"] == "teacher" else "Группа"
        )
        url = f"{self.config.calendar_public_url}/calendar/{subscription['token']}.ics"
        self.sender.send_message(
            chat_id,
            f"<b>{label}: {html.escape(name)}</b>\n"
            "В этом календаре только выбранная группа или преподаватель.\n"
            "Пары на завтра публикуются после обнаружения файла замен. "
            "При изменении ваших пар календарь обновляется без дублей.\n"
            "Это ваша личная ссылка. Формат заголовков меняется в меню «Календарь».\n\n"
            "iPhone: Календарь → Календари → Добавить календарь → "
            "Добавить подписной календарь. Вставьте этот адрес:\n"
            f"<code>{html.escape(url)}</code>\n\n"
            "Google Calendar: в веб-версии Другие календари → Добавить по URL. "
            "Используйте подписку, а не разовый импорт файла.\n"
            "До первой публикации календарь может быть пустым. "
            "Скорость обновления зависит от приложения.\n"
            "Ссылка закреплена за этим выбором. После смены /setup новая ссылка "
            "доступна в меню «Календарь» → «Получить ссылку». "
            "Старую подписку можно удалить в телефоне. "
            "Ссылка даёт доступ к расписанию — передавайте её только намеренно.",
            thread_id,
        )

    def _send_week(
        self,
        chat_id: int,
        thread_id: int | None,
        name: str,
        target_type: str,
        now: dt.datetime,
    ) -> None:
        monday = now.date() - dt.timedelta(days=now.weekday())
        self.validate_semester()
        snapshot = self.schedules.snapshot()
        for day_offset in range(6):
            first, _ = self._schedule(
                name,
                monday + dt.timedelta(days=day_offset),
                target_type=target_type,
                include_replacements=False,
                snapshot=snapshot,
            )
            second, _ = self._schedule(
                name,
                monday + dt.timedelta(days=day_offset + 7),
                target_type=target_type,
                include_replacements=False,
                snapshot=snapshot,
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

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")
        command, argument = parse_command(str(message.get("text", "")))
        if self.menu.is_date_reply(message):
            self.menu.handle_reply(message)
            return
        if command in MOVED_COMMANDS:
            self.menu.open(message, notice="Эта функция перенесена на кнопки меню.")
            return
        if command not in COMMANDS:
            return
        if command == "/help":
            self._send_help(chat_id, thread_id)
            return
        if command in {"/start", "/menu", "/setup", "/group", "/groups"}:
            self.menu.open(message, setup=command in {"/setup", "/group", "/groups"})
            return
        binding = self.storage.get_binding(chat_id, thread_id)
        if binding is None:
            self.menu.open(message, setup=True)
            return
        name = str(binding["target_name"])
        target_type = str(binding["target_type"])
        now = dt.datetime.now(self.timezone)
        if command in {"/today", "/tomorrow", "/date"}:
            date = (
                parse_flexible_date(argument)
                if command == "/date"
                else now.date() + dt.timedelta(days=command == "/tomorrow")
            )
            self._send_date(chat_id, thread_id, name, date, target_type=target_type)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        if callback_id:
            try:
                self.telegram.answer_callback(callback_id)
            except (TelegramAPIError, OSError, http.client.HTTPException) as error:
                # Acknowledgement only stops Telegram's button spinner.
                logger.warning(
                    "Could not acknowledge setup callback; continuing setup error_type=%s traceback=%s",
                    type(error).__name__,
                    safe_traceback(error.__traceback__),
                )
        if callback.get("data") == "refresh":
            return
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        if str(callback.get("data", "")).startswith("m:"):
            self.menu.handle_callback(callback)
            return
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")
        data = str(callback.get("data", ""))

        if data == "setup:teacher":
            self._send_teacher_page(chat_id, thread_id, message_id, 0)
            return
        if data.startswith("teachers:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = -1
            self._send_teacher_page(chat_id, thread_id, message_id, page)
            return
        if data in {"setup:groups", "courses"}:
            self._update_setup_message(
                chat_id,
                thread_id,
                message_id,
                "Выбери курс, затем группу:",
                _course_keyboard(),
            )
            return
        if data.startswith("course:"):
            try:
                course = int(data.split(":", 1)[1])
            except ValueError:
                course = 0
            if course not in {1, 2, 3, 4}:
                logger.warning("Rejected invalid course callback")
                self.sender.send_message(
                    chat_id, "Некорректный номер курса.", thread_id
                )
                return
            groups = self.schedules.groups(course)
            self._update_setup_message(
                chat_id,
                thread_id,
                message_id,
                f"Выбери группу {course} курса:",
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
                "Автоотправка и календарь: /menu",
                thread_id,
            )
            return
        if data.startswith("teacher:"):
            token = data.split(":", 1)[1]
            teacher = next(
                (
                    name
                    for name in self._teachers()
                    if hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16]
                    == token
                ),
                None,
            )
            if teacher is None:
                self.sender.send_message(
                    chat_id, "Преподавателя нет в актуальном списке.", thread_id
                )
                return
            self.storage.set_binding(chat_id, thread_id, teacher, "teacher")
            self.sender.send_message(
                chat_id,
                f"Преподаватель <b>{html.escape(teacher)}</b> привязан к этой теме.\n"
                "Проверь: /today или /tomorrow\nАвтоотправка и календарь: /menu",
                thread_id,
            )

    def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("message"):
            self.handle_message(update["message"])
        elif update.get("callback_query"):
            self.handle_callback(update["callback_query"])
