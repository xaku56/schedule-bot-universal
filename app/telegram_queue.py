from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from .telegram_api import TelegramAPI

logger = logging.getLogger(__name__)


@dataclass(order=True)
class _QueuedMessage:
    priority: int
    sequence: int
    chat_id: int = field(compare=False)
    text: str = field(compare=False)
    thread_id: int | None = field(compare=False)
    reply_markup: dict[str, Any] | None = field(compare=False)
    result: Future[None] = field(compare=False)
    message_id: int | None = field(default=None, compare=False)
    stop: bool = field(default=False, compare=False)


class TelegramSendQueue:
    """Serialize Telegram sends and enforce conservative API rate limits."""

    def __init__(
        self,
        api: TelegramAPI,
        messages_per_second: float = 20.0,
        per_chat_interval: float = 1.05,
        max_size: int = 1000,
    ) -> None:
        if messages_per_second <= 0:
            raise ValueError("messages_per_second must be positive")
        self.api = api
        self._global_interval = 1.0 / messages_per_second
        self._per_chat_interval = max(per_chat_interval, 0.0)
        self._queue: queue.PriorityQueue[_QueuedMessage] = queue.PriorityQueue(
            maxsize=max(max_size, 1)
        )
        self._sequence = itertools.count()
        self._closed = threading.Event()
        self._last_global_send = 0.0
        self._last_chat_send: dict[int, float] = {}
        self._worker = threading.Thread(
            target=self._run,
            name="telegram-send-queue",
            daemon=True,
        )
        self._worker.start()

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        *,
        priority: int = 0,
    ) -> None:
        if self._closed.is_set():
            raise RuntimeError("Telegram send queue is closed")
        result: Future[None] = Future()
        item = _QueuedMessage(
            priority=priority,
            sequence=next(self._sequence),
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            reply_markup=reply_markup,
            result=result,
        )
        self._queue.put(item, timeout=10)
        result.result()

    def _rate_limit(self, chat_id: int) -> None:
        now = time.monotonic()
        delay = max(
            self._global_interval - (now - self._last_global_send),
            self._per_chat_interval - (now - self._last_chat_send.get(chat_id, 0.0)),
            0.0,
        )
        if delay:
            time.sleep(delay)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        priority: int = 0,
    ) -> None:
        if self._closed.is_set():
            raise RuntimeError("Telegram send queue is closed")
        result: Future[None] = Future()
        self._queue.put(
            _QueuedMessage(
                priority=priority,
                sequence=next(self._sequence),
                chat_id=chat_id,
                text=text,
                thread_id=None,
                reply_markup=reply_markup,
                result=result,
                message_id=message_id,
            ),
            timeout=10,
        )
        result.result()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item.stop:
                    item.result.set_result(None)
                    return
                self._rate_limit(item.chat_id)
                if item.message_id is None:
                    self.api.send_message(
                        item.chat_id,
                        item.text,
                        item.thread_id,
                        item.reply_markup,
                    )
                else:
                    self.api.edit_message(
                        item.chat_id,
                        item.message_id,
                        item.text,
                        item.reply_markup,
                    )
                sent_at = time.monotonic()
                self._last_global_send = sent_at
                self._last_chat_send[item.chat_id] = sent_at
                if len(self._last_chat_send) > 5000:
                    cutoff = sent_at - 3600
                    self._last_chat_send = {
                        chat_id: timestamp
                        for chat_id, timestamp in self._last_chat_send.items()
                        if timestamp >= cutoff
                    }
                item.result.set_result(None)
            # The worker must propagate every API failure to the waiting Future.
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Telegram send failed chat_id=%s thread_id=%s: %s",
                    item.chat_id,
                    item.thread_id,
                    error,
                )
                item.result.set_exception(error)
            finally:
                self._queue.task_done()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        result: Future[None] = Future()
        self._queue.put(
            _QueuedMessage(
                priority=10**9,
                sequence=next(self._sequence),
                chat_id=0,
                text="",
                thread_id=None,
                reply_markup=None,
                result=result,
                stop=True,
            )
        )
        result.result()
        self._worker.join()
