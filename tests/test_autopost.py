from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from app.autopost import AutopostService
from app.storage import Storage


class FakeSchedules:
    cache: ClassVar[dict[str, str]] = {"source_fingerprint": "base-v1"}

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

    def find_for_date(self, _target_date: dt.date) -> dict:
        return self.item

    def replacements_for_item(self, _item: dict) -> dict:
        return {}


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(
        self, _chat_id: int, text: str, *_args: object, **_kwargs: object
    ) -> None:
        self.messages.append(text)


class AutopostTests(unittest.TestCase):
    def test_edited_replacement_is_sent_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 0, "11 ис")
            storage.set_autopost(1, 0, True)
            replacements = FakeReplacements()
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(
                    numerator_week_start=dt.date(2026, 8, 31),
                    autopost_time=dt.time(18, 0),
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
            service.run(now)
            self.assertEqual(len(sender.messages), 2)
