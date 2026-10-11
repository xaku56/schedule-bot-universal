from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.logging_config import (
    RedactingFormatter,
    diagnostic_context,
    diagnostic_logger,
    log_diagnostic,
    safe_traceback,
)
from app.telegram_api import TelegramAPI, TelegramAPIError
from tests import test_bot as bot_tests


def response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


class DiagnosticsTests(unittest.TestCase):
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

    def test_command_and_error_reply_keep_safe_traces_when_diagnostics_disabled(
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
                    patch.object(diagnostic_logger, "disabled", True),
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
        self.assertIsNone(diagnostic_context.get())

    def test_request_stages_and_errors_do_not_log_payloads_or_tokens(self) -> None:
        secret = "PRIVATE_MESSAGE_123"
        token = "123456789:" + "A" * 35

        class UnreadableResponse(io.BytesIO):
            def read(self, *_args):
                raise TimeoutError(secret + token)

        cases = (
            (TimeoutError(secret + token), "connect_headers", TimeoutError),
            (UnreadableResponse(), "read_body", TimeoutError),
            (io.BytesIO(secret.encode()), "decode_json", json.JSONDecodeError),
            (
                response(
                    {"ok": False, "error_code": 400, "description": secret + token}
                ),
                "telegram_result",
                TelegramAPIError,
            ),
            (
                urllib.error.HTTPError(
                    "https://api.telegram.org/bot" + token,
                    500,
                    secret,
                    {},
                    io.BytesIO(secret.encode()),
                ),
                "decode_json",
                TelegramAPIError,
            ),
        )
        for result, stage, exception in cases:
            with (
                self.subTest(stage=stage),
                patch(
                    "app.telegram_api.urllib.request.urlopen",
                    side_effect=result if isinstance(result, Exception) else None,
                    return_value=result,
                ),
                self.assertLogs("app.diagnostics", "INFO") as logs,
                self.assertRaises(exception),
            ):
                TelegramAPI(token).send_message(1, secret)
            text = "\n".join(logs.output)
            self.assertIn("telegram_request_started", text)
            self.assertIn("telegram_request_failed", text)
            self.assertIn("stage=" + stage, text)
            self.assertIn("elapsed_ms=", text)
            self.assertNotIn("telegram_request_finished", text)
            self.assertNotIn(token, text)
            self.assertNotIn(secret, text)

    def test_concurrent_updates_and_delivery_retries_keep_their_context(self) -> None:
        secret = "PRIVATE_MESSAGE_456"
        with tempfile.TemporaryDirectory() as directory:
            bot = bot_tests.BotTests().make_bot(directory)
            bot.sender._per_chat_interval = 0
            calls = {}

            def request(req, **_kwargs):
                # The first send in one chat is rate-limited; its context must survive retry.
                from urllib.parse import parse_qs

                chat = parse_qs(req.data.decode()).get("chat_id", [None])[0]
                calls[chat] = calls.get(chat, 0) + 1
                if chat == "1" and calls[chat] == 1:
                    return response(
                        {
                            "ok": False,
                            "error_code": 429,
                            "description": secret,
                            "parameters": {"retry_after": 0.001},
                        }
                    )
                return response({"ok": True, "result": []})

            def handle(update):
                chat_id = update["message"]["chat"]["id"]
                if chat_id == 2:
                    raise ValueError(secret)
                bot.sender.send_message(chat_id, secret)

            with (
                patch.object(bot.handlers, "handle_update", side_effect=handle),
                patch("app.telegram_api.urllib.request.urlopen", side_effect=request),
                self.assertLogs("app", "INFO") as logs,
            ):
                try:
                    for chat_id in (1, 2):
                        bot._submit_update(
                            {
                                "update_id": 100 + chat_id,
                                "message": {
                                    "chat": {"id": chat_id},
                                    "from": {"id": chat_id},
                                    "text": "/today " + secret,
                                },
                            }
                        )
                finally:
                    bot.close()
                bot.telegram.updates(None)
            text = "\n".join(logs.output)
            self.assertNotIn(secret, text)
            for update_id in (101, 102):
                for event in (
                    "update_received",
                    "update_handler_started",
                    "send_enqueued",
                    "send_started",
                    "telegram_request_started",
                    "send_finished",
                    "update_handler_finished",
                ):
                    self.assertTrue(
                        any(
                            event + " " in record.getMessage()
                            and f"update_id={update_id}" in record.getMessage()
                            for record in logs.records
                        ),
                        (event, update_id),
                    )
            self.assertIn("send_retry_scheduled update_id=101", text)
            self.assertIn("status=failed", text)
            self.assertIsNone(diagnostic_context.get())
            poll = next(
                record.getMessage()
                for record in logs.records
                if "telegram_request_started" in record.getMessage()
                and "method=getUpdates" in record.getMessage()
            )
            self.assertNotIn("update_id=", poll)

    def test_diagnostics_can_be_disabled_without_disabling_requests(self) -> None:
        with (
            patch.object(diagnostic_logger, "disabled", True),
            patch(
                "app.telegram_api.urllib.request.urlopen",
                return_value=response({"ok": True, "result": []}),
            ),
            self.assertNoLogs("app.diagnostics", "INFO"),
        ):
            log_diagnostic("test")
            self.assertEqual(TelegramAPI("test").updates(None), [])
        env = {"TELEGRAM_BOT_TOKEN": "test", "NUMERATOR_WEEK_START": "2026-09-28"}
        with (
            patch("app.config._load_env_file"),
            patch.dict(os.environ, env, clear=True),
        ):
            self.assertTrue(Config.from_env().diagnostics_enabled)
            with patch.dict(os.environ, {"DIAGNOSTICS_ENABLED": "false"}):
                self.assertFalse(Config.from_env().diagnostics_enabled)


class NetworkProbeTests(unittest.TestCase):
    def test_probe_samples_both_families_and_continues_after_failure(self) -> None:
        bash = shutil.which("bash")
        windows_bash = Path("C:/Program Files/Git/bin/bash.exe")
        if windows_bash.exists():
            bash = str(windows_bash)
        if not bash:
            self.skipTest("bash is unavailable")
        script = (Path(__file__).parents[1] / "scripts/diagnose-network.sh").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            curl = root / "bin/curl"
            curl.write_text(
                '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> probe.calls\nprintf "http_code=200 remote_ip=127.0.0.1 dns_s=0.001 total_s=0.010"\nif [[ " $* " == *" -6 "* ]]; then exit 28; fi\n',
                encoding="utf-8",
                newline="\n",
            )
            curl.chmod(0o755)
            command = [
                bash,
                "--noprofile",
                "--norc",
                "-c",
                'PATH="$PWD/bin:$PATH"; export PATH; exec bash -s -- "$@"',
                "_",
                "1",
                "2",
            ]
            result = subprocess.run(
                command,
                input=script,
                text=True,
                capture_output=True,
                cwd=root,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(len(lines), 8)
            self.assertEqual(
                sum("family=IPv4" in line and "curl_exit=0" in line for line in lines),
                4,
            )
            self.assertEqual(
                sum("family=IPv6" in line and "curl_exit=28" in line for line in lines),
                4,
            )
            calls = (root / "probe.calls").read_text().splitlines()
            self.assertEqual(len(calls), 8)
            self.assertEqual(
                sum("https://api.telegram.org" in call for call in calls), 4
            )
            self.assertEqual(sum("https://example.com" in call for call in calls), 4)
            self.assertTrue(
                all("--connect-timeout 5 --max-time 10" in call for call in calls)
            )
            command[-1] = "0"
            rejected = subprocess.run(
                command,
                input=script,
                text=True,
                capture_output=True,
                cwd=root,
                timeout=10,
                check=False,
            )
            self.assertEqual(rejected.returncode, 2)
