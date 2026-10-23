from __future__ import annotations

import threading
from typing import Any

COMMAND_DESCRIPTIONS = {
    "today": "Расписание на сегодня с заменами",
    "tomorrow": "Расписание на завтра с заменами",
    "date": "Расписание на дату: /date DD.MM.YYYY",
    "menu": "Главное меню и настройки",
    "setup": "Выбрать группу или преподавателя",
    "help": "Справка по командам",
}
COMMANDS = {f"/{command}" for command in COMMAND_DESCRIPTIONS} | {
    "/start",
    "/group",
    "/groups",
}
MOVED_COMMANDS = {
    "/week",
    "/autopost_on",
    "/autopost_off",
    "/calendar",
    "/calendar_format",
    "/calendar_off",
}
SCHEDULE_COMMANDS = {"/today", "/tomorrow", "/date"}


def parse_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].split("@", 1)[0].casefold(), parts[1].strip() if len(
        parts
    ) > 1 else ""


class CommandGuard:
    """Bound incoming work before submission to the shared executor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[int, tuple[float, float]] = {}
        self._recent: dict[tuple, float] = {}
        self._active: set[tuple] = set()
        self._cleaned_at = 0.0

    def admit(
        self, update: dict[str, Any], now: float, *, date_reply: bool = False
    ) -> tuple | None:
        callback = update.get("callback_query")
        message = (callback or {}).get("message") or update.get("message") or {}
        user_id = ((callback or message).get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if not user_id or not chat_id:
            return None
        if callback:
            command = "callback"
            argument = str(callback.get("data", ""))
            if argument not in {
                "courses",
                "refresh",
                "setup:groups",
                "setup:teacher",
            } and not argument.startswith(
                ("course:", "group:", "teacher:", "teachers:", "m:")
            ):
                return None
        else:
            command, argument = parse_command(str(message.get("text", "")))
            if command not in COMMANDS | MOVED_COMMANDS and not date_reply:
                return None
            if date_reply:
                command = "date_reply"
                argument = (
                    (message.get("reply_to_message") or {}).get("message_id"),
                    str(message.get("text", "")),
                )
            elif command != "/date":
                argument = ""
        request = (chat_id, message.get("message_thread_id") or 0, command, argument)
        recent_key = (user_id, *request)
        if date_reply:
            active_key = (
                user_id,
                chat_id,
                message.get("message_thread_id") or 0,
                "date_reply",
                argument[0],
            )
        elif callback and argument.startswith("m:"):
            # Serialize clicks on one menu before they occupy worker slots.
            active_key = (
                user_id,
                chat_id,
                message.get("message_thread_id") or 0,
                "menu",
                argument.split(":", 2)[1],
            )
        else:
            active_key = request if command in SCHEDULE_COMMANDS else recent_key
        with self._lock:
            if now - self._cleaned_at >= 60:
                self._users = {k: v for k, v in self._users.items() if now - v[1] < 60}
                self._recent = {k: v for k, v in self._recent.items() if now - v < 3}
                self._cleaned_at = now
            tokens, last = self._users.get(user_id, (3.0, now))
            tokens = min(3.0, tokens + (now - last) / 2)
            if tokens < 1 or active_key in self._active:
                return None
            if now - self._recent.get(recent_key, float("-inf")) < 3:
                return None
            # Bound memory even when many different accounts send commands.
            if len(self._users) >= 10000 and user_id not in self._users:
                return None
            if len(self._recent) >= 30000 and recent_key not in self._recent:
                return None
            self._users[user_id] = (tokens - 1, now)
            self._recent[recent_key] = now
            self._active.add(active_key)
            return active_key

    def finish(self, key: tuple) -> None:
        with self._lock:
            self._active.discard(key)
