from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.logging_config import (
    RedactingFormatter,
    safe_traceback,
)
from tests import test_bot as bot_tests


class LoggingTests(unittest.TestCase):
    def test_exception_formatter_keeps_only_locations_even_with_cached_text(
        self,
    ) -> None:
        secret = "PRIVATE_EXCEPTION_AND_LOCAL_VALUE"
        token = "123456789:" + "A" * 35

        class PrivateError(Exception):
            def __str__(self):
                raise AssertionError("Exception text must never be inspected")

        try:
            try:
                raise ValueError(secret + token)
            except ValueError as cause:
                raise PrivateError(secret + token) from cause
        except PrivateError:
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1, "Command failed", (), sys.exc_info()
            )

        record.exc_text = secret + token  # Simulate a previous handler's cached text.
        with (
            patch("linecache.getline", side_effect=AssertionError("Source read")),
            patch("linecache.getlines", side_effect=AssertionError("Source read")),
        ):
            text = RedactingFormatter("%(message)s").format(record)
        self.assertIn("PrivateError traceback=", text)
        frames = json.loads(text.split("traceback=", 1)[1])
        self.assertEqual(len(frames), 1)
        self.assertEqual(set(frames[0]), {"file", "function", "line"})
        self.assertEqual(frames[0]["file"], __file__)
        self.assertEqual(
            frames[0]["function"],
            "test_exception_formatter_keeps_only_locations_even_with_cached_text",
        )
        self.assertIsInstance(frames[0]["line"], int)
        for private in (secret, token, "raise PrivateError", "ValueError", "cause"):
            self.assertNotIn(private, text)
        self.assertEqual(safe_traceback(None), "[]")
        record.exc_info = (None, None, None)
        self.assertEqual(
            RedactingFormatter("%(message)s").format(record),
            "Command failed\nNoneType traceback=[]",
        )

    def test_command_and_error_reply_keep_safe_traces(
        self,
    ) -> None:
        secret = "PRIVATE_COMMAND_MESSAGE"
        token = "123456789:" + "B" * 35

        def fail_handler(_update):
            source_only_marker = "PRIVATE_SOURCE_AND_LOCAL_VALUE"
            raise RuntimeError(secret + token + source_only_marker)

        def fail_send(*_args):
            raise OSError(secret + token)

        with tempfile.TemporaryDirectory() as directory:
            bot = bot_tests.BotTests().make_bot(directory)
            try:
                with (
                    patch.object(
                        bot.handlers, "handle_update", side_effect=fail_handler
                    ),
                    patch.object(bot.sender, "send_message", side_effect=fail_send),
                    self.assertLogs("app.bot", "ERROR") as logs,
                ):
                    bot._handle_update_safely(
                        {
                            "update_id": 123,
                            "message": {"chat": {"id": 1}, "text": secret + token},
                        }
                    )
            finally:
                bot.close()
        self.assertEqual(len(logs.records), 2)
        for record, function, line in (
            (logs.records[0], fail_handler, fail_handler.__code__.co_firstlineno + 2),
            (logs.records[1], fail_send, fail_send.__code__.co_firstlineno + 1),
        ):
            text = record.getMessage()
            self.assertIsNone(record.exc_info)
            for private in (secret, token, "PRIVATE_SOURCE_AND_LOCAL_VALUE", "raise "):
                self.assertNotIn(private, text)
            frames = json.loads(text.split("traceback=", 1)[1])
            self.assertIn(
                {"file": __file__, "function": function.__name__, "line": line}, frames
            )
            self.assertTrue(
                all(set(frame) == {"file", "function", "line"} for frame in frames)
            )
        self.assertIn("update_id=123", logs.records[0].getMessage())
