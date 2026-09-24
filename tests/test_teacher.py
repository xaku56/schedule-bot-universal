from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from app.autopost import AutopostService
from app.schedule_formatter import (
    format_teacher_schedule,
    format_teacher_weekday_schedule,
)
from app.schedule_service import ScheduleRepository
from app.storage import Storage
from app.teacher_schedule import available_teachers, schedule_for_teacher, teacher_key
from app.telegram_handlers import TelegramHandlers

WEEK_START = dt.date(2026, 9, 21)
TUESDAY = dt.date(2026, 9, 22)


def lesson(pair: int, teacher: str, subject: str) -> dict:
    return {"pair": pair, "teacher": teacher, "subject": subject, "room": "101"}


def repository(directory: str) -> ScheduleRepository:
    storage = Storage(Path(directory))
    groups = {}
    for group, lessons in {
        "11 ис": [
            lesson(1, "Иванова И. И.", "Алгебра"),
            lesson(2, "Петров П.П.", "Физика"),
        ],
        "12 ис": [lesson(1, "Иванова И.И./Сидоров С.С.", "Практика")],
        "13 ис": [],
    }.items():
        groups[group] = {
            "course": 1,
            "days": {"вторник": {"числитель": lessons, "знаменатель": []}},
        }
    storage.save_cache({"groups": groups})
    return ScheduleRepository(storage, "https://example.invalid")


class TeacherScheduleTests(unittest.TestCase):
    def test_teacher_schedule_keeps_parallel_groups_and_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            self.assertEqual(
                teacher_key(" Иванова И. И. "), teacher_key("Иванова И.И.")
            )
            self.assertIn("Сидоров С.С.", available_teachers(schedules))
            replacements = {
                "11 ис": [
                    {
                        "pair": "I",
                        "from": "Иванова И.И.(Алгебра)",
                        "to": "нет",
                        "room": "-",
                    }
                ],
                "12 ис": [
                    {
                        "pair": "I",
                        "from": "Иванова И.И.(Практика)",
                        "to": "Петров П. П.(Физика)",
                        "room": "205",
                    }
                ],
                "13 ис": [
                    {
                        "pair": "I",
                        "from": "нет",
                        "to": "Иванова И.И.(Консультация)",
                        "room": "303",
                    }
                ],
            }
            base = schedule_for_teacher(schedules, "Иванова И.И.", TUESDAY, WEEK_START)
            self.assertEqual(
                [pair["group"] for pair in base["pairs"]], ["11 ис", "12 ис"]
            )
            result = schedule_for_teacher(
                schedules, "Иванова И.И.", TUESDAY, WEEK_START, replacements
            )
            self.assertEqual([pair["group"] for pair in result["pairs"]], ["13 ис"])
            self.assertEqual(
                [(item["group"], item["status"]) for item in result["changes"]],
                [("11 ис", "cancelled"), ("12 ис", "removed")],
            )
            self.assertIn("Снятые и отменённые пары", format_teacher_schedule(result))
            incoming = schedule_for_teacher(
                schedules, "Петров П.П.", TUESDAY, WEEK_START, replacements
            )
            self.assertEqual(
                [(pair["group"], pair["pair"]) for pair in incoming["pairs"]],
                [("12 ис", 1), ("11 ис", 2)],
            )

    def test_docx_old_teacher_is_visible_even_without_pdf_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            result = schedule_for_teacher(
                schedules,
                "Иванова И.И.",
                TUESDAY,
                WEEK_START,
                {
                    "13 ис": [
                        {
                            "pair": "III",
                            "from": "Иванова И. И.(Практика)",
                            "to": "Сидоров С.С.(Практика)",
                            "room": "201",
                        }
                    ],
                },
            )
            self.assertEqual(result["changes"][0]["group"], "13 ис")
            self.assertEqual(result["changes"][0]["pair"], 3)

    def test_week_format_keeps_two_groups_on_same_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            first = schedule_for_teacher(schedules, "Иванова И.И.", TUESDAY, WEEK_START)
            second = schedule_for_teacher(
                schedules, "Иванова И.И.", TUESDAY + dt.timedelta(days=7), WEEK_START
            )
            text = format_teacher_weekday_schedule(
                "Иванова И.И.", "вторник", first["pairs"], second["pairs"]
            )
            self.assertIn("11 ИС", text)
            self.assertIn("12 ИС", text)
            self.assertIn("Ч:", text)


class TeacherStorageTests(unittest.TestCase):
    def test_legacy_bindings_and_history_are_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE bindings (
                        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                        group_name TEXT NOT NULL, autopost INTEGER NOT NULL DEFAULT 0,
                        autopost_failures INTEGER NOT NULL DEFAULT 0, autopost_last_error TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, thread_id));
                    CREATE TABLE sent_autoposts (
                        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                        group_name TEXT NOT NULL, target_date TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, thread_id, group_name, target_date));
                    INSERT INTO bindings(chat_id, thread_id, group_name, autopost) VALUES (1, 0, '11 ис', 1);
                    INSERT INTO sent_autoposts(chat_id, thread_id, group_name, target_date, fingerprint)
                    VALUES (1, 0, '11 ис', '2026-09-22', 'old');
                """)
            storage = Storage(Path(directory))
            self.assertEqual(storage.get_binding(1, None)["target_name"], "11 ис")
            self.assertEqual(
                storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-22"), "old"
            )
            storage = Storage(Path(directory))
            storage.set_binding(1, None, "Иванова И.И.", "teacher")
            storage.mark_autopost(1, 0, "Иванова И.И.", "2026-09-22", "new", "teacher")
            self.assertEqual(storage.get_binding(1, None)["target_type"], "teacher")
            self.assertEqual(
                storage.autopost_fingerprint(
                    1, 0, "Иванова И.И.", "2026-09-22", "teacher"
                ),
                "new",
            )
            self.assertEqual(
                storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-22"), "old"
            )


class FakeReplacements:
    def __init__(self) -> None:
        self.item = {
            "name": "22.09.2026.docx",
            "path": "/22.docx",
            "modified": "1",
            "size": 100,
        }
        self.by_group = {
            "13 ис": [
                {
                    "pair": "I",
                    "from": "нет",
                    "to": "Иванова И.И.(Консультация)",
                    "room": "303",
                }
            ]
        }

    def find_for_date(self, _date: dt.date) -> dict:
        return self.item

    def replacements_for_item(self, _item: dict) -> dict:
        return self.by_group


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict | None]] = []

    def send_message(
        self,
        _chat_id: int,
        text: str,
        _thread_id: int | None = None,
        keyboard: dict | None = None,
        **_kwargs: object,
    ) -> None:
        self.messages.append((text, keyboard))


class TeacherIntegrationTests(unittest.TestCase):
    def test_search_bind_and_request_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            sender = FakeSender()
            handlers = TelegramHandlers(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=schedules.storage,
                schedules=schedules,
                replacements=FakeReplacements(),
                telegram=SimpleNamespace(answer_callback=lambda _id: None),
                sender=sender,
                timezone=dt.timezone.utc,
                validate_semester=lambda: None,
                status_text=lambda: "status",
            )
            message = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}
            handlers.handle_message({**message, "text": "/teacher Иванова"})
            token = sender.messages[-1][1]["inline_keyboard"][0][0]["callback_data"]
            handlers.handle_callback(
                {"id": "1", "data": token, "message": message, "from": {"id": 1}}
            )
            self.assertEqual(
                schedules.storage.get_binding(1, None)["target_type"], "teacher"
            )
            handlers.handle_message({**message, "text": "/date 22.09.2026"})
            self.assertIn("Преподаватель: <b>Иванова", sender.messages[-1][0])
            self.assertIn("13 ИС", sender.messages[-1][0])

    def test_teacher_autopost_only_resends_on_own_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            storage = schedules.storage
            storage.set_binding(1, None, "Иванова И.И.", "teacher")
            storage.set_autopost(1, None, True)
            replacements = FakeReplacements()
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=storage,
                schedules=schedules,
                replacements=replacements,
                sender=sender,
                validate_semester=lambda: None,
            )
            now = dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)
            service.run(now)
            self.assertEqual(len(sender.messages), 1)
            replacements.by_group["13 ис"][0]["room"] = "304"
            service.run(now)
            self.assertEqual(len(sender.messages), 2)
            replacements.by_group["13 ис"][0]["from"] = "Другая запись"
            service.run(now)
            self.assertEqual(len(sender.messages), 2)
