from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.autopost import AutopostService
from app.storage import Storage
from app.yandex_disk import file_fingerprint


class FakeSchedules:
    def schedule_for(
        self, group: str, target_date: dt.date, _week_start: dt.date
    ) -> dict:
        return {
            "group": group,
            "date": target_date.isoformat(),
            "weekday": "вторник",
            "week_type": "числитель",
            "pairs": [],
            "replacements_count": 0,
            "source": "pdf",
        }


class FakeReplacements:
    def __init__(self) -> None:
        self.item = {
            "name": "Замены 02.09.2026.docx",
            "path": "/replacement.docx",
            "modified": "2026-09-01T10:00:00Z",
            "size": 100,
        }
        self.by_group = {
            "11 ис": [{"pair": "1", "to": "Первый предмет", "room": "101"}],
            "12 ис": [{"pair": "2", "to": "Второй предмет", "room": "102"}],
        }

    def find_for_date(self, _target_date: dt.date) -> dict | None:
        return self.item

    def replacements_for_item(self, _item: dict) -> dict:
        return self.by_group


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(
        self, _chat_id: int, text: str, *_args: object, **_kwargs: object
    ) -> None:
        self.messages.append(text)


class AutopostTests(unittest.TestCase):
    def test_edited_file_resends_only_changed_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 0, "11 ис")
            storage.set_autopost(1, 0, True)
            storage.set_binding(2, 0, "12 ис")
            storage.set_autopost(2, 0, True)
            replacements = FakeReplacements()
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(
                    numerator_week_start=dt.date(2026, 8, 31),
                ),
                storage=storage,
                schedules=FakeSchedules(),
                replacements=replacements,
                sender=sender,
                validate_semester=lambda: None,
            )
            now = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
            service.run(now)
            service.run(now)
            replacements.item["modified"] = "2026-09-01T12:30:00Z"
            replacements.by_group["12 ис"][0]["to"] = "Новый предмет"
            service.run(now)
            self.assertEqual(len(sender.messages), 3)
            self.assertIn("<b>12 ИС</b>", sender.messages[-1])
            self.assertNotIn("Автоотправка:", sender.messages[-1])

    def test_no_replacement_file_sends_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 0, "11 ис")
            storage.set_autopost(1, 0, True)
            replacements = FakeReplacements()
            replacements.item = None
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(
                    numerator_week_start=dt.date(2026, 8, 31),
                ),
                storage=storage,
                schedules=FakeSchedules(),
                replacements=replacements,
                sender=sender,
                validate_semester=lambda: None,
            )

            service.run(dt.datetime(2026, 9, 1, 20, 0, tzinfo=dt.timezone.utc))

            self.assertEqual(sender.messages, [])

    def test_existing_file_fingerprint_is_migrated_without_resend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 0, "11 ис")
            storage.set_autopost(1, 0, True)
            replacements = FakeReplacements()
            date_key = "2026-09-02"
            storage.mark_autopost(
                1,
                0,
                "11 ис",
                date_key,
                "replacement:" + file_fingerprint(replacements.item),
            )
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(
                    numerator_week_start=dt.date(2026, 8, 31),
                ),
                storage=storage,
                schedules=FakeSchedules(),
                replacements=replacements,
                sender=sender,
                validate_semester=lambda: None,
            )

            service.run(dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc))

            self.assertEqual(sender.messages, [])
            self.assertTrue(
                storage.autopost_fingerprint(1, 0, "11 ис", date_key).startswith(
                    "group:"
                )
            )
