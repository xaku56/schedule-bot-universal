from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .command_guard import COMMAND_DESCRIPTIONS

MAX_TELEGRAM_RESPONSE_BYTES = 10 * 1024 * 1024
POLL_TIMEOUT_SECONDS = 10
POLL_SOCKET_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10


def _create_ipv4_connection(
    address: tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Keep create_connection semantics, restricting DNS results to IPv4."""
    last_error = None
    for family, kind, protocol, _, destination in socket.getaddrinfo(
        *address, family=socket.AF_INET, type=socket.SOCK_STREAM
    ):
        connection = None
        try:
            connection = socket.socket(family, kind, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(destination)
            return connection
        except OSError as error:
            last_error = error
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("getaddrinfo returns an empty list")


class _TelegramHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, **kwargs: Any):
        super().__init__(host, **kwargs)
        # Instance hook: other HTTP clients and the process resolver are untouched.
        self._create_connection = _create_ipv4_connection


class _TelegramHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request: urllib.request.Request):
        return self.do_open(
            _TelegramHTTPSConnection,
            request,
            context=self._context,
        )


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str):
        self.code = code
        self.delivery_unavailable = code == 403 or (
            code == 400
            and any(
                reason in description.casefold()
                for reason in (
                    "chat not found",
                    "message thread not found",
                    "chat_id is empty",
                )
            )
        )
        super().__init__(f"Telegram API {method}: {description}")


class TelegramRateLimitError(TelegramAPIError):
    def __init__(self, method: str, retry_after: float):
        self.retry_after = max(float(retry_after), 0.0)
        super().__init__(method, 429, f"retry after {self.retry_after} seconds")


def _read_response(response: Any) -> bytes:
    content = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
    if len(content) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise RuntimeError("Telegram API response is too large")
    return content


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._opener = urllib.request.build_opener(_TelegramHTTPSHandler())

    def request(
        self, method: str, *, retry_rate_limit: bool = True, **params: Any
    ) -> Any:
        try:
            return self._request_once(method, params)
        except TelegramRateLimitError as error:
            if not retry_rate_limit:
                raise
            time.sleep(error.retry_after)
        return self._request_once(method, params)

    def _request_once(self, method: str, params: dict[str, Any]) -> Any:
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            encoded[key] = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
            )
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=urllib.parse.urlencode(encoded).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        parsed_url = urllib.parse.urlparse(request.full_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.telegram.org":
            raise RuntimeError("Refusing an unsafe Telegram API URL")
        http_code = 0
        timeout = POLL_SOCKET_TIMEOUT_SECONDS if method == "getUpdates" else 20
        if method == "answerCallbackQuery":
            timeout = 3
        try:
            # The HTTPS scheme and exact Telegram host are validated above.
            with self._opener.open(request, timeout=timeout) as response:
                payload = json.loads(_read_response(response).decode("utf-8"))
        except urllib.error.HTTPError as error:
            http_code = error.code
            with error:
                try:
                    payload = json.loads(_read_response(error).decode("utf-8"))
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    RuntimeError,
                    OSError,
                ):
                    raise TelegramAPIError(
                        method, http_code, f"HTTP {http_code}"
                    ) from None
        if not isinstance(payload, dict):
            raise TelegramAPIError(method, http_code, "Invalid JSON response")
        if not payload.get("ok"):
            code = int(payload.get("error_code", http_code))
            retry_after = float((payload.get("parameters") or {}).get("retry_after", 0))
            if code == 429 or retry_after > 0:
                raise TelegramRateLimitError(method, retry_after or 1)
            raise TelegramAPIError(
                method, code, str(payload.get("description", "unknown error"))
            )
        return payload.get("result")

    def register_commands(self) -> None:
        self.request(
            "setMyCommands",
            commands=[
                {"command": name, "description": description}
                for name, description in COMMAND_DESCRIPTIONS.items()
            ],
        )

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        return self.request(
            "getUpdates",
            offset=offset,
            timeout=POLL_TIMEOUT_SECONDS,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "sendMessage",
            retry_rate_limit=False,
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
            retry_rate_limit=False,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
            reply_markup=reply_markup,
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.request(
            "answerCallbackQuery",
            retry_rate_limit=False,
            callback_query_id=callback_id,
            text=text,
        )
