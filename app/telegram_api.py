from __future__ import annotations

import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .logging_config import log_diagnostic

MAX_TELEGRAM_RESPONSE_BYTES = 10 * 1024 * 1024
POLL_TIMEOUT_SECONDS = 10
POLL_SOCKET_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10
_REQUEST_IDS = itertools.count(1)


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

    def request(
        self, method: str, *, retry_rate_limit: bool = True, **params: Any
    ) -> Any:
        request_id = next(_REQUEST_IDS)
        try:
            return self._request_once(method, params, request_id=request_id, attempt=1)
        except TelegramRateLimitError as error:
            if not retry_rate_limit:
                raise
            log_diagnostic(
                "telegram_rate_limit_wait",
                request_id=request_id,
                retry_after_s=error.retry_after,
            )
            time.sleep(error.retry_after)
        return self._request_once(method, params, request_id=request_id, attempt=2)

    def _request_once(
        self, method: str, params: dict[str, Any], *, request_id: int, attempt: int
    ) -> Any:
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
        fallback_api_code = 0
        timeout = POLL_SOCKET_TIMEOUT_SECONDS if method == "getUpdates" else 20
        if method == "answerCallbackQuery":
            timeout = 3
        metadata = {
            "request_id": request_id,
            "attempt": attempt,
            "method": method if method.isascii() and method.isalnum() else "invalid",
        }
        started = time.perf_counter()
        stage = "connect_headers"
        log_diagnostic("telegram_request_started", **metadata, timeout_s=timeout)
        try:
            try:
                # The HTTPS scheme and exact Telegram host are validated above.
                with urllib.request.urlopen(  # nosec B310
                    request, timeout=timeout
                ) as response:
                    http_code = getattr(response, "status", 200)
                    log_diagnostic(
                        "telegram_headers_received",
                        **metadata,
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                        http_code=http_code,
                    )
                    stage = "read_body"
                    content = _read_response(response)
                    stage = "decode_json"
                    payload = json.loads(content.decode("utf-8"))
            except urllib.error.HTTPError as error:
                http_code = error.code
                fallback_api_code = error.code
                log_diagnostic(
                    "telegram_headers_received",
                    **metadata,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    http_code=http_code,
                )
                with error:
                    try:
                        stage = "read_body"
                        content = _read_response(error)
                        stage = "decode_json"
                        payload = json.loads(content.decode("utf-8"))
                    except (
                        json.JSONDecodeError,
                        UnicodeDecodeError,
                        RuntimeError,
                        OSError,
                    ):
                        raise TelegramAPIError(
                            method, http_code, f"HTTP {http_code}"
                        ) from None
            stage = "telegram_result"
            if not isinstance(payload, dict):
                raise TelegramAPIError(
                    method, fallback_api_code, "Invalid JSON response"
                )
            if not payload.get("ok"):
                code = int(payload.get("error_code", fallback_api_code))
                retry_after = float(
                    (payload.get("parameters") or {}).get("retry_after", 0)
                )
                if code == 429 or retry_after > 0:
                    raise TelegramRateLimitError(method, retry_after or 1)
                raise TelegramAPIError(
                    method, code, str(payload.get("description", "unknown error"))
                )
        except Exception as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            error_number = getattr(reason, "errno", None)
            log_diagnostic(
                "telegram_request_failed",
                **metadata,
                stage=stage,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                http_code=http_code,
                error_type=type(error).__name__,
                reason_type=type(reason).__name__,
                errno=error_number if isinstance(error_number, int) else None,
                telegram_code=error.code
                if isinstance(error, TelegramAPIError)
                else None,
            )
            raise
        log_diagnostic(
            "telegram_request_finished",
            **metadata,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            http_code=http_code,
        )
        return payload.get("result")

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
    ) -> None:
        self.request(
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
