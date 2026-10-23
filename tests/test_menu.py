from __future__ import annotations

import datetime as dt
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app.bot import Bot
from app.command_guard import COMMANDS, MOVED_COMMANDS, CommandGuard
from app.teacher_schedule import teacher_key
from app.telegram_menu import DATE_PROMPT
from tests.test_calendar import config


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, chat_id, text, thread_id=None, reply_markup=None, **kwargs):
        message = {
            "chat_id": chat_id,
            "text": text,
            "thread_id": thread_id,
            "reply_markup": reply_markup,
            "message_id": len(self.messages) + 1,
        }
        self.messages.append(message)
        return message

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "message_id": message_id,
                "reply_markup": reply_markup,
            }
        )


class MenuTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bot = Bot(config(directory.name))
        self.addCleanup(self.bot.close)
        self.h = self.bot.handlers
        self.h.sender = self.sender = RecordingSender()
        self.h.telegram = MagicMock()
        self.h.validate_semester = lambda: None
        self.h.replacements = MagicMock()
        self.h.replacements.find_for_date.return_value = {"name": "26.10.2026.docx"}
        self.h.replacements.replacements_for_item.return_value = {
            "12 ис": [
                {
                    "pair": "I",
                    "from": "Тестовый Б.Б.(Физика)",
                    "to": "Тестовый Б.Б.(Новая тема)",
                    "room": "203",
                }
            ]
        }
        self.h.replacements.recent_teachers.return_value = []
        self.h.schedules._cache = {
            "groups": {
                group: {
                    "course": 1,
                    "days": {
                        day: {
                            week: [
                                {
                                    "pair": 1,
                                    "subject": subject,
                                    "teacher": teacher,
                                    "room": "101",
                                }
                            ]
                            for week in ("числитель", "знаменатель")
                        }
                        for day in (
                            "понедельник",
                            "вторник",
                            "среда",
                            "четверг",
                            "пятница",
                            "суббота",
                        )
                    },
                }
                for group, subject, teacher in (
                    ("11 ис", "Алгебра", "Тестовый А.А."),
                    ("12 ис", "Физика", "Тестовый Б.Б."),
                )
            }
        }

    def open(self, user=7, chat=-100, thread=10, command="/menu"):
        self.h.handle_message(
            {
                "chat": {"id": chat},
                "from": {"id": user},
                "message_thread_id": thread,
                "text": command,
            }
        )
        return list(self.h.menu._sessions.values())[-1]

    def click(self, session, action, **overrides):
        callback = {
            "id": "click",
            "from": {"id": session.user_id},
            "data": f"m:{session.token}:{action}",
            "message": {
                "chat": {"id": session.chat_id},
                "message_thread_id": session.thread_id,
                "message_id": session.message_id,
            },
        }
        callback.update(overrides)
        self.h.handle_callback(callback)

    def choose_group(self, session, group="12 ис"):
        for action in ("view", "groups", "course:1", "group:" + group):
            self.click(session, action)

    def reply(self, session, text, *, user=None, prompt_id=None):
        message = {
            "chat": {"id": session.chat_id},
            "message_thread_id": session.thread_id,
            "from": {"id": user or session.user_id},
            "text": text,
            "reply_to_message": {
                "message_id": prompt_id or session.prompt_id,
                "text": DATE_PROMPT,
                "from": {"is_bot": True},
            },
        }
        self.h.handle_update({"message": message})
        return message

    def test_group_once_uses_replacements_and_does_not_write_settings(self):
        self.bot.storage.set_binding(-100, 10, "11 ис")
        self.bot.storage.set_autopost(-100, 10, True)
        subscription = dict(self.bot.storage.calendar_subscription(-100, 10, 7))
        binding = dict(self.bot.storage.get_binding(-100, 10))
        session = self.open()
        self.choose_group(session)
        self.click(session, "day:custom")
        old_menu_id = session.message_id
        self.reply(session, "26.10.2026")
        self.assertNotEqual(session.message_id, old_menu_id)
        text = self.sender.messages[-1]["text"]
        self.assertIn("Разовый просмотр", text)
        self.assertIn("12 ИС", text)
        self.assertIn("Новая тема", text)
        self.assertNotIn("Алгебра", text)
        self.h.replacements.find_for_date.assert_called_with(dt.date(2026, 10, 26))
        self.assertEqual(dict(self.bot.storage.get_binding(-100, 10)), binding)
        self.assertEqual(
            dict(self.bot.storage.calendar_subscription(-100, 10, 7)), subscription
        )
        self.assertIsNone(self.bot.storage.get_binding(-100, None))

    def test_teacher_once_works_without_setup_and_uses_all_groups(self):
        for lessons in self.h.schedules._cache["groups"]["11 ис"]["days"][
            "понедельник"
        ].values():
            lessons[0]["teacher"] = "Тестовый Б.Б."
        session = self.open()
        self.click(session, "view")
        self.click(session, "teachers:0")
        teacher = "Тестовый Б. Б."
        token = hashlib.sha256(teacher_key(teacher).encode()).hexdigest()[:16]
        self.click(session, "teacher:" + token)
        self.click(session, "day:custom")
        self.reply(session, "26.10.2026")
        text = self.sender.messages[-1]["text"]
        self.assertIn("Разовый просмотр", text)
        self.assertIn("11 ИС", text)
        self.assertIn("12 ИС", text)
        self.assertIn("Новая тема", text)
        self.assertIsNone(self.bot.storage.get_binding(-100, 10))

    def test_missing_replacements_are_distinct_from_source_failure(self):
        session = self.open()
        self.choose_group(session)
        self.h.replacements.find_for_date.return_value = None
        self.click(session, "day:today")
        self.assertIn(
            "Файл замен на эту дату не найден", self.sender.messages[-1]["text"]
        )
        self.click(session, "dates")
        self.h.replacements.find_for_date.side_effect = RuntimeError("source failure")
        with self.assertRaisesRegex(RuntimeError, "source failure"):
            self.click(session, "day:today")

    def test_two_users_can_choose_independently_in_same_topic(self):
        first, second = self.open(user=7), self.open(user=8)
        with ThreadPoolExecutor(2) as workers:
            futures = [
                workers.submit(self.choose_group, s, group)
                for s, group in ((first, "11 ис"), (second, "12 ис"))
            ]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(first.target_name, "11 ис")
        self.assertEqual(second.target_name, "12 ис")
        self.click(first, "day:custom")
        self.click(second, "day:custom")
        self.reply(first, "26.10.2026", user=8)
        self.assertIn("принадлежит другому", self.sender.messages[-1]["text"])
        self.assertIsNotNone(first.prompt_id)
        self.reply(second, "26.10.2026")
        self.assertIn("12 ИС", self.sender.messages[-1]["text"])
        self.reply(first, "26.10.2026")
        self.assertIn("11 ИС", self.sender.messages[-1]["text"])
        self.assertIsNone(self.bot.storage.get_binding(-100, 10))

    def test_foreign_message_user_topic_and_hidden_action_cannot_mutate_menu(self):
        session = self.open()
        for overrides in (
            {"from": {"id": 8}},
            {
                "message": {
                    "chat": {"id": -100},
                    "message_thread_id": 11,
                    "message_id": session.message_id,
                }
            },
            {
                "message": {
                    "chat": {"id": -101},
                    "message_thread_id": 10,
                    "message_id": session.message_id,
                }
            },
            {
                "message": {
                    "chat": {"id": -100},
                    "message_thread_id": 10,
                    "message_id": session.message_id + 1,
                }
            },
        ):
            self.click(session, "view", **overrides)
            self.assertEqual(session.mode, "saved")
        self.click(session, "calendar:confirm")
        self.assertIsNone(self.bot.storage.get_binding(-100, 10))

    def test_expiry_restart_and_cancel_reject_old_buttons_and_date_replies(self):
        session = self.open()
        self.choose_group(session)
        self.click(session, "day:custom")
        prompt_id = session.prompt_id
        with patch(
            "app.telegram_menu.time.monotonic", return_value=session.expires_at + 1
        ):
            self.reply(session, "26.10.2026")
            self.assertIn("устарело", self.sender.messages[-1]["text"])
            self.click(session, "home")
            self.assertIn("устарело", self.sender.messages[-1]["text"])
        self.click(session, "home")
        self.reply(session, "26.10.2026", prompt_id=prompt_id)
        self.assertIn("устарело", self.sender.messages[-1]["text"])
        self.h.menu._sessions.clear()
        self.click(session, "view")
        self.assertIn("устарело", self.sender.messages[-1]["text"])

    def test_date_validation_can_retry_and_duplicate_reply_is_not_replayed(self):
        session = self.open()
        self.choose_group(session)
        self.click(session, "day:custom")
        for invalid in ("31.02.2026", "abc", "31.02.2026"):
            self.reply(session, invalid)
            self.assertIn("Некорректная дата", self.sender.messages[-1]["text"])
        prompt_id = session.prompt_id
        message = self.reply(session, "26.10.2026")
        guard = CommandGuard()
        key = guard.admit(
            {"message": message}, 0, date_reply=self.h.menu.is_date_reply(message)
        )
        self.assertIsNotNone(key)
        guard.finish(key)
        self.assertIsNone(guard.admit({"message": message}, 1, date_reply=True))
        self.reply(session, "26.10.2026", prompt_id=prompt_id)
        self.assertIn("устарело", self.sender.messages[-1]["text"])
        ordinary = {
            "message": {"chat": {"id": -100}, "from": {"id": 7}, "text": "26.10.2026"}
        }
        self.assertIsNone(CommandGuard().admit(ordinary, 0))

    def test_date_reply_passes_bot_dispatch_without_accepting_unrelated_text(self):
        session = self.open()
        self.choose_group(session)
        self.click(session, "day:custom")
        message = {
            "chat": {"id": -100},
            "message_thread_id": 10,
            "from": {"id": 7},
            "text": "26.10.2026",
            "reply_to_message": {
                "message_id": session.prompt_id,
                "text": DATE_PROMPT,
                "from": {"is_bot": True},
            },
        }
        self.bot._submit_update({"message": message})
        self.bot.executor.shutdown(wait=True)
        self.assertIn("Новая тема", self.sender.messages[-1]["text"])
        self.assertEqual(self.bot.command_guard._active, set())

    def test_setup_buttons_persist_only_explicit_selection(self):
        session = self.open(command="/setup")
        for action in ("groups", "course:1", "group:12 ис"):
            self.click(session, action)
        self.assertEqual(self.bot.storage.get_binding(-100, 10)["target_name"], "12 ис")
        self.assertIsNone(self.bot.storage.get_binding(-100, None))

    def test_quick_commands_and_week_button_use_existing_calculation(self):
        self.bot.storage.set_binding(-100, 10, "11 ис")
        for command in ("/today", "/tomorrow", "/date 26.10.2026"):
            self.h.handle_message(
                {
                    "chat": {"id": -100},
                    "message_thread_id": 10,
                    "from": {"id": 7},
                    "text": command,
                }
            )
            self.assertIn("11 ИС", self.sender.messages[-1]["text"])
        session = self.open()
        with patch.object(self.h, "_send_week") as week:
            self.click(session, "week")
        self.assertEqual(week.call_args.args[:4], (-100, 10, "11 ис", "group"))

    def test_old_commands_only_open_menu_without_changing_settings(self):
        self.bot.storage.set_binding(-100, 10, "11 ис")
        before = dict(self.bot.storage.get_binding(-100, 10))
        self.assertTrue(COMMANDS.isdisjoint(MOVED_COMMANDS))
        for command in sorted(MOVED_COMMANDS):
            self.open(
                command=command + (" short" if command == "/calendar_format" else "")
            )
            self.assertIn("перенесена", self.sender.messages[-1]["text"])
        self.assertEqual(dict(self.bot.storage.get_binding(-100, 10)), before)

    def test_autopost_uses_explicit_state_and_detects_changed_binding(self):
        self.bot.storage.set_binding(-100, 10, "11 ис")
        session = self.open()
        self.click(session, "autopost")
        self.bot.storage.set_binding(-100, 10, "12 ис")
        self.click(session, "autopost:on")
        self.assertEqual(self.bot.storage.get_binding(-100, 10)["autopost"], 0)
        self.click(session, "autopost:on")
        self.assertEqual(self.bot.storage.get_binding(-100, 10)["autopost"], 1)
        self.click(session, "autopost:on")
        self.assertEqual(self.bot.storage.get_binding(-100, 10)["autopost"], 1)
        self.click(session, "autopost:off")
        self.assertEqual(self.bot.storage.get_binding(-100, 10)["autopost"], 0)

    def test_calendar_revoke_requires_confirmation_and_is_personal(self):
        self.bot.storage.set_binding(-100, 10, "11 ис")
        own = self.bot.storage.calendar_subscription(-100, 10, 7)["token"]
        other = self.bot.storage.calendar_subscription(-100, 10, 8)["token"]
        session = self.open()
        self.click(session, "calendar")
        self.click(session, "calendar:confirm")
        self.assertIsNotNone(
            self.bot.storage.calendar_feed_data(own, dt.date.min, dt.date.max)
        )
        self.click(session, "calendar:revoke")
        self.assertIsNotNone(
            self.bot.storage.calendar_feed_data(own, dt.date.min, dt.date.max)
        )
        self.click(session, "calendar:confirm")
        self.assertIsNone(
            self.bot.storage.calendar_feed_data(own, dt.date.min, dt.date.max)
        )
        self.assertIsNotNone(
            self.bot.storage.calendar_feed_data(other, dt.date.min, dt.date.max)
        )
