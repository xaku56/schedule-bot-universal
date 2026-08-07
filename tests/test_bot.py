from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from app.bot import Bot
from app.config import Config


class FakeTelegram:
    def __init__(self, status: str = "member"):
        self.status = status
        self.member_calls = 0

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, str]:
        self.member_calls += 1
        return {"status": self.status}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(
        self, _chat_id: int, text: str, *_args: object, **_kwargs: object
    ) -> None:
        self.messages.append(text)


class BotPermissionTests(unittest.TestCase):
    def make_bot(self, directory: str) -> Bot:
        config = Config(
            token="test",
            schedule_url="https://example.invalid/schedule",
            replacements_url="https://example.invalid/replacements",
            data_dir=Path(directory),
            timezone="Europe/Saratov",
            numerator_week_start=dt.date(2026, 2, 2),
            refresh_interval_minutes=60,
            replacement_check_minutes=10,
            autopost_time=dt.time(18, 0),
            autopost_enabled=True,
            worker_count=2,
            expected_semester=None,
        )
        return Bot(config)

    def test_private_owner_can_manage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                bot.telegram = FakeTelegram()
                self.assertTrue(
                    bot._can_manage(
                        {"id": 42, "type": "private"},
                        {"id": 42},
                    )
                )
            finally:
                bot.close()

    def test_group_requires_admin_and_caches_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                fake = FakeTelegram("administrator")
                bot.telegram = fake
                bot.handlers.telegram = fake
                chat = {"id": -1001, "type": "supergroup"}
                user = {"id": 7}
                self.assertTrue(bot._can_manage(chat, user))
                self.assertTrue(bot._can_manage(chat, user))
                self.assertEqual(fake.member_calls, 1)
                bot._admin_cache.clear()
                fake.status = "member"
                self.assertFalse(bot._can_manage(chat, user))
            finally:
                bot.close()

    def test_expected_semester_is_derived_from_week_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                self.assertEqual(
                    bot._expected_semester_key(),
                    "2025-2026:semester-2",
                )
            finally:
                bot.close()

    def test_invalid_course_callback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                telegram = FakeTelegram()
                sender = FakeSender()
                bot.handlers.telegram = telegram
                bot.handlers.sender = sender
                bot.handlers.handle_callback(
                    {
                        "id": "callback",
                        "data": "course:999",
                        "from": {"id": 42},
                        "message": {"chat": {"id": 42, "type": "private"}},
                    }
                )
                self.assertEqual(sender.messages, ["Некорректный номер курса."])
            finally:
                bot.close()


if __name__ == "__main__":
    unittest.main()
