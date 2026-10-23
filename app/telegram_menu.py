from __future__ import annotations

import datetime as dt
import hashlib
import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .schedule_service import parse_flexible_date
from .teacher_schedule import teacher_key

if TYPE_CHECKING:
    from .telegram_handlers import TelegramHandlers

SESSION_SECONDS = 15 * 60
DATE_PROMPT = "Введите дату расписания в формате ДД.ММ.ГГГГ."


@dataclass
class MenuSession:
    token: str
    user_id: int
    chat_id: int
    thread_id: int | None
    expires_at: float
    message_id: int | None = None
    prompt_id: int | None = None
    mode: str = "saved"
    target_type: str = "group"
    target_name: str = ""
    actions: set[str] = field(default_factory=set)
    rendered: tuple[str, list[list[tuple[str, str]]]] | None = None
    lock: Any = field(default_factory=threading.Lock)


class TelegramMenu:
    """Button navigation and short-lived choices; permanent settings stay in Storage."""

    def __init__(self, handlers: TelegramHandlers) -> None:
        self.handlers = handlers
        self._sessions: dict[str, MenuSession] = {}
        self._lock = threading.Lock()

    def open(
        self, message: dict[str, Any], *, setup: bool = False, notice: str = ""
    ) -> None:
        user = message.get("from") or {}
        chat_id = int(message["chat"]["id"])
        thread_id = message.get("message_thread_id")
        user_id = int(user.get("id", 0))
        if user_id <= 0 or user.get("is_bot") or message.get("sender_chat"):
            self.handlers.sender.send_message(
                chat_id,
                "Откройте меню от своего имени, не анонимно и не от имени канала.",
                thread_id,
            )
            return
        now = time.monotonic()
        session = MenuSession(
            secrets.token_hex(6), user_id, chat_id, thread_id, now + SESSION_SECONDS
        )
        with self._lock:
            self._sessions = {
                key: value
                for key, value in self._sessions.items()
                if value.expires_at > now
            }
            if len(self._sessions) >= 1000:
                del self._sessions[next(iter(self._sessions))]
            self._sessions[session.token] = session
        with session.lock:
            if setup:
                session.mode = "setup"
                self._selection(session)
            else:
                self._home(session, notice)

    def _render(
        self, session: MenuSession, text: str, rows: list[list[tuple[str, str]]]
    ) -> None:
        if session.message_id is not None and session.rendered == (text, rows):
            return
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": label, "callback_data": f"m:{session.token}:{action}"}
                    for label, action in row
                ]
                for row in rows
            ]
        }
        if session.message_id is None:
            response = self.handlers.sender.send_message(
                session.chat_id, text, session.thread_id, keyboard
            )
            if isinstance(response, dict):
                session.message_id = response["message_id"]
        else:
            self.handlers.sender.edit_message(
                session.chat_id, session.message_id, text, keyboard
            )
        session.actions = {action for row in rows for _, action in row}
        session.rendered = (text, rows)

    def _home(self, session: MenuSession, notice: str = "") -> None:
        session.prompt_id = None
        binding = self.handlers.storage.get_binding(session.chat_id, session.thread_id)
        text = "<b>Главное меню</b>\n"
        if binding is None:
            text += "Расписание не выбрано.\n"
        else:
            label = "Преподаватель" if binding["target_type"] == "teacher" else "Группа"
            text += f"{label}: <b>{html.escape(binding['target_name'])}</b>\n"
            text += "Автоотправка: " + (
                "включена" if binding["autopost"] else "выключена"
            )
            text += "\n"
        if session.chat_id < 0:
            text += "Выбор и автоотправка общие для этого чата или темы.\n"
        if notice:
            text += "\n" + notice
        self._render(
            session,
            text,
            [
                [("Сегодня", "today"), ("Завтра", "tomorrow")],
                [("На дату", "date"), ("Неделя", "week")],
                [("Другое расписание", "view")],
                [("Календарь", "calendar"), ("Автоотправка", "autopost")],
                [("Изменить выбор", "setup"), ("Справка", "help")],
            ],
        )

    def _selection(self, session: MenuSession) -> None:
        session.prompt_id = None
        text = (
            "<b>Разовый просмотр</b>\nОсновной выбор, календарь и автоотправка не меняются."
            if session.mode == "view"
            else "<b>Основное расписание</b>\nВыбор сохранится для этого чата или темы."
        )
        self._render(
            session,
            "Выбери группу или преподавателя.\n" + text,
            [
                [("Группа", "groups"), ("Преподаватель", "teachers:0")],
                [("Главное меню", "home")],
            ],
        )

    def _courses(self, session: MenuSession) -> None:
        self._render(
            session,
            "Выбор курса:",
            [
                [(f"{course} курс", f"course:{course}") for course in (1, 2)],
                [(f"{course} курс", f"course:{course}") for course in (3, 4)],
                [("← Назад", "choose"), ("Главное меню", "home")],
            ],
        )

    def _groups(self, session: MenuSession, course: int) -> None:
        groups = self.handlers.schedules.groups(course)
        rows = [
            [(group.upper(), f"group:{group}") for group in groups[index : index + 2]]
            for index in range(0, len(groups), 2)
        ]
        rows.append([("← Курсы", "groups"), ("Главное меню", "home")])
        self._render(session, f"Выбор группы {course} курса:", rows)

    def _teachers(self, session: MenuSession, page: int) -> None:
        teachers = self.handlers._teachers()
        pages = (len(teachers) + 11) // 12
        if not 0 <= page < pages:
            self._selection(session)
            return
        rows = [
            [
                (
                    name,
                    "teacher:"
                    + hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16],
                )
            ]
            for name in teachers[page * 12 : (page + 1) * 12]
        ]
        navigation = []
        if page:
            navigation.append(("← Назад", f"teachers:{page - 1}"))
        if page + 1 < pages:
            navigation.append(("Далее →", f"teachers:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("Выбор типа", "choose"), ("Главное меню", "home")])
        self._render(
            session, f"Выбор преподавателя · страница {page + 1}/{pages}:", rows
        )

    def _choose_target(self, session: MenuSession, kind: str, name: str) -> None:
        session.target_type, session.target_name = kind, name
        if session.mode == "setup":
            self.handlers.storage.set_binding(
                session.chat_id, session.thread_id, name, kind
            )
            self._home(
                session,
                "Основное расписание сохранено. Существующие календарные ссылки не изменены.",
            )
        else:
            self._dates(session)

    def _capture_binding(self, session: MenuSession) -> dict[str, Any] | None:
        binding = self.handlers.storage.get_binding(session.chat_id, session.thread_id)
        if binding is None:
            self._home(
                session,
                "Для этого действия сначала нужен основной выбор. Кнопка «Другое расписание» работает без него.",
            )
            return None
        session.mode = "saved"
        session.target_type = binding["target_type"]
        session.target_name = binding["target_name"]
        return dict(binding)

    def _dates(self, session: MenuSession) -> None:
        session.prompt_id = None
        label = "Разовый просмотр" if session.mode == "view" else "Расписание"
        self._render(
            session,
            f"<b>{label}: {html.escape(session.target_name)}</b>\nВыбор даты:",
            [
                [("Сегодня", "day:today"), ("Завтра", "day:tomorrow")],
                [("Другая дата", "day:custom")],
                [("Другой выбор", "view"), ("Главное меню", "home")],
            ],
        )

    def _prompt_date(self, session: MenuSession, error: str = "") -> None:
        session.prompt_id = None
        self._render(
            session,
            "Ожидается ответ на сообщение с запросом даты.",
            [
                [("← Выбор даты", "dates"), ("Главное меню", "home")],
            ],
        )
        text = f'<a href="tg://user?id={session.user_id}">{DATE_PROMPT}</a>'
        text += "\n" + html.escape(session.target_name)
        if error:
            text += "\n" + error
        response = self.handlers.sender.send_message(
            session.chat_id,
            text,
            session.thread_id,
            {
                "force_reply": True,
                "selective": True,
                "input_field_placeholder": "ДД.ММ.ГГГГ",
            },
        )
        session.prompt_id = (
            response["message_id"] if isinstance(response, dict) else None
        )

    @staticmethod
    def is_date_reply(message: dict[str, Any]) -> bool:
        reply = message.get("reply_to_message") or {}
        text = str(message.get("text", ""))
        return (
            bool((reply.get("from") or {}).get("is_bot"))
            and str(reply.get("text", "")).startswith(DATE_PROMPT)
            and bool(text)
            and not text.startswith("/")
            and len(text) <= 32
        )

    def handle_reply(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        thread_id = message.get("message_thread_id")
        user_id = (message.get("from") or {}).get("id")
        prompt_id = message["reply_to_message"]["message_id"]
        with self._lock:
            session = next(
                (
                    s
                    for s in self._sessions.values()
                    if (s.chat_id, s.thread_id, s.user_id, s.prompt_id)
                    == (chat_id, thread_id, user_id, prompt_id)
                ),
                None,
            )
        if session is None:
            self._unavailable(chat_id, thread_id)
            return
        with session.lock:
            if session.expires_at <= time.monotonic() or session.prompt_id != prompt_id:
                self._unavailable(chat_id, thread_id)
                return
            session.expires_at = time.monotonic() + SESSION_SECONDS
            try:
                date = parse_flexible_date(str(message["text"]))
            except ValueError:
                self._prompt_date(session, "Некорректная дата. Пример: 26.10.2026.")
                return
            self._show_date(session, date, new_message=True)

    def _show_date(
        self,
        session: MenuSession,
        date: dt.date,
        *,
        new_message: bool = False,
    ) -> None:
        text = self.handlers._date_text(
            session.target_name, date, target_type=session.target_type
        )
        if session.mode == "view":
            text = "<b>Разовый просмотр</b>\n" + text
        if new_message:
            session.message_id = None
        self._render(
            session,
            text,
            [
                [("Другая дата", "dates"), ("Другой выбор", "view")],
                [("Главное меню", "home")],
            ],
        )
        session.prompt_id = None

    def _calendar(self, session: MenuSession) -> None:
        self._render(
            session,
            "<b>Личный календарь</b>\n"
            "Формат действует на все ваши личные подписки. "
            "Отзыв затрагивает только ваши ссылки этого чата или темы.",
            [
                [("Получить ссылку", "calendar:link")],
                [
                    ("Полный формат", "calendar:full"),
                    ("Короткий формат", "calendar:short"),
                ],
                [("Отозвать ссылки", "calendar:revoke")],
                [("Главное меню", "home")],
            ],
        )

    def _autopost(self, session: MenuSession) -> None:
        binding = self._capture_binding(session)
        if binding is None:
            return
        enabled = bool(binding["autopost"])
        self._render(
            session,
            "<b>Автоотправка</b>\n"
            f"Расписание: {html.escape(session.target_name)}\n"
            + ("Включена" if enabled else "Выключена")
            + " для этого чата или темы.\n"
            "Расписание на завтра отправляется после появления файла замен. "
            "Повторно — только при изменении выбранного расписания.",
            [
                [
                    (
                        "Выключить" if enabled else "Включить",
                        "autopost:off" if enabled else "autopost:on",
                    )
                ],
                [("Главное меню", "home")],
            ],
        )

    def _unavailable(self, chat_id: int, thread_id: int | None) -> None:
        self.handlers.sender.send_message(
            chat_id,
            "Меню устарело или принадлежит другому пользователю. Откройте своё: /menu.",
            thread_id,
        )

    def handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        chat_id = int(message["chat"]["id"])
        thread_id = message.get("message_thread_id")
        parts = str(callback.get("data", "")).split(":", 2)
        with self._lock:
            session = self._sessions.get(parts[1]) if len(parts) == 3 else None
        if session is None:
            self._unavailable(chat_id, thread_id)
            return
        with session.lock:
            if (
                session.user_id != (callback.get("from") or {}).get("id")
                or (session.chat_id, session.thread_id) != (chat_id, thread_id)
                or session.expires_at <= time.monotonic()
                or (
                    session.message_id is not None
                    and session.message_id != message.get("message_id")
                )
                or parts[2] not in session.actions
            ):
                self._unavailable(chat_id, thread_id)
                return
            session.message_id = message.get("message_id")
            session.expires_at = time.monotonic() + SESSION_SECONDS
            self._act(session, parts[2])

    def _act(self, session: MenuSession, action: str) -> None:
        h = self.handlers
        if action == "home":
            self._home(session)
        elif action in {"setup", "view", "choose"}:
            if action != "choose":
                session.mode = action
            self._selection(session)
        elif action == "groups":
            self._courses(session)
        elif action.startswith("course:"):
            self._groups(session, int(action.split(":")[1]))
        elif action.startswith("teachers:"):
            self._teachers(session, int(action.split(":")[1]))
        elif action.startswith("group:"):
            name = action.split(":", 1)[1]
            if name not in h.schedules.groups():
                self._selection(session)
                return
            self._choose_target(session, "group", name)
        elif action.startswith("teacher:"):
            token = action.split(":")[1]
            name = next(
                (
                    name
                    for name in h._teachers()
                    if hashlib.sha256(teacher_key(name).encode()).hexdigest()[:16]
                    == token
                ),
                None,
            )
            if name is None:
                self._selection(session)
                return
            self._choose_target(session, "teacher", name)
        elif action in {"today", "tomorrow", "date", "week"}:
            if not self._capture_binding(session):
                return
            if action == "date":
                self._dates(session)
            elif action == "week":
                h._send_week(
                    session.chat_id,
                    session.thread_id,
                    session.target_name,
                    session.target_type,
                    dt.datetime.now(h.timezone),
                )
                session.message_id = None
                self._home(session)
            else:
                self._show_date(
                    session,
                    dt.datetime.now(h.timezone).date()
                    + dt.timedelta(days=action == "tomorrow"),
                )
        elif action == "dates":
            self._dates(session)
        elif action == "day:custom":
            self._prompt_date(session)
        elif action in {"day:today", "day:tomorrow"}:
            self._show_date(
                session,
                dt.datetime.now(h.timezone).date()
                + dt.timedelta(days=action == "day:tomorrow"),
            )
        elif action == "calendar":
            self._calendar(session)
        elif action == "calendar:link":
            h._send_calendar(session.user_id, session.chat_id, session.thread_id)
        elif action in {"calendar:short", "calendar:full"}:
            h._set_calendar_format(
                session.user_id,
                action.split(":")[1],
                session.chat_id,
                session.thread_id,
            )
        elif action == "calendar:revoke":
            self._render(
                session,
                "Отозвать ваши личные ссылки этого чата или темы? "
                "После этого подписки потребуется подключить заново.",
                [
                    [("Да, отозвать", "calendar:confirm")],
                    [("Отмена", "calendar")],
                ],
            )
        elif action == "calendar:confirm":
            h.storage.revoke_calendars(
                session.chat_id, session.thread_id, session.user_id
            )
            self._home(
                session,
                "Личные ссылки этого чата или темы отозваны. "
                "Старые подписки можно удалить в приложении календаря.",
            )
        elif action == "autopost":
            self._autopost(session)
        elif action in {"autopost:on", "autopost:off"}:
            h.storage.set_autopost(
                session.chat_id,
                session.thread_id,
                action == "autopost:on",
                expected_target=(session.target_type, session.target_name),
            )
            self._autopost(session)
        elif action == "help":
            h._send_help(session.chat_id, session.thread_id)
