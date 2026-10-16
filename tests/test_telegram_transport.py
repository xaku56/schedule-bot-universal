from __future__ import annotations

import io
import json
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from app.command_guard import COMMAND_DESCRIPTIONS, COMMANDS
from app.telegram_api import (
    TelegramAPI,
    _create_ipv4_connection,
    _TelegramHTTPSConnection,
    _TelegramHTTPSHandler,
)


class TelegramTransportTests(unittest.TestCase):
    def test_ipv4_connection_filters_dns_and_closes_failed_addresses(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ("192.0.2.1", "192.0.2.2")
        ]
        first, second = MagicMock(), MagicMock()
        first.connect.side_effect = TimeoutError("first address unavailable")
        with (
            patch("app.telegram_api.socket.getaddrinfo", return_value=addresses) as dns,
            patch("app.telegram_api.socket.socket", side_effect=[first, second]),
        ):
            result = _create_ipv4_connection(
                ("api.telegram.org", 443), 20, ("0.0.0.0", 0)
            )
        dns.assert_called_once_with(
            "api.telegram.org", 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        self.assertIs(result, second)
        first.close.assert_called_once()
        second.close.assert_not_called()
        for connection, address in zip((first, second), addresses):
            connection.settimeout.assert_called_once_with(20)
            connection.bind.assert_called_once_with(("0.0.0.0", 0))
            connection.connect.assert_called_once_with(address[4])
        with (
            patch("app.telegram_api.socket.getaddrinfo", return_value=addresses[:1]),
            patch("app.telegram_api.socket.socket", return_value=first),
            self.assertRaises(TimeoutError),
        ):
            _create_ipv4_connection(("api.telegram.org", 443), 3)
        with (
            patch("app.telegram_api.socket.getaddrinfo", return_value=[]),
            self.assertRaisesRegex(OSError, "empty list"),
        ):
            _create_ipv4_connection(("api.telegram.org", 443), 3)

    def test_settings_are_scoped_and_default_tls_and_proxies_are_preserved(
        self,
    ) -> None:
        resolver, factory, global_opener = (
            socket.getaddrinfo,
            socket.create_connection,
            urllib.request._opener,
        )
        proxies = {"https": "http://proxy.invalid:8080"}
        for ipv4_only in (False, True):
            with patch("urllib.request.getproxies", return_value=proxies):
                api = TelegramAPI("test", ipv4_only=ipv4_only)
            handler = next(
                h for h in api._opener.handlers if isinstance(h, _TelegramHTTPSHandler)
            )
            proxy = next(
                h
                for h in api._opener.handlers
                if isinstance(h, urllib.request.ProxyHandler)
            )
            self.assertEqual(proxy.proxies, proxies)
            self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(handler._context.check_hostname)
            connection = _TelegramHTTPSConnection(
                "api.telegram.org", ipv4_only=ipv4_only, diagnostic_metadata={}
            )
            self.assertIs(
                connection._create_connection,
                _create_ipv4_connection if ipv4_only else factory,
            )
        self.assertIs(socket.getaddrinfo, resolver)
        self.assertIs(socket.create_connection, factory)
        self.assertIs(urllib.request._opener, global_opener)

    def test_requests_log_actual_family_and_keep_tls_hostname_and_proxy_tunnel(
        self,
    ) -> None:
        token = "123456789:" + "A" * 35
        secret = "PRIVATE_MESSAGE_AND_PROXY_PASSWORD"
        body = json.dumps({"ok": True, "result": []}).encode()
        for ipv4_only, family, proxies in (
            (True, socket.AF_INET, {}),
            (False, socket.AF_INET6, {}),
            (
                True,
                socket.AF_INET,
                {"https": f"http://user:{secret}@proxy.invalid:8080"},
            ),
        ):
            with self.subTest(ipv4_only=ipv4_only, proxy=bool(proxies)):
                with patch("urllib.request.getproxies", return_value=proxies):
                    api = TelegramAPI(token, ipv4_only=ipv4_only)
                handler = next(
                    h
                    for h in api._opener.handlers
                    if isinstance(h, _TelegramHTTPSHandler)
                )
                raw, secured = MagicMock(), MagicMock()
                raw.makefile.return_value = io.BytesIO(
                    b"HTTP/1.1 200 Connection established\r\n\r\n"
                )
                secured.family = family
                secured.makefile.return_value = io.BytesIO(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                handler._context = MagicMock()
                handler._context.wrap_socket.return_value = secured
                factory_path = (
                    "app.telegram_api._create_ipv4_connection"
                    if ipv4_only
                    else "app.telegram_api.socket.create_connection"
                )
                with (
                    patch(factory_path, return_value=raw) as connect,
                    self.assertLogs("app.diagnostics", "INFO") as logs,
                ):
                    self.assertEqual(
                        api.request("sendMessage", chat_id=1, text=secret), []
                    )
                destination = (
                    ("proxy.invalid", 8080) if proxies else ("api.telegram.org", 443)
                )
                connect.assert_called_once_with(destination, 20, None)
                handler._context.wrap_socket.assert_called_once_with(
                    raw, server_hostname="api.telegram.org"
                )
                connected = next(
                    record
                    for record in logs.records
                    if record.getMessage().startswith("telegram_connected ")
                )
                text = connected.getMessage()
                self.assertIn(
                    "ip_family=" + ("IPv4" if family == socket.AF_INET else "IPv6"),
                    text,
                )
                self.assertIn(f"via_proxy={bool(proxies)}", text)
                self.assertIn("request_id=", text)
                self.assertIn("attempt=1", text)
                self.assertIn("method=sendMessage", text)
                for private in (token, secret):
                    self.assertNotIn(private, "\n".join(logs.output))

    def test_certificate_failure_closes_connection_and_keeps_logs_safe(self) -> None:
        secret = "PRIVATE_TLS_EXCEPTION"
        token = "123456789:" + "B" * 35
        with patch("urllib.request.getproxies", return_value={}):
            api = TelegramAPI(token, ipv4_only=True)
        handler = next(
            h for h in api._opener.handlers if isinstance(h, _TelegramHTTPSHandler)
        )
        handler._context = MagicMock()
        handler._context.wrap_socket.side_effect = ssl.SSLCertVerificationError(
            secret + token
        )
        raw = MagicMock()
        with (
            patch("app.telegram_api._create_ipv4_connection", return_value=raw),
            self.assertLogs("app.diagnostics", "INFO") as logs,
            self.assertRaises(urllib.error.URLError),
        ):
            api.updates(None)
        raw.close.assert_called_once()
        text = "\n".join(logs.output)
        self.assertIn("telegram_request_failed", text)
        self.assertIn("SSLCertVerificationError", text)
        self.assertNotIn("telegram_connected", text)
        self.assertNotIn(secret, text)
        self.assertNotIn(token, text)

    def test_command_menu_contains_supported_commands_and_argument_hints(self) -> None:
        api = TelegramAPI("test")
        with patch.object(api, "request") as request:
            api.register_commands()
        self.assertEqual(request.call_args.args, ("setMyCommands",))
        commands = request.call_args.kwargs["commands"]
        self.assertEqual(
            {item["command"] for item in commands}, set(COMMAND_DESCRIPTIONS)
        )
        self.assertTrue(all("/" + item["command"] in COMMANDS for item in commands))
        self.assertTrue(all(1 <= len(item["description"]) <= 256 for item in commands))
        self.assertIn("DD.MM.YYYY", COMMAND_DESCRIPTIONS["date"])
        self.assertIn("short", COMMAND_DESCRIPTIONS["calendar_format"])
        self.assertIn("full", COMMAND_DESCRIPTIONS["calendar_format"])
        self.assertNotIn("refresh", COMMAND_DESCRIPTIONS)
