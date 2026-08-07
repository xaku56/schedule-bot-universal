from __future__ import annotations

import datetime as dt
import html
from typing import Any

WEEKDAY_ACCUSATIVE = {
    "понедельник": "понедельник",
    "вторник": "вторник",
    "среда": "среду",
    "четверг": "четверг",
    "пятница": "пятницу",
    "суббота": "субботу",
    "воскресенье": "воскресенье",
}
MONTHS = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
PAIR_TIMES = {
    1: "08:00-09:30",
    2: "09:40-11:10",
    3: "11:20-12:50",
    4: "13:20-14:50",
    5: "15:00-16:30",
    6: "16:40-18:10",
    7: "18:20-19:50",
}


def format_schedule(schedule: dict[str, Any], note: str | None = None) -> str:
    target_date = dt.date.fromisoformat(schedule["date"])
    weekday = WEEKDAY_ACCUSATIVE.get(schedule["weekday"], schedule["weekday"])
    lines = [
        f"<b>Расписание на {html.escape(weekday)} {target_date.day} {MONTHS[target_date.month]}</b>",
        f"Группа: <b>{html.escape(schedule['group'])}</b> · {html.escape(schedule['week_type'])}",
    ]
    if not schedule["pairs"]:
        lines.extend(["", "Пар нет"])
    for lesson in schedule["pairs"]:
        pair = int(lesson["pair"])
        subject = html.escape(str(lesson.get("subject") or lesson.get("raw") or ""))
        teacher = html.escape(str(lesson.get("teacher", "")))
        room = html.escape(str(lesson.get("room", "")))
        status = str(lesson.get("status", ""))
        detail = subject
        if teacher:
            detail += f" ({teacher})"
        if room and room != "-":
            detail += f" — {room}"
        if status in {"replaced", "replacement_only"}:
            detail += " / <b>ЗАМЕНА</b>"
        elif status == "cancelled":
            detail += " / <b>ОТМЕНА</b>"
        if lesson.get("parse_warning"):
            detail += " / ⚠️ <i>проверьте исходный PDF</i>"
        lines.extend(["", f"<b>{pair}) {PAIR_TIMES[pair]}</b>", detail])
    if note:
        lines.extend(["", html.escape(note)])
    return "\n".join(lines)
