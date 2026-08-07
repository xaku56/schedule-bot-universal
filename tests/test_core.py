from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.pdf_parser import (
    PARSER_VERSION,
    _is_complete_lesson,
    _lesson_from_raw,
    normalize_group,
    parse_schedule_pdf,
)
from app.replacement_service import apply_replacements
from app.schedule_service import (
    CACHE_SCHEMA_VERSION,
    _select_course_files,
    _semester_key,
    week_type_for_date,
)
from app.storage import Storage


class CoreTests(unittest.TestCase):
    def test_group_normalization(self) -> None:
        self.assertEqual(normalize_group("31ИС"), "31 ис")
        self.assertEqual(normalize_group(" 31   ИС "), "31 ис")

    def test_week_parity(self) -> None:
        start = dt.date(2026, 2, 2)
        self.assertEqual(week_type_for_date(start, start), "числитель")
        self.assertEqual(
            week_type_for_date(start + dt.timedelta(days=7), start), "знаменатель"
        )
        self.assertEqual(
            week_type_for_date(start + dt.timedelta(days=14), start), "числитель"
        )

    def test_pdf_lesson_tolerates_source_typos(self) -> None:
        self.assertTrue(_is_complete_lesson("Егорова\nИнформатика\n207 каб."))
        self.assertTrue(
            _is_complete_lesson("Умбеткалиев Г. А.\nФизическая культура\nспорт.ззал")
        )
        lesson = _lesson_from_raw(
            "Курченок И. Л.\nНачертательная геометрия",
            5,
            "unresolved_pdf_fragment",
        )
        self.assertEqual(lesson["room"], "")
        self.assertEqual(lesson["subject"], "Начертательная геометрия")
        self.assertIn("parse_warning", lesson)

    def test_semester_file_selection(self) -> None:
        files = []
        for semester, modified in (("1", "2025-09-01"), ("2", "2026-02-01")):
            for course in range(1, 5):
                files.append(
                    {
                        "name": f"{course} курс {semester} семестр 2025-2026.pdf",
                        "modified": modified,
                    }
                )
        semester, selected = _select_course_files(files)
        self.assertEqual(semester, "2025-2026:semester-2")
        self.assertEqual([course for course, _ in selected], [1, 2, 3, 4])
        self.assertEqual(_semester_key("1 курс 2 семестр 2025-2026.pdf"), semester)

    def test_version_constants_are_positive(self) -> None:
        self.assertGreaterEqual(PARSER_VERSION, 1)
        self.assertGreaterEqual(CACHE_SCHEMA_VERSION, 1)

    def test_replacement_merge(self) -> None:
        schedule = {
            "pairs": [
                {
                    "pair": 2,
                    "time": "09:40-11:10",
                    "teacher": "Старый",
                    "subject": "Старый предмет",
                    "room": "100",
                }
            ],
            "replacements_count": 0,
        }
        replacements = [
            {
                "pair": "II",
                "group": "31 ИС",
                "from": "",
                "to": "Иванов И.И. (Новый предмет)",
                "room": "200",
            }
        ]
        result = apply_replacements(schedule, replacements)
        self.assertEqual(result["pairs"][0]["subject"], "Новый предмет")
        self.assertEqual(result["pairs"][0]["teacher"], "Иванов И.И.")
        self.assertEqual(result["pairs"][0]["room"], "200")
        self.assertEqual(result["pairs"][0]["status"], "replaced")


class StorageTests(unittest.TestCase):
    def test_autopost_disables_only_failing_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 10, "31 ис")
            storage.set_binding(2, 20, "21 ис")
            storage.set_autopost(1, 10, True)
            storage.set_autopost(2, 20, True)
            self.assertFalse(storage.record_autopost_failure(1, 10, "error"))
            self.assertFalse(storage.record_autopost_failure(1, 10, "error"))
            self.assertTrue(storage.record_autopost_failure(1, 10, "error"))
            active = {
                (row["chat_id"], row["thread_id"])
                for row in storage.autopost_bindings()
            }
            self.assertEqual(active, {(2, 20)})

    def test_corrupt_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.cache_path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(storage.load_cache())


class ConfigTests(unittest.TestCase):
    def test_numerator_week_must_be_explicit_monday(self) -> None:
        with (
            patch("app.config._load_env_file"),
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test"}, clear=True),
            self.assertRaises(ValueError),
        ):
            Config.from_env()
        with (
            patch("app.config._load_env_file"),
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "NUMERATOR_WEEK_START": "2026-02-03",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            Config.from_env()


@unittest.skipUnless(
    os.getenv("SCHEDULE_PDF_DIR"), "Set SCHEDULE_PDF_DIR for PDF integration test"
)
class RealPdfTests(unittest.TestCase):
    def test_all_course_pdfs(self) -> None:
        root = Path(os.environ["SCHEDULE_PDF_DIR"])
        expected_counts = {1: 23, 2: 23, 3: 20, 4: 16}
        total = 0
        for course, expected in expected_counts.items():
            path = root / f"{course} курс 2 семестр 2025-2026.pdf"
            parsed = parse_schedule_pdf(path, course)
            self.assertEqual(len(parsed), expected)
            if course == 3:
                tuesday = parsed["31 ис"]["days"]["вторник"]
                numerator = {
                    item["pair"]: item["subject"] for item in tuesday["числитель"]
                }
                denominator = {
                    item["pair"]: item["subject"] for item in tuesday["знаменатель"]
                }
                self.assertEqual(numerator[2], "МДК 05.01")
                self.assertEqual(denominator[2], "МДК 05.02")
            for group_data in parsed.values():
                warning_raw = {item["raw"] for item in group_data.get("warnings", [])}
                lessons = [
                    lesson
                    for day in group_data["days"].values()
                    for week in day.values()
                    for lesson in week
                ]
                preserved = {
                    lesson["raw"] for lesson in lessons if lesson.get("parse_warning")
                }
                self.assertTrue(warning_raw.issubset(preserved))
            total += len(parsed)
        self.assertEqual(total, 82)


if __name__ == "__main__":
    unittest.main()
