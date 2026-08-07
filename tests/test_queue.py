from __future__ import annotations

import unittest

from app.telegram_queue import TelegramSendQueue


class FakeTelegramAPI:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[int, str]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append((chat_id, text))


class TelegramQueueTests(unittest.TestCase):
    def test_messages_keep_fifo_order(self) -> None:
        api = FakeTelegramAPI()
        sender = TelegramSendQueue(
            api, messages_per_second=10000, per_chat_interval=0, max_size=10
        )
        try:
            sender.send_message(1, "first")
            sender.send_message(1, "second")
            sender.send_message(2, "third")
        finally:
            sender.close()
        self.assertEqual(
            api.messages,
            [(1, "first"), (1, "second"), (2, "third")],
        )

    def test_send_error_is_propagated(self) -> None:
        sender = TelegramSendQueue(
            FakeTelegramAPI(fail=True),
            messages_per_second=10000,
            per_chat_interval=0,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                sender.send_message(1, "message")
        finally:
            sender.close()


if __name__ == "__main__":
    unittest.main()
