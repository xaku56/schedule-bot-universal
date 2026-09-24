from __future__ import annotations

import datetime as dt
import html
from typing import Any

from .pdf_parser import PAIR_TIMES_DISPLAY

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


def _lesson_detail(lesson: dict[str, Any]) -> str:
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
    return detail


def format_schedule(schedule: dict[str, Any], note: str | None = None) -> str:
    target_date = dt.date.fromisoformat(schedule["date"])
    weekday = WEEKDAY_ACCUSATIVE.get(schedule["weekday"], schedule["weekday"])
    lines = [
        f"<b>Расписание на {html.escape(weekday)} {target_date.day} {MONTHS[target_date.month]}</b>",
        f"Группа: <b>{html.escape(str(schedule['group']).upper())}</b> · {html.escape(schedule['week_type'])}",
    ]
    if not schedule["pairs"]:
        lines.extend(["", "Пар нет"])
    for lesson in schedule["pairs"]:
        pair = int(lesson["pair"])
        lines.extend(
            ["", f"<b>{pair}) {PAIR_TIMES_DISPLAY[pair]}</b>", _lesson_detail(lesson)]
        )
    if note:
        lines.extend(["", html.escape(note)])
    return "\n".join(lines)


def format_weekday_schedule(
    group: str,
    weekday: str,
    numerator_pairs: list[dict[str, Any]],
    denominator_pairs: list[dict[str, Any]],
) -> str:
    numerator = {int(lesson["pair"]): lesson for lesson in numerator_pairs}
    denominator = {int(lesson["pair"]): lesson for lesson in denominator_pairs}
    pair_numbers = sorted(numerator.keys() | denominator.keys())
    lines = [
        f"<b>{html.escape(weekday.capitalize())}</b>",
        f"Группа: <b>{html.escape(group.upper())}</b>",
    ]
    if not pair_numbers:
        lines.extend(["", "Пар нет"])
    for pair in pair_numbers:
        numerator_detail = _lesson_detail(numerator[pair]) if pair in numerator else "—"
        denominator_detail = (
            _lesson_detail(denominator[pair]) if pair in denominator else "—"
        )
        lines.extend(["", f"<b>{pair}) {PAIR_TIMES_DISPLAY[pair]}</b>"])
        if numerator_detail == denominator_detail:
            lines.append(numerator_detail)
        else:
            lines.extend([f"Ч: {numerator_detail}", f"З: {denominator_detail}"])
    return "\n".join(lines)


def _teacher_detail(lesson: dict[str, Any]) -> str:
    group = html.escape(str(lesson["group"]).upper())
    subject = html.escape(str(lesson.get("subject") or lesson.get("raw") or ""))
    room = html.escape(str(lesson.get("room", "")))
    detail = f"{group} — {subject}"
    if room and room != "-":
        detail += f" — {room}"
    if lesson.get("status") in {"replaced", "replacement_only"}:
        detail += " / <b>ЗАМЕНА</b>"
    if lesson.get("parse_warning"):
        detail += " / ⚠️ <i>проверьте исходный PDF</i>"
    return detail


def format_teacher_schedule(schedule: dict[str, Any], note: str | None = None) -> str:
    target_date = dt.date.fromisoformat(schedule["date"])
    weekday = WEEKDAY_ACCUSATIVE.get(schedule["weekday"], schedule["weekday"])
    lines = [
        f"<b>Расписание на {html.escape(weekday)} {target_date.day} {MONTHS[target_date.month]}</b>",
        f"Преподаватель: <b>{html.escape(schedule['teacher'])}</b> · {html.escape(schedule['week_type'])}",
    ]
    if not schedule["pairs"]:
        lines.extend(["", "Пар нет"])
    for lesson in schedule["pairs"]:
        pair = int(lesson["pair"])
        lines.extend(
            ["", f"<b>{pair}) {PAIR_TIMES_DISPLAY[pair]}</b>", _teacher_detail(lesson)]
        )
    if schedule["changes"]:
        lines.extend(["", "<b>Снятые и отменённые пары</b>"])
        for lesson in schedule["changes"]:
            pair = int(lesson["pair"])
            status = (
                "ОТМЕНА" if lesson["status"] == "cancelled" else "ЗАМЕНА ПРЕПОДАВАТЕЛЯ"
            )
            lines.append(f"{pair}) {_teacher_detail(lesson)} / <b>{status}</b>")
    if note:
        lines.extend(["", html.escape(note)])
    return "\n".join(lines)


def format_teacher_weekday_schedule(
    teacher: str,
    weekday: str,
    numerator_pairs: list[dict[str, Any]],
    denominator_pairs: list[dict[str, Any]],
) -> str:
    def by_pair(lessons: list[dict[str, Any]]) -> dict[int, list[str]]:
        result: dict[int, list[str]] = {}
        for lesson in lessons:
            result.setdefault(int(lesson["pair"]), []).append(_teacher_detail(lesson))
        return result

    numerator = by_pair(numerator_pairs)
    denominator = by_pair(denominator_pairs)
    lines = [
        f"<b>{html.escape(weekday.capitalize())}</b>",
        f"Преподаватель: <b>{html.escape(teacher)}</b>",
    ]
    pair_numbers = sorted(numerator.keys() | denominator.keys())
    if not pair_numbers:
        lines.extend(["", "Пар нет"])
    for pair in pair_numbers:
        first = "; ".join(numerator.get(pair, [])) or "—"
        second = "; ".join(denominator.get(pair, [])) or "—"
        lines.extend(["", f"<b>{pair}) {PAIR_TIMES_DISPLAY[pair]}</b>"])
        if first == second:
            lines.append(first)
        else:
            lines.extend([f"Ч: {first}", f"З: {second}"])
    return "\n".join(lines)
