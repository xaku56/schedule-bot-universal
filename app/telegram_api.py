from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_TELEGRAM_RESPONSE_BYTES = 10 * 1024 * 1024


def _read_response(response: Any) -> bytes:
    content = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
    if len(content) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise RuntimeError("Telegram API response is too large")
    return content


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, **params: Any) -> Any:
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            encoded[key] = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
            )
        for attempt in range(2):
            request = urllib.request.Request(
                f"{self.base_url}/{method}",
                data=urllib.parse.urlencode(encoded).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            parsed_url = urllib.parse.urlparse(request.full_url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.hostname != "api.telegram.org"
            ):
                raise RuntimeError("Refusing an unsafe Telegram API URL")
            try:
                # The HTTPS scheme and exact Telegram host are validated above.
                with urllib.request.urlopen(  # nosec B310
                    request, timeout=70
                ) as response:
                    payload = json.loads(_read_response(response).decode("utf-8"))
            except urllib.error.HTTPError as error:
                try:
                    payload = json.loads(_read_response(error).decode("utf-8"))
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    RuntimeError,
                    OSError,
                ):
                    raise RuntimeError(
                        f"Telegram API {method}: HTTP {error.code}"
                    ) from None
            if payload.get("ok"):
                return payload.get("result")
            retry_after = int((payload.get("parameters") or {}).get("retry_after", 0))
            if attempt == 0 and retry_after:
                time.sleep(min(retry_after, 10))
                continue
            description = str(payload.get("description", "unknown error"))
            raise RuntimeError(f"Telegram API {method}: {description}")
        raise RuntimeError(f"Telegram API {method}: retry limit reached")

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        return self.request(
            "getUpdates",
            offset=offset,
            timeout=50,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.request(
            "sendMessage",
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
            reply_markup=reply_markup,
        )

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.request(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
            reply_markup=reply_markup,
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.request("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        result = self.request("getChatMember", chat_id=chat_id, user_id=user_id)
        return dict(result or {})
