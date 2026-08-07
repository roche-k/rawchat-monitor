import unittest
import http.server
import os
import socket
import inspect
import threading
import time
import io
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import rawchat_monitor as monitor
import rawchat.client as client_module
import rawchat.dashboard as dashboard_module
import rawchat.runtime as runtime_module
import requests
from urllib3.exceptions import ProtocolError


class ModuleBoundaryTests(unittest.TestCase):
    def test_config_module_backs_the_facade(self):
        from rawchat import config

        self.assertIs(monitor.ProxyConfig, config.ProxyConfig)
        self.assertIs(monitor.load_accounts, config.load_accounts)
        self.assertIs(monitor.load_proxy_config, config.load_proxy_config)

    def test_facade_record_limit_override_reaches_records_module(self):
        records = [
            {
                "requestId": str(index),
                "requestTime": f"2026-07-14T10:{index:02d}:00",
            }
            for index in range(4)
        ]

        with mock.patch.object(monitor, "RECORD_LIMIT", 3):
            normalized = monitor.normalize_codex_data(
                {"codex": {"recentRecords": records}}
            )

        self.assertEqual(3, len(normalized["recentRecords"]))

    def test_records_and_sources_modules_back_the_facade(self):
        from rawchat import records, sources

        self.assertIs(monitor.RecordStore, records.RecordStore)
        self.assertIs(monitor.normalize_codex_data, records.normalize_codex_data)
        self.assertIs(monitor.ApiKeyCache, sources.ApiKeyCache)
        self.assertIs(monitor.SourcePool, sources.SourcePool)

    def test_proxy_and_codex_config_modules_back_the_facade(self):
        from rawchat import codex_config, proxy

        self.assertIs(monitor.RawChatProxyServer, proxy.RawChatProxyServer)
        self.assertIs(monitor.CodexConfigManager, codex_config.CodexConfigManager)

    def test_client_module_backs_the_facade(self):
        from rawchat import client

        self.assertIs(monitor.RawChatClient, client.RawChatClient)
        self.assertIs(monitor.RefreshWorker, client.RefreshWorker)

    def test_dashboard_module_backs_the_facade(self):
        from rawchat import dashboard

        self.assertIs(monitor.DashboardState, dashboard.DashboardState)
        self.assertIs(monitor.build_summary_lines, dashboard.build_summary_lines)
        self.assertIs(monitor.render_dashboard, dashboard.render_dashboard)

    def test_runtime_module_backs_the_facade(self):
        from rawchat import runtime

        self.assertIs(monitor.MonitorRuntime, runtime.MonitorRuntime)
        self.assertIs(monitor.parse_args, runtime.parse_args)
        self.assertIs(monitor.run_dashboard, runtime.run_dashboard)


class SnapshotFormattingTests(unittest.TestCase):
    def test_normalize_codex_keeps_newest_twenty(self):
        self.assertTrue(hasattr(monitor, "normalize_codex_data"))
        records = [
            {
                "requestTime": f"2026-07-14T10:{minute:02d}:00",
                "requestId": str(minute),
            }
            for minute in range(22)
        ]

        normalized = monitor.normalize_codex_data(
            {"codex": {"recentRecords": records}}
        )

        self.assertEqual(20, len(normalized["recentRecords"]))
        self.assertEqual("21", normalized["recentRecords"][0]["requestId"])
        self.assertEqual("2", normalized["recentRecords"][-1]["requestId"])

    def test_normalize_codex_ignores_claude(self):
        self.assertTrue(hasattr(monitor, "normalize_codex_data"))

        normalized = monitor.normalize_codex_data(
            {
                "claudecode": {
                    "recentRecords": [{"requestId": "claude"}]
                },
                "codex": {"recentRecords": [{"requestId": "codex"}]},
            }
        )

        self.assertEqual(
            "codex", normalized["recentRecords"][0]["requestId"]
        )

    def test_web_formatters(self):
        self.assertEqual("1.23K", monitor.fmt_tokens(1234))
        self.assertEqual("$0.12346", monitor.fmt_cost(0.123456))
        self.assertTrue(hasattr(monitor, "fmt_duration"))
        self.assertTrue(hasattr(monitor, "fmt_discount"))
        self.assertEqual("1.23s", monitor.fmt_duration(1234))
        self.assertEqual(
            "8折 (-$0.02000)", monitor.fmt_discount(0.8, 0.02)
        )
        self.assertEqual("-", monitor.fmt_tokens(None))

    def test_record_values_include_every_visible_web_field(self):
        self.assertTrue(hasattr(monitor, "record_values"))
        values = monitor.record_values(
            {
                "requestTime": "2026-07-14T10:20:30",
                "model": "gpt-5-codex",
                "inputTokens": 1000,
                "outputTokens": 200,
                "cacheInputTokens": 300,
                "cacheWriteTokens": 40,
                "reasoningTokens": 50,
                "totalTokens": 1590,
                "rawCost": 0.1,
                "discountRate": 0.8,
                "discountAmount": 0.02,
                "cost": 0.08,
                "ip": "127.0.0.1",
                "responseTime": 2000,
                "firstByteTime": 500,
                "status": "success",
                "_account_email": "one@example.com",
            }
        )

        self.assertEqual(16, len(values))
        self.assertEqual("成功", values[-3])
        self.assertEqual("one@example.com", values[-2])
        self.assertEqual("127.0.0.1", values[-1])

    def test_invalid_time_and_unknown_status_render_as_missing(self):
        values = monitor.record_values(
            {"requestTime": "not-a-time", "status": "unknown"}
        )

        self.assertEqual("-", values[0])
        self.assertEqual("-", values[-1])

    def test_number_rejects_nonfinite_values(self):
        self.assertIsNone(monitor._number(float("nan")))
        self.assertIsNone(monitor._number(float("inf")))
        self.assertIsNone(monitor._number(float("-inf")))

    def test_normalize_sorts_offsets_chronologically_and_invalid_last(self):
        normalized = monitor.normalize_codex_data(
            {
                "codex": {
                    "recentRecords": [
                        {
                            "requestId": "older",
                            "requestTime": "2026-07-14T10:00:00+08:00",
                        },
                        {
                            "requestId": "newer",
                            "requestTime": "2026-07-14T03:00:00+00:00",
                        },
                        {
                            "requestId": "invalid",
                            "requestTime": "not-a-time",
                        },
                    ]
                }
            }
        )

        self.assertEqual(
            ["newer", "older", "invalid"],
            [record["requestId"] for record in normalized["recentRecords"]],
        )


class KeyAndQuotaTests(unittest.TestCase):
    DAILY_EXHAUSTED_MESSAGE = (
        "您当前的 Codex 额度已用完，请返回网页端查看明细。"
    )

    def test_key_cache_round_trips_key_without_password(self):
        self.assertTrue(hasattr(monitor, "ApiKeyCache"))
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = Path(temp_dir) / "keys.json"
            cache = monitor.ApiKeyCache(path)

            cache.set("one@example.com", "sk-one")

            self.assertEqual("sk-one", cache.get("one@example.com"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"one@example.com": "sk-one"}, payload["keys"])
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_quota_data_marks_only_exhausted_account(self):
        self.assertTrue(hasattr(monitor, "is_quota_exhausted"))
        self.assertTrue(
            monitor.is_quota_exhausted(
                {
                    "errorMessage": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE,
                    "subscriptions": {},
                }
            )
        )
        self.assertTrue(
            monitor.is_quota_exhausted(
                {"subscriptions": {"remainingCount": 0}}
            )
        )
        self.assertTrue(
            monitor.is_quota_exhausted(
                {"subscriptions": {}},
                {
                    "enabled": True,
                    "window": {
                        "isLimited": True,
                        "remainingUsd": 0,
                        "disabledReason": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE,
                    },
                },
            )
        )
        self.assertFalse(
            monitor.is_quota_exhausted(
                {"subscriptions": {"remainingCount": 4}}
            )
        )
        self.assertFalse(
            monitor.is_quota_exhausted(
                {
                    "subscriptions": {
                        "billingType": "amount",
                        "remainingCount": 0,
                        "remainingAmount": 99.8,
                    }
                }
            )
        )
        self.assertFalse(
            monitor.is_quota_exhausted(
                {
                    "subscriptions": {
                        "billingType": "amount",
                        "remainingAmount": 99.8,
                    },
                    "recentRecords": [
                        {"errorMessage": "previous quota exhausted"}
                    ],
                }
            )
        )

    def test_daily_amount_limit_exhaustion_overrides_positive_rolling_balance(self):
        self.assertTrue(
            monitor.is_quota_exhausted(
                {
                    "subscriptions": {
                        "billingType": "amount",
                        "period": "daily",
                        "usedAmount": 10,
                        "amountLimit": 10,
                    },
                },
                {
                    "enabled": True,
                    "window": {"remainingUsd": 4, "isLimited": False},
                },
            )
        )

    def test_daily_remaining_amount_zero_is_exhausted_with_rolling_balance(self):
        self.assertTrue(
            monitor.is_quota_exhausted(
                {
                    "subscriptions": {
                        "billingType": "amount",
                        "period": "daily",
                        "remainingAmount": 0,
                    },
                },
                {
                    "enabled": True,
                    "window": {"remainingUsd": 4, "isLimited": False},
                },
            )
        )

    def test_refresh_readds_account_after_daily_quota_recovers(self):
        accounts = [{"email": "one@example.com", "password": "p1"}]
        pool = monitor.SourcePool(accounts, keys={"one@example.com": "key-1"})
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        daily_exhausted = {
            "apiKey": "key-1",
            "subscriptions": {
                "billingType": "amount",
                "period": "daily",
                "usedAmount": 10,
                "amountLimit": 10,
            },
            "recentRecords": [],
        }
        daily_available = {
            **daily_exhausted,
            "subscriptions": {
                "billingType": "amount",
                "period": "daily",
                "usedAmount": 5,
                "amountLimit": 10,
                "remainingAmount": 5,
            },
        }
        client.clients[0].fetch_codex = mock.Mock(
            side_effect=[daily_exhausted, daily_available]
        )
        client.clients[0].fetch_records = mock.Mock(return_value={"items": []})
        client.clients[0].fetch_balance = mock.Mock(return_value={})
        client.clients[0].fetch_user_token = mock.Mock(return_value="user-token")
        client.clients[0].fetch_rolling_limit = mock.Mock(
            return_value={
                "enabled": True,
                "window": {"remainingUsd": 4, "isLimited": False},
            }
        )

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            client.fetch_all_codex()
            client.fetch_rolling_limits()
            self.assertIsNone(pool.choose())
            client.fetch_all_codex()
            client.fetch_rolling_limits()

        self.assertEqual("one@example.com", pool.choose().email)

    def test_upstream_quota_classifier_does_not_rotate_on_auth_or_bad_request(self):
        self.assertTrue(hasattr(monitor, "is_quota_error"))
        self.assertTrue(
            monitor.is_quota_error(402, b'{"error":"payment required"}')
        )
        self.assertTrue(
            monitor.is_quota_error(429, b'{"error":"quota exceeded"}')
        )
        self.assertFalse(
            monitor.is_quota_error(401, b'{"error":"invalid api key"}')
        )
        self.assertFalse(
            monitor.is_quota_error(400, b'{"error":"invalid request"}')
        )

    def test_daily_exhaustion_message_is_detected_by_refresh_and_proxy(self):
        body = json.dumps(
            {"error": self.DAILY_EXHAUSTED_MESSAGE}, ensure_ascii=False
        ).encode("utf-8")

        self.assertTrue(
            monitor.is_quota_exhausted({"error": self.DAILY_EXHAUSTED_MESSAGE})
        )
        self.assertFalse(monitor.is_quota_exhausted({"message": "当天资金用完"}))
        self.assertTrue(monitor.is_quota_error(403, body))
        self.assertTrue(
            monitor.is_quota_exhausted(
                {"errorMessage": self.DAILY_EXHAUSTED_MESSAGE}
            )
        )
        self.assertTrue(
            monitor.is_quota_error(
                403,
                (
                    "unexpected status 403 Forbidden: "
                    + self.DAILY_EXHAUSTED_MESSAGE
                    + " (traceid: example)"
                ).encode("utf-8"),
            )
        )
        self.assertFalse(
            monitor.is_quota_error(403, b'{"error":"insufficient_scope"}')
        )
        self.assertFalse(
            monitor.is_quota_error(403, b'{"error":"quota access forbidden"}')
        )


class AccountConfigurationTests(unittest.TestCase):
    def write_config(self, temp_dir, text, mode=0o600):
        path = Path(temp_dir) / "accounts.toml"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_load_accounts_reads_toml_accounts(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n',
            )

            accounts = monitor.load_accounts(path)

        self.assertEqual(
            [{"email": "one@example.com", "password": "secret"}],
            accounts,
        )

    def test_load_accounts_rejects_missing_file(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            monitor.load_accounts(Path("test") / "missing-accounts.toml")

    def test_load_accounts_rejects_missing_password_without_echoing_value(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\n',
            )

            with self.assertRaisesRegex(ValueError, "password") as raised:
                monitor.load_accounts(path)

        self.assertNotIn("secret", str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX file mode only")
    def test_load_accounts_rejects_group_or_other_permissions(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n',
                mode=0o644,
            )

            with self.assertRaisesRegex(ValueError, "权限"):
                monitor.load_accounts(path)


class ProxyConfigTests(unittest.TestCase):
    def write_config(self, temp_dir, text, mode=0o600):
        path = Path(temp_dir) / "accounts.toml"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_load_proxy_config_parses_socks(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n'
                '[proxy]\nsocks = "127.0.0.1:1080"\n',
            )

            proxy = monitor.load_proxy_config(path)

        self.assertIsNotNone(proxy)
        self.assertEqual("127.0.0.1:1080", proxy.socks)
        self.assertEqual("", proxy.username)
        self.assertEqual("", proxy.password)

    def test_load_proxy_config_supports_auth(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n'
                '[proxy]\nsocks = "proxy.example:9050"\n'
                'username = "user"\npassword = "pass"\n',
            )

            proxy = monitor.load_proxy_config(path)

        self.assertIsNotNone(proxy)
        self.assertEqual("proxy.example:9050", proxy.socks)
        self.assertEqual("user", proxy.username)
        self.assertEqual("pass", proxy.password)

    def test_load_proxy_config_returns_none_when_absent_or_empty(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            absent = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n',
            )
            empty = self.write_config(
                temp_dir,
                '[[accounts]]\nemail = "one@example.com"\npassword = "secret"\n'
                '[proxy]\nsocks = ""\n',
            )

            self.assertIsNone(monitor.load_proxy_config(absent))
            self.assertIsNone(monitor.load_proxy_config(empty))

    def test_requests_proxies_url(self):
        proxy = monitor.ProxyConfig(socks="127.0.0.1:1080")
        self.assertEqual(
            {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"},
            proxy.requests_proxies(),
        )

    def test_requests_proxies_url_with_auth(self):
        proxy = monitor.ProxyConfig(
            socks="proxy.example.com:9050", username="user", password="pass"
        )
        self.assertEqual(
            {
                "http": "socks5://user:pass@proxy.example.com:9050",
                "https": "socks5://user:pass@proxy.example.com:9050",
            },
            proxy.requests_proxies(),
        )

    def test_requests_proxies_url_encodes_reserved_auth_characters(self):
        proxy = monitor.ProxyConfig(
            socks="proxy.example.com:9050",
            username="user/name#tag@example",
            password="pa:ss/word#@",
        )

        self.assertEqual(
            {
                "http": (
                    "socks5://user%2Fname%23tag%40example:"
                    "pa%3Ass%2Fword%23%40@proxy.example.com:9050"
                ),
                "https": (
                    "socks5://user%2Fname%23tag%40example:"
                    "pa%3Ass%2Fword%23%40@proxy.example.com:9050"
                ),
            },
            proxy.requests_proxies(),
        )

    def test_require_socks_passes_without_proxy(self):
        monitor._require_socks(None)

    def test_require_socks_raises_when_pysocks_missing(self):
        proxy = monitor.ProxyConfig(socks="127.0.0.1:1080")
        with mock.patch.dict("sys.modules", {"socks": None}):
            with self.assertRaisesRegex(RuntimeError, "PySocks"):
                monitor._require_socks(proxy)

    def test_rawchat_client_sets_proxies_when_configured(self):
        proxy = monitor.ProxyConfig(socks="127.0.0.1:1080")
        client = monitor.RawChatClient(
            email="one@example.com", password="secret", proxy=proxy
        )
        self.assertEqual(
            {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"},
            client.session.proxies,
        )
        client.close()

    def test_rawchat_client_leaves_proxies_empty_when_not_configured(self):
        client = monitor.RawChatClient(email="one@example.com", password="secret")
        self.assertEqual({}, client.session.proxies)
        client.close()

    def test_rawchat_proxy_server_forwards_proxies(self):
        proxy = monitor.ProxyConfig(socks="127.0.0.1:1080")
        server = monitor.RawChatProxyServer(
            monitor.SourcePool([{"email": "one@example.com", "password": "secret"}]),
            "https://example.invalid",
            proxy=proxy,
        )
        self.assertIs(proxy, server.proxy)
        self.assertEqual(
            {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"},
            server.proxy.requests_proxies(),
        )

    def test_rawchat_proxy_server_defaults_to_no_proxy(self):
        server = monitor.RawChatProxyServer(
            monitor.SourcePool([{"email": "one@example.com", "password": "secret"}]),
            "https://example.invalid",
        )
        self.assertIsNone(server.proxy)


class SourcePoolTests(unittest.TestCase):
    def test_pool_reports_configured_account_count(self):
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )

        self.assertEqual(2, pool.account_count())

    def test_pool_skips_exhausted_source_and_promotes_fallback(self):
        self.assertTrue(hasattr(monitor, "SourcePool"))
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )

        first = pool.choose()
        self.assertEqual("one@example.com", first.email)
        pool.mark_quota_exhausted(first.email, "quota")

        second = pool.choose()
        self.assertEqual("two@example.com", second.email)
        pool.mark_success(second.email)
        self.assertEqual("two@example.com", pool.current_email())

    def test_pool_has_no_available_source_after_both_quota_fail(self):
        self.assertTrue(hasattr(monitor, "SourcePool"))
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )

        pool.mark_quota_exhausted("one@example.com", "first")
        pool.mark_quota_exhausted("two@example.com", "second")

        self.assertIsNone(pool.choose())

    def test_pool_keeps_last_successful_source_when_earlier_source_recovers(self):
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )

        first = pool.choose()
        self.assertEqual("one@example.com", first.email)
        pool.mark_quota_exhausted(first.email, "quota")

        fallback = pool.choose()
        self.assertEqual("two@example.com", fallback.email)
        pool.mark_success(fallback.email)
        pool.update_quota(first.email, False)

        self.assertEqual("two@example.com", pool.choose().email)

    def test_pool_refresh_failure_preserves_route_and_records_failure_state(self):
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )

        pool.mark_refresh_failed("one@example.com", "temporary refresh failure")

        source = pool.choose()
        self.assertIsNotNone(source)
        self.assertEqual("one@example.com", source.email)
        self.assertEqual("refresh_failed", source.status)
        self.assertTrue(source.refresh_failed)
        self.assertEqual("temporary refresh failure", source.refresh_error)

        pool.mark_quota_exhausted("one@example.com", "confirmed quota exhaustion")

        self.assertEqual("exhausted", source.status)
        self.assertEqual("two@example.com", pool.choose().email)

    def test_pool_normalizes_aware_release_time_before_comparison(self):
        release_at = monitor._parse_release_at(
            b'{"releaseAt":"2000-01-01T00:00:00Z"}'
        )
        self.assertIsNotNone(release_at)
        self.assertIsNone(release_at.tzinfo)

        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        pool.mark_quota_exhausted("one@example.com", "quota", release_at)

        self.assertEqual("one@example.com", pool.choose().email)


class ScriptedUpstream:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.lock = threading.Lock()

        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, body):
                with owner.lock:
                    owner.requests.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )
                    status, headers, response_body = owner.scripts.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)

            def do_GET(self):
                self._respond(b"")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                self._respond(body)

            def log_message(self, *_args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class ProxyTests(unittest.TestCase):
    def test_get_models_is_forwarded_with_source_auth(self):
        self.assertTrue(hasattr(monitor, "RawChatProxyServer"))
        upstream = ScriptedUpstream(
            [(200, {"content-type": "application/json"}, b'{"data":[]}')]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.get(
                f"{proxy.base_url}/v1/models", timeout=5
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"data": []}, response.json())
            self.assertEqual("Bearer key-1", upstream.requests[0]["authorization"])
            self.assertEqual(b"", upstream.requests[0]["body"])
        finally:
            proxy.stop()
            upstream.stop()

    def test_quota_response_is_retried_once_with_next_source(self):
        self.assertTrue(hasattr(monitor, "RawChatProxyServer"))
        upstream = ScriptedUpstream(
            [
                (
                    402,
                    {"content-type": "application/json"},
                    b'{"error":"quota exhausted"}',
                ),
                (
                    200,
                    {"content-type": "application/json"},
                    b'{"id":"resp-2"}',
                ),
            ]
        )
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"Authorization": "Bearer local-placeholder"},
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"id": "resp-2"}, response.json())
            self.assertEqual(
                ["Bearer key-1", "Bearer key-2"],
                [item["authorization"] for item in upstream.requests],
            )
        finally:
            proxy.stop()
            upstream.stop()

    def test_forbidden_daily_quota_response_is_retried_once_with_next_source(self):
        upstream = ScriptedUpstream(
            [
                (
                    403,
                    {"content-type": "application/json"},
                    json.dumps(
                        {
                            "error": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
                (
                    200,
                    {"content-type": "application/json"},
                    b'{"id":"resp-2"}',
                ),
            ]
        )
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"id": "resp-2"}, response.json())
            self.assertEqual(
                ["Bearer key-1", "Bearer key-2"],
                [item["authorization"] for item in upstream.requests],
            )
            self.assertEqual(["two@example.com"], pool.available_emails())
        finally:
            proxy.stop()
            upstream.stop()

    def test_forbidden_non_quota_response_is_returned_without_rotation(self):
        upstream = ScriptedUpstream(
            [
                (
                    403,
                    {"content-type": "application/json"},
                    b'{"error":"insufficient_scope"}',
                )
            ]
        )
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(403, response.status_code)
            self.assertEqual(1, len(upstream.requests))
            self.assertEqual("Bearer key-1", upstream.requests[0]["authorization"])
            self.assertEqual(
                ["one@example.com", "two@example.com"],
                pool.available_emails(),
            )
        finally:
            proxy.stop()
            upstream.stop()

    def test_rate_limited_response_is_retried_once_with_next_source(self):
        upstream = ScriptedUpstream(
            [
                (
                    429,
                    {"content-type": "application/json"},
                    b'{"error":"rate limit exceeded"}',
                ),
                (
                    200,
                    {"content-type": "application/json"},
                    b'{"id":"resp-2"}',
                ),
            ]
        )
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"id": "resp-2"}, response.json())
            self.assertEqual(
                ["Bearer key-1", "Bearer key-2"],
                [item["authorization"] for item in upstream.requests],
            )
            self.assertEqual(
                ["one@example.com", "two@example.com"],
                pool.available_emails(),
            )
            source = pool.choose(excluded={"two@example.com"})
            self.assertEqual("one@example.com", source.email)
            self.assertTrue(source.refresh_failed)
        finally:
            proxy.stop()
            upstream.stop()

    def test_non_quota_client_error_is_returned_without_rotation(self):
        self.assertTrue(hasattr(monitor, "RawChatProxyServer"))
        upstream = ScriptedUpstream(
            [(401, {"content-type": "application/json"}, b'{"error":"bad key"}')]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(401, response.status_code)
            self.assertEqual(1, len(upstream.requests))
        finally:
            proxy.stop()
            upstream.stop()

    def test_proxy_event_log_records_status_without_secrets(self):
        upstream = ScriptedUpstream(
            [
                (
                    503,
                    {"content-type": "application/json"},
                    b'{"error":"ordinary upstream outage"}',
                )
            ]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        try:
            with tempfile.TemporaryDirectory(dir="test") as temp_dir:
                proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
                proxy.event_log_dir = Path(temp_dir)
                proxy.start()
                try:
                    response = requests.post(
                        f"{proxy.base_url}/v1/responses",
                        headers={"Authorization": "Bearer local-placeholder"},
                        json={"model": "gpt-5.4", "input": "do-not-log-body"},
                        timeout=5,
                    )
                finally:
                    proxy.stop()

                self.assertEqual(503, response.status_code)
                paths = list(Path(temp_dir).glob("rawchat_proxy_*.jsonl"))
                self.assertEqual(1, len(paths))
                events = [
                    json.loads(line)
                    for line in paths[0].read_text(encoding="utf-8").splitlines()
                ]
                responses = [
                    event
                    for event in events
                    if event["event"] == "upstream_response"
                ]
                self.assertEqual(1, len(responses))
                self.assertEqual(
                    {
                        "source": "account-1",
                        "attempt": 1,
                        "status": 503,
                        "quota_error": False,
                        "switching": False,
                        "error_category": "unknown",
                    },
                    {
                        key: responses[0].get(key)
                        for key in (
                            "source",
                            "attempt",
                            "status",
                            "quota_error",
                            "switching",
                            "error_category",
                        )
                    },
                )
                serialized = paths[0].read_text(encoding="utf-8")
                self.assertNotIn("key-1", serialized)
                self.assertNotIn("local-placeholder", serialized)
                self.assertNotIn("do-not-log-body", serialized)
                request_events = [
                    event
                    for event in events
                    if event["event"] == "request_received"
                ]
                self.assertEqual(1, len(request_events))
                self.assertEqual(
                    {"model": "gpt-5.4"},
                    request_events[0].get("input"),
                )
        finally:
            upstream.stop()

    def test_proxy_event_log_records_first_byte_and_total_response_times(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json", "Content-Length": "3"},
            content=b"",
            iter_content=lambda chunk_size: iter([b"abc"]),
            close=mock.Mock(),
        )
        session = mock.MagicMock()
        session.request.return_value = response
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            proxy.event_log_dir = Path(temp_dir)
            with mock.patch.object(
                monitor.requests, "Session", return_value=session_context
            ), mock.patch.object(
                monitor.time, "monotonic", side_effect=[100.0, 100.125, 100.5]
            ):
                proxy._handle_request(handler)

            paths = list(Path(temp_dir).glob("rawchat_proxy_*.jsonl"))
            events = [
                json.loads(line)
                for line in paths[0].read_text(encoding="utf-8").splitlines()
            ]
            upstream_events = [
                event for event in events if event["event"] == "upstream_response"
            ]

        self.assertEqual(1, len(upstream_events))
        self.assertEqual(125.0, upstream_events[0]["first_byte_time_ms"])
        self.assertEqual(500.0, upstream_events[0]["response_time_ms"])
        self.assertEqual(b"abc", handler.wfile.getvalue())

    def test_proxy_logs_model_without_session_details(self):
        upstream = ScriptedUpstream(
            [
                (
                    200,
                    {"content-type": "application/json"},
                    b'{"id":"resp-1","output":[]}',
                )
            ]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        try:
            with tempfile.TemporaryDirectory(dir="test") as temp_dir:
                proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
                proxy.event_log_dir = Path(temp_dir)
                proxy.start()
                try:
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        json={
                            "model": "gpt-5.6",
                            "instructions": "You are a helpful assistant.",
                            "input": [
                                {"role": "user", "content": "hello world"}
                            ],
                            "tools": [{"type": "web_search_preview"}],
                        },
                        timeout=5,
                    )
                finally:
                    proxy.stop()

                paths = list(Path(temp_dir).glob("rawchat_proxy_*.jsonl"))
                events = [
                    json.loads(line)
                    for line in paths[0].read_text(encoding="utf-8").splitlines()
                ]
                request_events = [
                    e for e in events if e["event"] == "request_received"
                ]
                self.assertEqual(1, len(request_events))
                inp = request_events[0].get("input", {})
                self.assertEqual({"model": "gpt-5.6"}, inp)
                serialized = paths[0].read_text(encoding="utf-8")
                self.assertNotIn("key-1", serialized)
                for detail in (
                    "You are a helpful assistant.",
                    "hello world",
                    "web_search_preview",
                ):
                    self.assertNotIn(detail, serialized)
        finally:
            upstream.stop()

    def test_proxy_does_not_log_non_string_model(self):
        body = json.dumps(
            {"model": {"nested": "do-not-log"}, "input": "secret"}
        ).encode("utf-8")

        self.assertEqual(
            {}, monitor.RawChatProxyServer._extract_input_fields(body)
        )

    def test_proxy_input_extraction_handles_non_json_body(self):
        upstream = ScriptedUpstream(
            [
                (
                    200, {"content-type": "application/json"},
                    b'{"id":"resp-1"}',
                )
            ]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        try:
            with tempfile.TemporaryDirectory(dir="test") as temp_dir:
                proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
                proxy.event_log_dir = Path(temp_dir)
                proxy.start()
                try:
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        data=b"not-json",
                        headers={"Content-Type": "text/plain"},
                        timeout=5,
                    )
                finally:
                    proxy.stop()

                paths = list(Path(temp_dir).glob("rawchat_proxy_*.jsonl"))
                events = [
                    json.loads(line)
                    for line in paths[0].read_text(encoding="utf-8").splitlines()
                ]
                request_events = [
                    e for e in events if e["event"] == "request_received"
                ]
                self.assertEqual(1, len(request_events))
                self.assertNotIn("input", request_events[0])
        finally:
            upstream.stop()

    def test_missing_keys_return_service_unavailable_without_upstream_call(self):
        self.assertTrue(hasattr(monitor, "RawChatProxyServer"))
        upstream = ScriptedUpstream([])
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-5.4", "input": "ping"},
                timeout=5,
            )
            self.assertEqual(503, response.status_code)
            self.assertEqual([], upstream.requests)
        finally:
            proxy.stop()
            upstream.stop()

    def test_successful_sse_is_forwarded_without_rewriting(self):
        self.assertTrue(hasattr(monitor, "RawChatProxyServer"))
        upstream = ScriptedUpstream(
            [(200, {"content-type": "text/event-stream"}, b"data: ok\n\n")]
        )
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, upstream.base_url)
        proxy.start()
        try:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"Accept": "text/event-stream"},
                json={"model": "gpt-5.4", "input": "ping", "stream": True},
                timeout=5,
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual("text/event-stream", response.headers["content-type"])
            self.assertEqual(b"data: ok\n\n", response.content)
        finally:
            proxy.stop()
            upstream.stop()

    def test_close_delimited_sse_forwards_first_event_before_stream_ends(self):
        class DelayedStreamHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                time.sleep(0.3)
                self.wfile.write(b"data: second\n\n")
                self.wfile.flush()

            def log_message(self, *_args):
                return

        upstream = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), DelayedStreamHandler
        )
        upstream_thread = threading.Thread(
            target=upstream.serve_forever, daemon=True
        )
        upstream_thread.start()
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(
            pool, f"http://127.0.0.1:{upstream.server_port}"
        )
        proxy.start()
        try:
            started = time.monotonic()
            with requests.post(
                f"{proxy.base_url}/v1/responses",
                data=b"{}",
                stream=True,
                timeout=5,
            ) as response:
                first_byte = response.raw.read(1)
                first_byte_time = time.monotonic() - started
                response.raw.read()

            self.assertEqual(b"d", first_byte)
            self.assertLess(first_byte_time, 0.2)
        finally:
            proxy.stop()
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=1)

    def test_stream_read_error_after_headers_does_not_send_second_response(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")

        def failing_chunks(chunk_size):
            self.assertEqual(8192, chunk_size)
            yield b"data: first\n\n"
            raise requests.ConnectionError("upstream read timed out")

        response = SimpleNamespace(
            status_code=200,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Length": "100",
            },
            content=b"",
            iter_content=failing_chunks,
            close=mock.Mock(),
        )
        session = mock.MagicMock()
        session.request.return_value = response
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch.object(
            monitor.requests, "Session", return_value=session_context
        ):
            proxy._handle_request(handler)

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(b"data: first\n\n", handler.wfile.getvalue())
        self.assertTrue(handler.close_connection)
        response.close.assert_called_once_with()

    def test_stream_eof_before_declared_length_is_incomplete(self):
        handler = SimpleNamespace(
            close_connection=False,
            wfile=io.BytesIO(),
        )
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Length": "100"},
            raw=SimpleNamespace(
                read1=mock.Mock(side_effect=[b"partial", b""])
            ),
            _content_consumed=False,
            close=mock.Mock(),
        )
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        _, complete = monitor.RawChatProxyServer._send_stream(
            handler, response
        )

        self.assertFalse(complete)
        self.assertTrue(handler.close_connection)
        self.assertEqual(b"partial", handler.wfile.getvalue())
        response.close.assert_called_once_with()

    def test_stream_header_write_error_closes_upstream_without_escaping(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Length": "2"},
            close=mock.Mock(),
        )
        handler = SimpleNamespace(
            close_connection=False,
            send_response=mock.Mock(side_effect=BrokenPipeError()),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
            wfile=io.BytesIO(),
        )

        monitor.RawChatProxyServer._send_stream(handler, response)

        self.assertTrue(handler.close_connection)
        response.close.assert_called_once_with()

    def test_buffered_response_write_error_closes_connection_without_escaping(self):
        handler = SimpleNamespace(
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
            wfile=mock.Mock(),
        )
        handler.wfile.write.side_effect = ConnectionResetError()

        monitor.RawChatProxyServer._send_json(handler, 502, "unavailable")

        self.assertTrue(handler.close_connection)

    def test_request_body_disconnect_does_not_escape_proxy_handler(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=mock.Mock(),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )
        handler.rfile.read.side_effect = ConnectionResetError()

        proxy._handle_request(handler)

        handler.send_response.assert_called_once_with(400)
        self.assertTrue(handler.close_connection)

    def test_truncated_upstream_error_body_is_returned_without_proxy_traceback(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")

        class BrokenBodyResponse:
            status_code = 429
            headers = {"Content-Type": "application/json"}
            close = mock.Mock()

            @property
            def content(self):
                raise ProtocolError("truncated error body")

        session = mock.MagicMock()
        session.request.return_value = BrokenBodyResponse()
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch.object(
            monitor.requests, "Session", return_value=session_context
        ):
            proxy._handle_request(handler)

        handler.send_response.assert_called_once_with(429)
        self.assertTrue(handler.close_connection)


    def test_raw_stream_protocol_error_after_headers_does_not_escape_handler(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")
        read_calls = 0

        def failing_read1(amount, decode_content=True):
            nonlocal read_calls
            self.assertEqual(8192, amount)
            self.assertTrue(decode_content)
            read_calls += 1
            if read_calls == 1:
                return b"data: first\n\n"
            raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")

        response = SimpleNamespace(
            status_code=200,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Length": "100",
            },
            raw=SimpleNamespace(read1=failing_read1),
            _content_consumed=False,
            close=mock.Mock(),
        )
        session = mock.MagicMock()
        session.request.return_value = response
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch.object(
            monitor.requests, "Session", return_value=session_context
        ):
            proxy._handle_request(handler)

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(b"data: first\n\n", handler.wfile.getvalue())
        self.assertTrue(handler.close_connection)
        response.close.assert_called_once_with()

    def test_proxy_uses_separate_connect_and_read_timeouts(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Length": "2"},
            content=b"",
            iter_content=lambda chunk_size: iter([b"{}"]),
            close=mock.Mock(),
        )
        session = mock.MagicMock()
        session.request.return_value = response
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch.object(
            monitor.requests, "Session", return_value=session_context
        ):
            proxy._handle_request(handler)

        self.assertEqual((15, 180), session.request.call_args.kwargs["timeout"])

    def test_upstream_error_before_headers_still_returns_single_502(self):
        pool = monitor.SourcePool(
            [{"email": "one@example.com", "password": "p1"}],
            keys={"one@example.com": "key-1"},
        )
        proxy = monitor.RawChatProxyServer(pool, "https://example.invalid")
        session = mock.MagicMock()
        session.request.side_effect = requests.ConnectTimeout("connect timed out")
        session_context = mock.MagicMock()
        session_context.__enter__.return_value = session
        handler = SimpleNamespace(
            command="POST",
            path="/v1/responses",
            headers={"Content-Length": "2"},
            rfile=io.BytesIO(b"{}"),
            wfile=io.BytesIO(),
            close_connection=False,
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch.object(
            monitor.requests, "Session", return_value=session_context
        ):
            proxy._handle_request(handler)

        handler.send_response.assert_called_once_with(502)


class CodexConfigManagerTests(unittest.TestCase):
    def test_apply_preserves_unrelated_toml_without_backup_or_restore(self):
        self.assertTrue(hasattr(monitor, "CodexConfigManager"))
        self.assertTrue(hasattr(monitor, "tomllib"))
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            original = (
                'model_provider = "old"\n'
                'model = "gpt-5.4"\n'
                'model_reasoning_effort = "xhigh"\n\n'
                '[model_providers.old]\n'
                'name = "Old"\n'
                'base_url = "https://example.invalid/v1"\n\n'
                '[tui]\n'
                'status_line = ["model-with-reasoning", "current-dir"]\n\n'
                '[projects."/src/raw"]\n'
                'trust_level = "trusted"\n\n'
                '[features]\n'
                'goals = true\n'
            )
            config_path.write_text(original, encoding="utf-8")
            manager = monitor.CodexConfigManager(config_path, port=15872)

            manager.apply()

            parsed = monitor.tomllib.loads(
                config_path.read_text(encoding="utf-8")
            )
            self.assertEqual("rawchat_monitor", parsed["model_provider"])
            self.assertEqual("gpt-5.4", parsed["model"])
            self.assertEqual("xhigh", parsed["model_reasoning_effort"])
            self.assertEqual(
                ["model-with-reasoning", "current-dir"],
                parsed["tui"]["status_line"],
            )
            self.assertEqual(
                "trusted", parsed["projects"]["/src/raw"]["trust_level"]
            )
            self.assertTrue(parsed["features"]["goals"])
            self.assertEqual(
                "http://127.0.0.1:15872/v1",
                parsed["model_providers"]["rawchat_monitor"]["base_url"],
            )
            self.assertFalse(
                (
                    config_path.parent
                    / ".config.toml.rawchat-monitor.bak"
                ).exists()
            )

    def test_apply_skips_write_when_provider_is_already_current(self):
        self.assertTrue(hasattr(monitor, "CodexConfigManager"))
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                'model_provider = "rawchat_monitor"\n'
                "\n"
                "[model_providers.rawchat_monitor]\n"
                'name = "RawChat Monitor"\n'
                'base_url = "http://127.0.0.1:15872/v1"\n'
                'wire_api = "responses"\n'
                "requires_openai_auth = false\n",
                encoding="utf-8",
            )
            manager = monitor.CodexConfigManager(config_path, port=15872)
            with mock.patch.object(manager, "_write_atomic") as write_atomic:
                manager.apply()

            write_atomic.assert_not_called()


class ClientTests(unittest.TestCase):
    @staticmethod
    def response(payload):
        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def test_fetch_codex_returns_only_codex(self):
        self.assertTrue(hasattr(monitor, "RawChatClient"))
        session = mock.Mock()
        session.get.return_value = self.response(
            {
                "code": 1,
                "msg": "ok",
                "data": {
                    "claudecode": {
                        "apiKey": "ignored",
                        "isAuth": True,
                        "subscriptions": None,
                        "currentUsage": None,
                        "recentRecords": [],
                        "error": None,
                    },
                    "codex": {
                        "apiKey": "kept",
                        "isAuth": True,
                        "subscriptions": None,
                        "currentUsage": None,
                        "recentRecords": [],
                        "error": None,
                    },
                },
            }
        )

        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        self.assertEqual("kept", client.fetch_codex()["apiKey"])

    def test_facade_timeout_override_reaches_rawchat_client(self):
        session = mock.Mock()
        session.get.return_value = self.response(
            {
                "code": 1,
                "msg": "ok",
                "data": {
                    "codex": {
                        "apiKey": "kept",
                        "isAuth": True,
                        "subscriptions": None,
                        "currentUsage": None,
                        "recentRecords": [],
                        "error": None,
                    }
                },
            }
        )
        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        with mock.patch.object(monitor, "REQUEST_TIMEOUT", 0.01):
            client.fetch_codex()

        self.assertEqual(0.01, session.get.call_args.kwargs["timeout"])

    def test_http_quota_error_keeps_status_and_body(self):
        body = json.dumps(
            {"error": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE},
            ensure_ascii=False,
        ).encode("utf-8")
        response = self.response({})
        response.status_code = 403
        response.content = body
        response.raise_for_status.side_effect = requests.HTTPError(
            "403 Client Error: Forbidden"
        )
        session = mock.Mock()
        session.get.return_value = response
        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        with self.assertRaises(monitor.RawChatError) as raised:
            client.fetch_codex()

        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual(body, raised.exception.body)

    def test_http_error_with_truncated_body_still_becomes_rawchat_error(self):
        class BrokenBodyResponse:
            status_code = 502

            def raise_for_status(self):
                raise requests.HTTPError("502 Bad Gateway")

            @property
            def content(self):
                raise ProtocolError("truncated error body")

        session = mock.Mock()
        session.get.return_value = BrokenBodyResponse()
        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        with self.assertRaises(monitor.RawChatError) as raised:
            client.fetch_codex()

        self.assertEqual(502, raised.exception.status_code)
        self.assertEqual(b"", raised.exception.body)

    def test_fetch_balance_uses_billing_profile_endpoint(self):
        session = mock.Mock()
        session.get.return_value = self.response(
            {
                "code": 1,
                "msg": "ok",
                "data": {
                    "balance": 12.34,
                    "temporaryBalance": 5.67,
                    "temporaryBalanceResetAt": "2026-07-29T00:00:00Z",
                    "billingPreference": "subscription_first",
                    "allowedPreferences": ["subscription_first"],
                },
            }
        )
        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        self.assertEqual(
            {
                "balance": 12.34,
                "temporaryBalance": 5.67,
                "temporaryBalanceResetAt": "2026-07-29T00:00:00Z",
                "billingPreference": "subscription_first",
                "allowedPreferences": ["subscription_first"],
            },
            client.fetch_balance(),
        )
        self.assertEqual(
            "https://rawchat.cn/frontend-api/vibe-code/codex/billing-profile",
            session.get.call_args.args[0],
        )

    def test_balance_text_uses_billing_profile_fields(self):
        self.assertEqual(
            "临时额度:5.67，长期余额:12.34",
            monitor._balance_text(
                {"temporaryBalance": 5.67, "balance": 12.34}
            ),
        )

    def test_summary_uses_codex_subscription_amount_as_balance(self):
        subscription = {
            "subTypeName": "codex 特惠 每日100刀月卡",
            "billingType": "amount",
            "amountLimit": 100,
            "usedAmount": 70.1094657,
            "remainingAmount": 29.8905343,
            "expireTime": "2026-08-22 07:56:04",
        }
        snapshot = monitor.DashboardSnapshot(
            codex={"subscriptions": subscription, "recentRecords": []},
            rolling_limit=None,
            rolling_error=None,
            fetched_at=datetime(2026, 7, 28, 16, 0),
            per_account=[
                {
                    "email": "user@example.com",
                    "subscriptions": subscription,
                    "balance": {
                        "balance": 0,
                        "temporaryBalance": 0,
                    },
                }
            ],
        )
        state = monitor.DashboardState(snapshot=snapshot)

        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 28, 16, 0),
            monotonic_now=0.0,
        )

        self.assertIn("总额 $100.00000", lines[0])
        self.assertIn("已用 $70.10947", lines[0])
        self.assertIn("剩余 $29.89053", lines[0])

    def test_fetch_all_codex_writes_api_key_to_cache_only_when_available(self):
        self.assertTrue(hasattr(monitor, "ApiKeyCache"))
        self.assertIn(
            "key_cache", inspect.signature(monitor.MultiAccountClient).parameters
        )
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            cache = monitor.ApiKeyCache(Path(temp_dir) / "keys.json")
            source_pool = monitor.SourcePool(
                [{"email": "user@example.com", "password": "secret"}],
                keys={},
            )
            client = monitor.MultiAccountClient(
                [{"email": "user@example.com", "password": "secret"}],
                key_cache=cache,
                source_pool=source_pool,
            )
            client.clients[0].fetch_codex = mock.Mock(
                return_value={"apiKey": "stable-key", "recentRecords": []}
            )
            client.clients[0].fetch_records = mock.Mock(
                return_value={"items": []}
            )

            with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
                client.fetch_all_codex()

            self.assertEqual("stable-key", cache.get("user@example.com"))
            self.assertEqual("stable-key", source_pool.choose().api_key)
            self.assertNotIn(
                "secret", Path(cache.path).read_text(encoding="utf-8")
            )

    def test_refresh_marks_daily_exhausted_account_unavailable(self):
        accounts = [
            {"email": "one@example.com", "password": "p1"},
            {"email": "two@example.com", "password": "p2"},
        ]
        pool = monitor.SourcePool(
            accounts,
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        client.clients[0].fetch_codex = mock.Mock(
            return_value={
                "apiKey": "key-1",
                "errorMessage": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE,
                "subscriptions": {},
                "recentRecords": [],
            }
        )
        client.clients[1].fetch_codex = mock.Mock(
            return_value={
                "apiKey": "key-2",
                "subscriptions": {"remainingAmount": 1},
                "recentRecords": [],
            }
        )
        for raw_client in client.clients:
            raw_client.fetch_records = mock.Mock(return_value={"items": []})
            raw_client.fetch_balance = mock.Mock(return_value={})

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            client.fetch_all_codex()

        self.assertEqual("two@example.com", pool.choose().email)

    def test_refresh_marks_http_daily_quota_error_account_unavailable(self):
        accounts = [
            {"email": "one@example.com", "password": "p1"},
            {"email": "two@example.com", "password": "p2"},
        ]
        body = json.dumps(
            {"error": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE},
            ensure_ascii=False,
        ).encode("utf-8")
        pool = monitor.SourcePool(
            accounts,
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        client.clients[0].fetch_codex = mock.Mock(
            side_effect=monitor.RawChatError(
                "配额请求失败: 403 Forbidden",
                status_code=403,
                body=body,
            )
        )
        client.clients[1].fetch_codex = mock.Mock(
            return_value={
                "apiKey": "key-2",
                "subscriptions": {"remainingAmount": 1},
                "recentRecords": [],
            }
        )
        for raw_client in client.clients:
            raw_client.fetch_records = mock.Mock(return_value={"items": []})
            raw_client.fetch_balance = mock.Mock(return_value={})

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            client.fetch_all_codex()

        self.assertEqual("two@example.com", pool.choose().email)

    def test_one_account_quota_failure_does_not_block_other_source(self):
        accounts = [
            {"email": "one@example.com", "password": "p1"},
            {"email": "two@example.com", "password": "p2"},
        ]
        pool = monitor.SourcePool(accounts, keys={})
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        client.clients[0].fetch_codex = mock.Mock(
            side_effect=monitor.RawChatError("first quota endpoint unavailable")
        )
        client.clients[1].fetch_codex = mock.Mock(
            return_value={
                "apiKey": "key-2",
                "subscriptions": {"remainingCount": 4},
                "recentRecords": [],
            }
        )
        for raw_client in client.clients:
            raw_client.fetch_records = mock.Mock(return_value={"items": []})

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            try:
                client.fetch_all_codex()
            except monitor.RawChatError as exc:
                self.fail(f"one account failure aborted the refresh: {exc}")

        chosen = pool.choose()
        self.assertIsNotNone(chosen)
        self.assertEqual("two@example.com", chosen.email)

    def test_refresh_failure_keeps_active_source_and_marks_refresh_failed(self):
        accounts = [{"email": "one@example.com", "password": "p1"}]
        pool = monitor.SourcePool(
            accounts,
            keys={"one@example.com": "key-1"},
        )
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        client.clients[0].fetch_codex = mock.Mock(
            side_effect=monitor.RawChatError(
                "temporary refresh failure",
                status_code=429,
                body=b'{"error":"rate limit exceeded"}',
            )
        )

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            with self.assertRaises(monitor.RawChatError):
                client.fetch_all_codex()

        source = pool.choose()
        self.assertIsNotNone(source)
        self.assertEqual("one@example.com", source.email)
        self.assertEqual("refresh_failed", source.status)

    def test_rolling_refresh_quota_error_removes_source(self):
        accounts = [
            {"email": "one@example.com", "password": "p1"},
            {"email": "two@example.com", "password": "p2"},
        ]
        body = json.dumps(
            {"error": KeyAndQuotaTests.DAILY_EXHAUSTED_MESSAGE},
            ensure_ascii=False,
        ).encode("utf-8")
        pool = monitor.SourcePool(
            accounts,
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        client = monitor.MultiAccountClient(accounts, source_pool=pool)
        client.clients[0].fetch_user_token = mock.Mock(
            side_effect=monitor.RawChatError(
                "rolling quota failure", status_code=403, body=body
            )
        )
        client.clients[1].fetch_user_token = mock.Mock(return_value="user-token")
        client.clients[1].fetch_rolling_limit = mock.Mock(
            return_value={"enabled": False}
        )

        with mock.patch.object(client_module, "ACCOUNT_REQUEST_GAP", 0):
            client.fetch_rolling_limits()

        self.assertEqual("two@example.com", pool.choose().email)

    def test_cached_key_is_loaded_without_login(self):
        self.assertTrue(hasattr(monitor, "MultiAccountClient"))
        self.assertIn(
            "key_cache", inspect.signature(monitor.MultiAccountClient).parameters
        )
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            cache = monitor.ApiKeyCache(Path(temp_dir) / "keys.json")
            cache.set("user@example.com", "stable-key")
            pool = monitor.SourcePool(
                [{"email": "user@example.com", "password": "secret"}],
                keys={},
            )
            client = monitor.MultiAccountClient(
                [{"email": "user@example.com", "password": "secret"}],
                key_cache=cache,
                source_pool=pool,
            )
            client.clients[0].login = mock.Mock(
                side_effect=AssertionError(
                    "cached key must not require key acquisition"
                )
            )

            self.assertEqual("stable-key", pool.choose().api_key)

    def test_rolling_limit_uses_current_api_host(self):
        self.assertTrue(hasattr(monitor, "RawChatClient"))
        session = mock.Mock()
        session.post.return_value = self.response(
            {
                "code": 1,
                "msg": "ok",
                "data": {
                    "generatedAt": "2026-07-14T10:00:00Z",
                    "enabled": False,
                    "disabledReason": "no rule",
                    "subscription": {},
                    "rule": None,
                    "window": None,
                },
            }
        )
        client = monitor.RawChatClient(
            session=session, email="user@example.com", password="secret"
        )

        client.fetch_rolling_limit("user-token")

        self.assertEqual(
            "https://api.rawchat.cn/frontend-api/vibe-code/codex/rolling-limit",
            session.post.call_args.args[0],
        )

    def test_collect_snapshot_keeps_quota_when_rolling_fails(self):
        self.assertTrue(hasattr(monitor, "collect_snapshot"))
        client = mock.Mock()
        codex_data = {
            "apiKey": "key",
            "isAuth": True,
            "subscriptions": None,
            "currentUsage": None,
            "recentRecords": [],
            "error": None,
        }
        client.fetch_all_codex.return_value = (codex_data, [])
        client.fetch_rolling_limits.side_effect = monitor.RawChatError(
            "rolling unavailable"
        )

        snapshot = monitor.collect_snapshot(
            client, now=lambda: datetime(2026, 7, 14, 10, 0)
        )

        self.assertEqual([], snapshot.codex["recentRecords"])
        self.assertEqual([], snapshot.per_account)


class FakeClient:
    def __init__(self, codex_outcomes):
        self.codex_outcomes = list(codex_outcomes)
        self.login_calls = 0

    def login(self):
        self.login_calls += 1

    def login_all(self):
        self.login_calls += 1

    def fetch_codex(self):
        outcome = self.codex_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fetch_all_codex(self):
        codex = self.fetch_codex()
        subs = codex.get("subscriptions") if isinstance(codex, dict) else None
        per_account = [{"subscriptions": subs, "email": "test@example.com"}] if subs else []
        return codex, per_account

    def fetch_rolling_limits(self):
        return [{
            "generatedAt": "2026-07-14T10:00:00Z",
            "enabled": False,
            "disabledReason": "no rule",
            "subscription": {},
            "rule": None,
            "window": None,
        }]

    def close(self):
        pass


class RefreshEngineTests(unittest.TestCase):
    def test_authenticated_client_is_not_logged_in_again(self):
        client = FakeClient([{"recentRecords": []}])
        client.authenticated = True
        client.login_all = mock.Mock(
            side_effect=AssertionError("already authenticated")
        )
        engine = monitor.RefreshEngine(client)

        outcome = engine.refresh()

        self.assertIsNone(outcome.error)
        client.login_all.assert_not_called()

    def test_failure_keeps_last_good_snapshot(self):
        self.assertTrue(hasattr(monitor, "RefreshEngine"))
        client = FakeClient(
            [
                {"recentRecords": []},
                monitor.RawChatError("offline"),
            ]
        )
        engine = monitor.RefreshEngine(
            client, now=lambda: datetime(2026, 7, 14, 10, 0)
        )

        first = engine.refresh()
        second = engine.refresh()

        self.assertIs(first.snapshot, second.snapshot)
        self.assertEqual(1, second.failure_count)
        self.assertEqual("offline", second.error)

    def test_third_failure_reauthenticates_and_retries(self):
        self.assertTrue(hasattr(monitor, "RefreshEngine"))
        client = FakeClient(
            [
                {"recentRecords": []},
                monitor.RawChatError("one"),
                monitor.RawChatError("two"),
                monitor.RawChatError("three"),
                {
                    "recentRecords": [
                        {
                            "requestId": "after-login",
                            "requestTime": "2026-07-14T10:00:00",
                        }
                    ]
                },
            ]
        )
        engine = monitor.RefreshEngine(client)

        for _ in range(4):
            outcome = engine.refresh()

        self.assertEqual(2, client.login_calls)
        self.assertEqual(0, outcome.failure_count)
        self.assertEqual(
            "after-login",
            outcome.snapshot.codex["recentRecords"][0]["requestId"],
        )

    def test_worker_coalesces_requests_and_stops(self):
        self.assertTrue(hasattr(monitor, "RefreshWorker"))
        started = threading.Event()
        release = threading.Event()
        snapshot = monitor.DashboardSnapshot(
            {"recentRecords": []},
            None,
            None,
            datetime(2026, 7, 14, 10, 0),
        )

        class BlockingEngine:
            def refresh(self):
                started.set()
                release.wait(timeout=1)
                return monitor.RefreshOutcome(snapshot, None, 0)

        worker = monitor.RefreshWorker(BlockingEngine())
        worker.start()
        self.assertTrue(worker.request_refresh())
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(worker.request_refresh())
        release.set()

        result = None
        deadline = time.monotonic() + 1
        while result is None and time.monotonic() < deadline:
            result = worker.get_result()
            time.sleep(0.01)

        self.assertIs(snapshot, result.snapshot)
        worker.stop()
        self.assertFalse(worker.request_refresh())

    def test_worker_survives_unexpected_refresh_exception_and_clears_pending(self):
        snapshot = monitor.DashboardSnapshot(
            {"recentRecords": []}, None, None, datetime(2026, 7, 14, 10, 0)
        )

        class FlakyEngine:
            last_snapshot = None
            failure_count = 0

            def __init__(self):
                self.calls = 0

            def refresh(self):
                self.calls += 1
                if self.calls == 1:
                    raise TypeError("unexpected data shape")
                return monitor.RefreshOutcome(snapshot, None, 0)

        engine = FlakyEngine()
        worker = monitor.RefreshWorker(engine)
        worker.start()
        try:
            self.assertTrue(worker.request_refresh())
            first = None
            deadline = time.monotonic() + 1
            while first is None and time.monotonic() < deadline:
                first = worker.get_result()
                time.sleep(0.01)

            self.assertIsNotNone(first)
            self.assertIn("unexpected data shape", first.error)
            self.assertTrue(worker._thread.is_alive())
            self.assertTrue(worker.request_refresh())

            second = None
            deadline = time.monotonic() + 1
            while second is None and time.monotonic() < deadline:
                second = worker.get_result()
                time.sleep(0.01)
            self.assertIs(snapshot, second.snapshot)
        finally:
            worker.stop()

    def test_stop_returns_promptly_and_skips_followup_requests(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingClient:
            def __init__(self):
                self.user_token_calls = 0
                self.close_calls = 0

            def login(self):
                return None

            def login_all(self):
                return None

            def fetch_codex(self):
                started.set()
                release.wait(timeout=1)
                return {"recentRecords": []}

            def fetch_all_codex(self):
                codex = self.fetch_codex()
                return codex, []

            def fetch_rolling_limits(self):
                return [{"enabled": False}]

            def close(self):
                self.close_calls += 1

        client = BlockingClient()
        worker = monitor.RefreshWorker(monitor.RefreshEngine(client))
        worker.start()
        self.assertTrue(worker.request_refresh())
        self.assertTrue(started.wait(timeout=1))
        timer = threading.Timer(0.3, release.set)
        timer.start()

        before = time.monotonic()
        worker.stop()
        elapsed = time.monotonic() - before
        timer.join(timeout=1)
        time.sleep(0.05)

        self.assertLess(elapsed, 0.15)
        self.assertEqual(1, client.close_calls)
        self.assertEqual(0, client.user_token_calls)


class LayoutTests(unittest.TestCase):
    @staticmethod
    def snapshot(*request_ids, rolling=None, rolling_error=None):
        codex = {
            "isAuth": True,
            "subscriptions": {
                "subTypeName": "Codex Pro",
                "billingType": "amount",
                "period": "daily",
                "usedAmount": 1.25,
                "amountLimit": 10,
                "remainingAmount": 8.75,
                "periodResetTime": "2026-07-15T00:00:00",
                "expireTime": "2026-08-01T00:00:00",
            },
            "currentUsage": {
                "totalRequests": 12,
                "totalTokens": 12345,
                "totalCost": 1.23456,
                "lastRequestTime": "2026-07-14T10:20:30",
            },
            "recentRecords": [
                {
                    "requestId": request_id,
                    "requestTime": f"2026-07-14T10:{index:02d}:00",
                    "status": "success",
                }
                for index, request_id in enumerate(request_ids)
            ],
        }
        per_account = [{
            "subscriptions": codex["subscriptions"],
            "rolling_limit": rolling,
            "email": "test@example.com",
        }]
        return monitor.DashboardSnapshot(
            codex,
            rolling,
            rolling_error,
            datetime(2026, 7, 14, 10, 20),
            per_account=per_account,
        )

    def test_table_has_fixed_header_and_full_width(self):
        self.assertTrue(hasattr(monitor, "table_header_line"))

        header = monitor.table_header_line()

        self.assertIn("缓存输入", header)
        self.assertIn("首字耗时", header)
        self.assertEqual(
            monitor.TABLE_WIDTH, monitor.display_width(header)
        )
        self.assertEqual(
            ("状态", "账户", "IP"),
            tuple(column[0] for column in monitor.TABLE_COLUMNS[-3:]),
        )

    def test_navigation_clamps_rows_and_horizontal_width(self):
        self.assertTrue(hasattr(monitor, "DashboardState"))
        state = monitor.DashboardState(selected_row=0)

        for _ in range(30):
            monitor.handle_key(
                state, monitor.curses.KEY_DOWN, 20, 5, 80
            )
        self.assertEqual(19, state.selected_row)
        self.assertEqual(15, state.row_offset)

        for _ in range(100):
            monitor.handle_key(
                state, monitor.curses.KEY_RIGHT, 20, 5, 80
            )
        self.assertEqual(
            max(0, monitor.TABLE_WIDTH - 80), state.column_offset
        )

    def test_refresh_preserves_selected_request(self):
        self.assertTrue(hasattr(monitor, "DashboardState"))
        state = monitor.DashboardState(
            snapshot=self.snapshot("a", "b"), selected_row=1
        )
        outcome = monitor.RefreshOutcome(
            self.snapshot("new", "a", "b"), None, 0
        )

        monitor.apply_outcome(state, outcome)

        self.assertEqual(2, state.selected_row)
        self.assertEqual(datetime(2026, 7, 14, 10, 20), state.last_success)

    def test_apply_outcome_rebuilds_backend_dashboard_data_once(self):
        state = monitor.DashboardState(snapshot=self.snapshot("old"))
        outcome = monitor.RefreshOutcome(self.snapshot("new"), None, 0)

        with mock.patch.object(
            dashboard_module,
            "load_proxy_request_total",
            return_value=41,
        ) as load_total, mock.patch.object(
            dashboard_module,
            "refresh_dashboard_data",
            wraps=dashboard_module.refresh_dashboard_data,
        ) as refresh_data:
            monitor.apply_outcome(state, outcome)

        load_total.assert_called_once()
        refresh_data.assert_called_once_with(state, proxy_request_total=41)
        self.assertEqual(41, state.proxy_request_total)

    def test_summary_shows_per_account_lines_with_rolling_and_subs(self):
        self.assertTrue(hasattr(monitor, "build_summary_lines"))
        rolling = {
            "enabled": False,
            "disabledReason": "套餐未配置规则",
            "rule": None,
            "window": None,
        }
        state = monitor.DashboardState(
            snapshot=self.snapshot("a", rolling=rolling),
            next_refresh_at=70.0,
        )

        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )

        self.assertEqual(1, len(lines))
        self.assertIn("账号 1/1", lines[0])
        self.assertIn("记录 1 (当天)", lines[0])
        self.assertIn("套餐未配置规则", lines[0])
        self.assertIn("Codex Pro", lines[0])
        self.assertIn("到期", lines[0])

    def test_summary_reads_precomputed_account_counts(self):
        snapshot = self.snapshot("a")
        record = dict(snapshot.codex["recentRecords"][0])
        record["_account_email"] = "test@example.com"
        state = monitor.DashboardState(
            snapshot=snapshot,
            all_records=[record],
            next_refresh_at=70.0,
        )

        class FrontendMustNotIterateRecords(list):
            def __iter__(self):
                raise AssertionError("frontend iterated backend records")

        state.all_records = FrontendMustNotIterateRecords(state.all_records)
        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )

        self.assertIn("记录 1 (当天)", lines[0])

    def test_summary_displays_count_usage_without_billing_heading(self):
        snapshot = self.snapshot("a")
        new_subs = {
            "subTypeName": "Count Plan",
            "billingType": "count",
            "usedCount": 7,
            "limit": 200,
            "remainingCount": 193,
            "periodResetTime": "2026-07-15T00:00:00",
            "expireTime": "2026-08-01T00:00:00",
        }
        snapshot.codex["subscriptions"] = new_subs
        snapshot.per_account[0]["subscriptions"] = new_subs
        state = monitor.DashboardState(snapshot=snapshot)

        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )

        self.assertIn("Count Plan", lines[0])
        self.assertIn("到期", lines[0])
        self.assertIn("08-01", lines[0])

    def test_summary_displays_active_rolling_window(self):
        self.assertTrue(hasattr(monitor, "build_summary_lines"))
        rolling = {
            "enabled": True,
            "disabledReason": "",
            "rule": {"windowHours": 5, "limitUsd": 20},
            "window": {
                "usedUsd": 4,
                "remainingUsd": 16,
                "requestCount": 9,
                "isLimited": True,
                "releaseAt": "2026-07-14T11:00:00",
            },
        }
        state = monitor.DashboardState(
            snapshot=self.snapshot("a", rolling=rolling)
        )

        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )

        self.assertIn("5h", lines[0])
        self.assertIn("$4.00000", lines[0])
        self.assertIn("请求 9", lines[0])
        self.assertIn("解除 07-14 11:00:00", lines[0])

    def test_navigation_accounts_for_current_account_summary_row(self):
        class CurrentAccountPool:
            def account_count(self):
                return 1

            def current_email(self):
                return "test@example.com"

        state = monitor.DashboardState(
            snapshot=self.snapshot("a"),
            all_records=[{"requestId": str(index)} for index in range(20)],
            source_pool=CurrentAccountPool(),
        )

        monitor.handle_key_for_screen(
            state, monitor.curses.KEY_NPAGE, (30, 120)
        )

        self.assertEqual(11, state.selected_row)


class FakeWindow:
    def __init__(self, rows, columns):
        self.rows = rows
        self.columns = columns
        self.writes = []
        self.refreshes = []
        self.erased = False

    def getmaxyx(self):
        return self.rows, self.columns

    def erase(self):
        self.erased = True

    def addnstr(self, *args):
        self.writes.append(args)

    def noutrefresh(self, *args):
        self.refreshes.append(args)


class StrictPad(FakeWindow):
    def noutrefresh(
        self,
        pminrow,
        pmincol,
        sminrow,
        smincol,
        smaxrow,
        smaxcol,
    ):
        visible_width = smaxcol - smincol + 1
        if pmincol + visible_width > self.columns:
            raise AssertionError("pad viewport exceeds pad width")
        super().noutrefresh(
            pminrow,
            pmincol,
            sminrow,
            smincol,
            smaxrow,
            smaxcol,
        )


class RendererTests(unittest.TestCase):
    @staticmethod
    def state(record_count=20, failed_index=None):
        records = []
        for index in range(record_count):
            failed = index == failed_index
            records.append(
                {
                    "requestId": str(index),
                    "requestTime": f"2026-07-14T10:{index:02d}:00",
                    "model": "gpt-5-codex",
                    "inputTokens": 1000,
                    "outputTokens": 200,
                    "cacheInputTokens": 300,
                    "cacheWriteTokens": 0,
                    "reasoningTokens": 50,
                    "totalTokens": 1550,
                    "rawCost": 0.1,
                    "discountRate": 0.8,
                    "discountAmount": 0.02,
                    "cost": 0.08,
                    "ip": "127.0.0.1",
                    "responseTime": 1200,
                    "firstByteTime": 300,
                    "status": "failed" if failed else "success",
                    "errorMessage": "complete server error" if failed else "",
                }
            )
        snapshot = monitor.DashboardSnapshot(
            {
                "subscriptions": None,
                "currentUsage": None,
                "recentRecords": records,
            },
            None,
            None,
            datetime(2026, 7, 14, 10, 20),
            per_account=[],
        )
        return monitor.DashboardState(
            snapshot=snapshot, last_success=snapshot.fetched_at
        )

    def test_layout_reserves_fixed_regions(self):
        self.assertTrue(hasattr(monitor, "layout_for_size"))

        layout = monitor.layout_for_size(30, 120)

        self.assertEqual(1, layout.header_rows)
        self.assertEqual(16, layout.table_header_y)
        self.assertEqual(17, layout.records_y)
        self.assertEqual(29, layout.footer_y)
        self.assertEqual(12, layout.visible_rows)
        self.assertEqual(6, layout.stats_rows)
        self.assertEqual(9, layout.chart_rows)
        self.assertEqual(7, layout.chart_y)

    def test_render_places_chart_at_reserved_layout_row(self):
        screen = FakeWindow(30, 120)

        with mock.patch.object(
            monitor.curses,
            "newpad",
            side_effect=lambda rows, columns: FakeWindow(rows, columns),
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses,
            "color_pair",
            return_value=0,
        ), mock.patch.object(
            dashboard_module,
            "render_token_chart",
            return_value=["chart-row-0", "chart-row-1"],
        ):
            monitor.render_dashboard(
                screen,
                self.state(),
                datetime(2026, 7, 14, 10, 20),
                0.0,
            )

        chart_writes = [
            write for write in screen.writes if write[2].startswith("chart-row")
        ]
        self.assertEqual([7, 8], [write[0] for write in chart_writes])

    def test_small_terminal_draws_size_warning_without_pads(self):
        self.assertTrue(hasattr(monitor, "render_dashboard"))
        screen = FakeWindow(5, 40)

        with mock.patch.object(monitor.curses, "newpad") as newpad, mock.patch.object(
            monitor.curses, "doupdate"
        ):
            monitor.render_dashboard(
                screen, self.state(), datetime(2026, 7, 14, 10, 20), 0.0
            )

        self.assertFalse(newpad.called)
        self.assertTrue(
            any("终端太小" in str(write) for write in screen.writes)
        )

    def test_render_ignores_newpad_error_during_resize(self):
        screen = FakeWindow(30, 120)

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=monitor.curses.error
        ), mock.patch.object(monitor.curses, "doupdate"):
            monitor.render_dashboard(
                screen, self.state(), datetime(2026, 7, 14, 10, 20), 0.0
            )

    def test_render_ignores_doupdate_error_during_terminal_close(self):
        screen = FakeWindow(5, 40)

        with mock.patch.object(
            monitor.curses, "doupdate", side_effect=monitor.curses.error
        ):
            monitor.render_dashboard(
                screen, self.state(), datetime(2026, 7, 14, 10, 20), 0.0
            )

    def test_render_uses_precomputed_token_buckets(self):
        screen = FakeWindow(30, 120)
        state = self.state()

        with mock.patch.object(
            monitor.curses,
            "newpad",
            side_effect=lambda rows, columns: FakeWindow(rows, columns),
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses,
            "color_pair",
            return_value=0,
        ), mock.patch.object(
            dashboard_module,
            "build_token_chart",
            side_effect=AssertionError("frontend aggregated raw records"),
        ):
            monitor.render_dashboard(
                screen,
                state,
                datetime(2026, 7, 14, 10, 20),
                0.0,
            )

        screen_text = "".join(str(write[2]) for write in screen.writes)
        self.assertIn("峰值", screen_text)

    def test_render_uses_shared_horizontal_and_record_vertical_offsets(self):
        self.assertTrue(hasattr(monitor, "render_dashboard"))
        screen = FakeWindow(30, 80)
        state = self.state()
        state.selected_row = 10
        state.row_offset = 6
        state.column_offset = 8
        pads = []

        def make_pad(rows, columns):
            pad = FakeWindow(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", return_value=0
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        self.assertEqual(2, len(pads))
        self.assertEqual((0, 8, 16, 0, 16, 78), pads[0].refreshes[-1])
        self.assertEqual(12, pads[1].rows)
        self.assertEqual(12, len(pads[1].writes))
        self.assertEqual((0, 8, 17, 0, 28, 78), pads[1].refreshes[-1])

    def test_failure_footer_contains_complete_error(self):
        self.assertTrue(hasattr(monitor, "footer_text"))
        state = self.state(failed_index=0)

        self.assertIn("complete server error", monitor.footer_text(state))

    def test_long_failure_can_scroll_to_the_end(self):
        self.assertTrue(hasattr(monitor, "footer_view"))
        state = self.state(failed_index=0)
        long_error = "failure-detail-" * 40 + "TAIL-MARKER"
        state.snapshot.codex["recentRecords"][0][
            "errorMessage"
        ] = long_error

        for _ in range(100):
            monitor.handle_key(
                state, monitor.curses.KEY_RIGHT, 20, 5, 79
            )
        visible = monitor.footer_view(state, 79)

        self.assertIn("TAIL-MARKER", visible)
        self.assertLessEqual(monitor.display_width(visible), 79)

    def test_apply_outcome_ingests_new_records_into_store(self):
        self.assertTrue(hasattr(monitor, "apply_outcome"))
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile

        log_dir = tempfile.mkdtemp(dir="test")
        store = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        store.ingest([{"requestId": "a", "requestTime": "2026-07-14T10:00:00"}])
        snap_a = monitor.DashboardSnapshot(
            {"recentRecords": [{"requestId": "a", "requestTime": "2026-07-14T10:00:00"}]},
            None, None, datetime(2026, 7, 14, 10, 0),
        )
        state = monitor.DashboardState(snapshot=snap_a, all_records=store.all_records())
        outcome = monitor.RefreshOutcome(
            monitor.DashboardSnapshot(
                {"recentRecords": [{"requestId": "b", "requestTime": "2026-07-14T10:01:00"}]},
                None, None, datetime(2026, 7, 14, 10, 1),
            ),
            None, 0,
        )

        monitor.apply_outcome(state, outcome, store=store)

        self.assertEqual(2, len(state.all_records))
        self.assertEqual(2, len(store.all_records()))

    def test_apply_outcome_starts_with_store_seed(self):
        self.assertTrue(hasattr(monitor, "apply_outcome"))
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile

        log_dir = tempfile.mkdtemp(dir="test")
        store = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        store.ingest([{"requestId": "seed", "requestTime": "2026-07-14T09:00:00"}])
        state = monitor.DashboardState(all_records=[])

        monitor.apply_outcome(state, monitor.RefreshOutcome(None, None, 0), store=store)

        self.assertEqual(1, len(state.all_records))
        self.assertEqual("seed", state.all_records[0]["requestId"])

    def test_long_error_keeps_table_pads_valid_and_error_colored(self):
        screen = FakeWindow(30, 80)
        state = self.state(failed_index=0)
        state.snapshot.codex["recentRecords"][0][
            "errorMessage"
        ] = "error-" * 80 + "TAIL-MARKER"
        for _ in range(100):
            monitor.handle_key(
                state, monitor.curses.KEY_RIGHT, 20, 4, 79
            )
        pads = []

        def make_pad(rows, columns):
            pad = StrictPad(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", side_effect=lambda pair: pair * 100
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        footer_write = next(
            write for write in screen.writes if write[0] == 29
        )
        self.assertIn("TAIL-MARKER", footer_write[2])
        self.assertEqual(100, footer_write[-1])
        self.assertLessEqual(
            pads[0].refreshes[-1][1], max(0, monitor.TABLE_WIDTH - 79)
        )

    def test_unknown_status_uses_neutral_color(self):
        screen = FakeWindow(30, 80)
        state = self.state(record_count=2)
        state.snapshot.codex["recentRecords"][0]["status"] = "unknown"
        state.selected_row = 1
        pads = []

        def make_pad(rows, columns):
            pad = FakeWindow(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", side_effect=lambda pair: pair * 100
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        self.assertEqual(0, pads[1].writes[0][-1])


class InteractiveWindow(FakeWindow):
    def __init__(self, rows, columns, keys):
        super().__init__(rows, columns)
        self.keys = list(keys)
        self.timeout_value = None
        self.keypad_value = None

    def timeout(self, value):
        self.timeout_value = value

    def keypad(self, value):
        self.keypad_value = value

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")


class EventLoopTests(unittest.TestCase):
    def test_run_dashboard_starts_refresh_and_stops_worker_on_q(self):
        self.assertTrue(hasattr(monitor, "run_dashboard"))
        screen = InteractiveWindow(30, 120, [ord("q")])

        class FakeWorker:
            def __init__(self):
                self.started = False
                self.stopped = False
                self.request_calls = 0

            def start(self):
                self.started = True

            def request_refresh(self):
                self.request_calls += 1
                return True

            def get_result(self):
                return None

            def stop(self):
                self.stopped = True

        worker = FakeWorker()
        with mock.patch.object(runtime_module, "render_dashboard"):
            monitor.run_dashboard(screen, worker_factory=lambda: worker)

        self.assertTrue(worker.started)
        self.assertTrue(worker.stopped)
        self.assertEqual(1, worker.request_calls)
        self.assertEqual(100, screen.timeout_value)
        self.assertTrue(screen.keypad_value)

    def test_run_dashboard_does_not_redraw_on_idle_input_polls(self):
        screen = InteractiveWindow(30, 120, [-1, -1, ord("q")])

        class FakeWorker:
            def start(self):
                return None

            def request_refresh(self):
                return True

            def get_result(self):
                return None

            def stop(self):
                return None

        with tempfile.TemporaryDirectory(dir="test") as temp_dir, mock.patch.object(
            runtime_module,
            "LOG_DIR",
            temp_dir,
        ), mock.patch.object(
            runtime_module.time,
            "monotonic",
            side_effect=[100.0, 100.1, 100.2, 100.3],
        ), mock.patch.object(
            runtime_module,
            "render_dashboard",
        ) as render:
            monitor.run_dashboard(screen, worker_factory=FakeWorker)

        self.assertEqual(1, render.call_count)

    def test_run_dashboard_handles_resize_before_navigation(self):
        screen = InteractiveWindow(
            30, 120, [monitor.curses.KEY_RESIZE, ord("q")]
        )

        class FakeWorker:
            def start(self):
                return None

            def request_refresh(self):
                return True

            def get_result(self):
                return None

            def stop(self):
                return None

        with tempfile.TemporaryDirectory(dir="test") as temp_dir, mock.patch.object(
            runtime_module,
            "LOG_DIR",
            temp_dir,
        ), mock.patch.object(
            runtime_module,
            "render_dashboard",
        ), mock.patch.object(
            runtime_module,
            "handle_key_for_screen",
        ) as handle_key, mock.patch.object(
            runtime_module.curses,
            "update_lines_cols",
        ) as update_lines_cols:
            handle_key.side_effect = ["quit"]
            monitor.run_dashboard(screen, worker_factory=FakeWorker)

        update_lines_cols.assert_called_once_with()
        self.assertEqual(1, handle_key.call_count)
        self.assertEqual(ord("q"), handle_key.call_args.args[1])

    def test_main_rejects_non_interactive_output(self):
        self.assertTrue(hasattr(monitor, "run_dashboard"))
        fake_stdin = mock.Mock()
        fake_stdout = mock.Mock()
        fake_stdin.isatty.return_value = False
        fake_stdout.isatty.return_value = False
        stderr = io.StringIO()

        with mock.patch.object(runtime_module.sys, "stdin", fake_stdin), mock.patch.object(
            runtime_module.sys, "stdout", fake_stdout
        ), mock.patch.object(runtime_module.sys, "stderr", stderr):
            with self.assertRaises(SystemExit) as raised:
                monitor.main()

        self.assertEqual(2, raised.exception.code)
        self.assertIn("交互式终端", stderr.getvalue())


class RuntimeTests(unittest.TestCase):
    def test_parse_args_supports_alternate_proxy_and_no_takeover(self):
        self.assertTrue(hasattr(monitor, "parse_args"))

        args = monitor.parse_args(
            [
                "--proxy-port",
                "15872",
                "test/accounts.toml",
                "--no-apply-codex-config",
            ]
        )

        self.assertEqual(15872, args.proxy_port)
        self.assertEqual("test/accounts.toml", args.accounts_file)
        self.assertFalse(args.apply_codex_config)

    def test_parse_args_uses_script_directory_default_and_optional_path(self):
        default_args = monitor.parse_args([])
        explicit_args = monitor.parse_args(["/secure/accounts.toml"])

        self.assertIsNone(default_args.accounts_file)
        self.assertEqual("/secure/accounts.toml", explicit_args.accounts_file)
        self.assertEqual(
            Path(monitor.__file__).resolve().with_name("accounts.toml"),
            monitor.DEFAULT_ACCOUNTS_FILE,
        )

    def test_runtime_factory_shares_one_source_pool(self):
        self.assertTrue(hasattr(monitor, "build_runtime"))
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            args = SimpleNamespace(
                proxy_port=15872,
                upstream_url="http://127.0.0.1:1",
                key_cache=str(Path(temp_dir) / "keys.json"),
                codex_config=str(Path(temp_dir) / "config.toml"),
                accounts_file="test/accounts.toml",
                apply_codex_config=False,
            )
            with mock.patch.object(
                runtime_module,
                "load_accounts",
                return_value=[{"email": "one@example.com", "password": "secret"}],
            ) as load_accounts, mock.patch.object(
                runtime_module,
                "load_proxy_config",
                return_value=None,
            ) as load_proxy_config:
                runtime = monitor.build_runtime(args)

            load_accounts.assert_called_once_with("test/accounts.toml")
            load_proxy_config.assert_called_once_with("test/accounts.toml")

            self.assertIs(runtime.source_pool, runtime.proxy.source_pool)
            self.assertIs(
                runtime.source_pool,
                runtime.worker.engine.client.source_pool,
            )

    def test_runtime_factory_uses_default_accounts_path_when_argument_is_missing(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            args = SimpleNamespace(
                proxy_port=15872,
                upstream_url="http://127.0.0.1:1",
                key_cache=str(Path(temp_dir) / "keys.json"),
                codex_config=str(Path(temp_dir) / "config.toml"),
                accounts_file=None,
                apply_codex_config=False,
            )
            with mock.patch.object(
                runtime_module,
                "DEFAULT_ACCOUNTS_FILE",
                Path(temp_dir) / "accounts.toml",
            ), mock.patch.object(
                runtime_module,
                "load_accounts",
                return_value=[{"email": "one@example.com", "password": "secret"}],
            ) as load_accounts, mock.patch.object(
                runtime_module,
                "load_proxy_config",
                return_value=None,
            ) as load_proxy_config:
                monitor.build_runtime(args)

            load_accounts.assert_called_once_with(
                Path(temp_dir) / "accounts.toml"
            )
            load_proxy_config.assert_called_once_with(
                Path(temp_dir) / "accounts.toml"
            )

    def test_default_worker_factory_passes_configured_proxy_to_clients(self):
        accounts = [{"email": "one@example.com", "password": "secret"}]
        proxy = monitor.ProxyConfig(socks="127.0.0.1:1080")
        with mock.patch.object(
            runtime_module, "load_accounts", return_value=accounts
        ) as load_accounts, mock.patch.object(
            runtime_module, "load_proxy_config", return_value=proxy
        ) as load_proxy_config, mock.patch.object(
            runtime_module, "_require_socks"
        ) as require_socks:
            worker = monitor.default_worker_factory()

        try:
            client = worker.engine.client.clients[0]
            self.assertIs(proxy, client.proxy)
            self.assertEqual(proxy.requests_proxies(), client.session.proxies)
            load_accounts.assert_called_once_with(monitor.DEFAULT_ACCOUNTS_FILE)
            load_proxy_config.assert_called_once_with(
                monitor.DEFAULT_ACCOUNTS_FILE
            )
            require_socks.assert_called_once_with(proxy)
        finally:
            for client in worker.engine.client.clients:
                client.close()

    def test_runtime_smoke_uses_alternate_port_without_config_takeover(self):
        self.assertTrue(hasattr(monitor, "MonitorRuntime"))
        upstream = ScriptedUpstream(
            [
                (
                    402,
                    {"content-type": "application/json"},
                    b'{"error":"quota exhausted"}',
                ),
                (
                    200,
                    {"content-type": "application/json"},
                    b'{"ok":true}',
                ),
            ]
        )
        pool = monitor.SourcePool(
            [
                {"email": "one@example.com", "password": "p1"},
                {"email": "two@example.com", "password": "p2"},
            ],
            keys={"one@example.com": "key-1", "two@example.com": "key-2"},
        )
        worker = mock.Mock()
        worker.request_refresh.return_value = False
        config = mock.Mock()
        proxy = monitor.RawChatProxyServer(
            pool,
            upstream.base_url,
            port=0,
        )
        runtime = monitor.MonitorRuntime(
            worker,
            proxy,
            config,
            apply_codex_config=False,
        )
        runtime.start()
        try:
            body = b'{"model":"gpt-5.4","input":"smoke"}'
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"Authorization": "Bearer local-placeholder"},
                data=body,
                timeout=5,
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"ok": True}, response.json())
            self.assertEqual(
                ["Bearer key-1", "Bearer key-2"],
                [item["authorization"] for item in upstream.requests],
            )
            self.assertEqual([body, body], [item["body"] for item in upstream.requests])
            config.apply.assert_not_called()
            config.restore_if_unchanged.assert_not_called()
        finally:
            runtime.stop()
            upstream.stop()

        second_proxy = monitor.RawChatProxyServer(
            pool,
            upstream.base_url,
            port=0,
        )
        second_proxy.start()
        second_proxy.stop()

    def test_runtime_stop_stops_components_without_changing_config(self):
        self.assertTrue(hasattr(monitor, "MonitorRuntime"))
        worker = mock.Mock()
        proxy = mock.Mock()
        config = mock.Mock()
        runtime = monitor.MonitorRuntime(
            worker,
            proxy,
            config,
            apply_codex_config=True,
        )
        runtime._config_applied = True

        runtime.stop()
        runtime.stop()

        worker.stop.assert_called_once_with()
        proxy.stop.assert_called_once_with()
        config.restore_if_unchanged.assert_not_called()

    def test_proxy_bind_failure_does_not_apply_codex_config(self):
        self.assertTrue(hasattr(monitor, "MonitorRuntime"))
        worker = mock.Mock()
        proxy = mock.Mock()
        proxy.start.side_effect = OSError("port occupied")
        config = mock.Mock()
        runtime = monitor.MonitorRuntime(
            worker,
            proxy,
            config,
            apply_codex_config=True,
        )

        with self.assertRaises(OSError):
            runtime.start()

        worker.start.assert_not_called()
        config.apply.assert_not_called()

    def test_config_apply_error_becomes_dashboard_error(self):
        snapshot = monitor.DashboardSnapshot(
            {"recentRecords": []}, None, None, datetime(2026, 7, 14, 10, 0)
        )
        outcome = monitor.RefreshOutcome(snapshot, None, 0)
        worker = mock.Mock()
        worker.get_result.side_effect = [outcome, None]
        proxy = mock.Mock()
        config = mock.Mock()
        config.apply.side_effect = PermissionError("config is read-only")
        runtime = monitor.MonitorRuntime(
            worker, proxy, config, apply_codex_config=True
        )
        state = monitor.DashboardState()

        self.assertTrue(
            runtime_module.drain_refresh_results(
                worker, state, on_outcome=runtime.handle_outcome
            )
        )

        self.assertIn("配置接管失败", state.error)
        self.assertIn("read-only", state.error)


class RecordStoreTests(unittest.TestCase):
    def _tmp_log(self, tmp_path=None):
        import tempfile

        if tmp_path is None:
            tmp_path = tempfile.mkdtemp(dir="test")
        return tmp_path

    def test_dedup_key_is_stable_for_same_fields(self):
        self.assertTrue(hasattr(monitor, "record_key"))
        a = {"requestId": "x", "requestTime": "2026-07-14T10:00:00", "cost": 1}
        b = {"requestId": "x", "requestTime": "2026-07-14T10:00:00", "cost": 1}
        self.assertEqual(monitor.record_key(a), monitor.record_key(b))

    def test_dedup_key_differs_on_any_field_change(self):
        self.assertTrue(hasattr(monitor, "record_key"))
        a = {"requestId": "x", "requestTime": "2026-07-14T10:00:00"}
        b = {"requestId": "x", "requestTime": "2026-07-14T10:00:01"}
        self.assertNotEqual(monitor.record_key(a), monitor.record_key(b))

    def test_ingest_appends_only_new_records_to_daily_log(self):
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile
        import os

        log_dir = tempfile.mkdtemp(dir="test")
        store = monitor.RecordStore(log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0))

        first = store.ingest(
            [
                {"requestId": "1", "requestTime": "2026-07-14T10:00:00"},
                {"requestId": "2", "requestTime": "2026-07-14T10:01:00"},
            ]
        )
        self.assertEqual(2, len(first))
        second = store.ingest(
            [
                {"requestId": "1", "requestTime": "2026-07-14T10:00:00"},
                {"requestId": "3", "requestTime": "2026-07-14T10:02:00"},
            ]
        )
        self.assertEqual(1, len(second))
        self.assertEqual("3", second[0]["requestId"])

        log_path = store.log_path()
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(3, len(lines))

    def test_store_loads_todays_records_on_startup(self):
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile

        log_dir = tempfile.mkdtemp(dir="test")
        first = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        first.ingest(
            [
                {"requestId": "1", "requestTime": "2026-07-14T10:00:00"},
                {"requestId": "2", "requestTime": "2026-07-14T10:01:00"},
            ]
        )

        reopened = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 18, 0)
        )
        self.assertEqual(2, len(reopened.all_records()))
        self.assertTrue(reopened.seen(monitor.record_key({"requestId": "1", "requestTime": "2026-07-14T10:00:00"})))

    def test_store_ignores_other_day_files(self):
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile
        import os

        log_dir = tempfile.mkdtemp(dir="test")
        other_path = os.path.join(log_dir, "rawchat_codex_2026-07-13.jsonl")
        with open(other_path, "w", encoding="utf-8") as handle:
            handle.write('{"requestId": "old", "requestTime": "2026-07-13T10:00:00"}\n')
        store = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        self.assertEqual(0, len(store.all_records()))

    def test_store_orders_newest_first(self):
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile

        log_dir = tempfile.mkdtemp(dir="test")
        store = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        store.ingest(
            [
                {"requestId": "old", "requestTime": "2026-07-14T09:00:00"},
                {"requestId": "new", "requestTime": "2026-07-14T11:00:00"},
            ]
        )
        ordered = store.all_records()
        self.assertEqual("new", ordered[0]["requestId"])
        self.assertEqual("old", ordered[-1]["requestId"])

    def test_store_keeps_newest_first_after_reload(self):
        self.assertTrue(hasattr(monitor, "RecordStore"))
        import tempfile

        log_dir = tempfile.mkdtemp(dir="test")
        first = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 12, 0)
        )
        first.ingest(
            [
                {"requestId": "a", "requestTime": "2026-07-14T08:00:00"},
                {"requestId": "c", "requestTime": "2026-07-14T10:00:00"},
            ]
        )

        second = monitor.RecordStore(
            log_dir=log_dir, now=lambda: datetime(2026, 7, 14, 18, 0)
        )
        second.ingest(
            [{"requestId": "b", "requestTime": "2026-07-14T09:00:00"}]
        )
        ordered = second.all_records()
        self.assertEqual(
            ["c", "b", "a"], [r["requestId"] for r in ordered]
        )


class HistoryViewTests(unittest.TestCase):
    @staticmethod
    def state_with_history(count, all_records):
        snapshot = monitor.DashboardSnapshot(
            {
                "subscriptions": None,
                "currentUsage": None,
                "recentRecords": [
                    {"requestId": str(i), "requestTime": f"2026-07-14T10:{i:02d}:00"}
                    for i in range(count)
                ],
            },
            None,
            None,
            datetime(2026, 7, 14, 10, 20),
            per_account=[],
        )
        return monitor.DashboardState(
            snapshot=snapshot, all_records=list(all_records)
        )

    def test_summary_shows_total_all_records_count(self):
        self.assertTrue(hasattr(monitor, "build_summary_lines"))
        history = [
            {"requestId": str(i)} for i in range(150)
        ]
        state = self.state_with_history(20, history)
        state.last_success = datetime(2026, 7, 14, 10, 20)
        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )
        self.assertIn("记录 150 (当天)", lines[0])

    def test_summary_falls_back_to_snapshot_count_without_history(self):
        self.assertTrue(hasattr(monitor, "build_summary_lines"))
        state = self.state_with_history(5, [])
        lines = monitor.build_summary_lines(
            state,
            wall_now=datetime(2026, 7, 14, 10, 20),
            monotonic_now=10.0,
        )
        self.assertIn("记录 5 (当天)", lines[0])

    def test_render_draws_scrollbar_when_history_exceeds_visible(self):
        self.assertTrue(hasattr(monitor, "render_dashboard"))
        screen = FakeWindow(30, 120)
        history = [
            {"requestId": str(i), "requestTime": f"2026-07-14T10:{i:02d}:00"}
            for i in range(100)
        ]
        state = self.state_with_history(20, history)
        pads = []

        def make_pad(rows, columns):
            pad = FakeWindow(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", return_value=0
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        scroll_chars = [
            write[2]
            for write in screen.writes
            if write[0] >= 5 and write[1] == 119
        ]
        self.assertTrue(any(ch in ("█", "│") for ch in scroll_chars))

    def test_render_submits_screen_once_without_overwriting_pads(self):
        screen = FakeWindow(30, 120)
        history = [
            {"requestId": str(i), "requestTime": f"2026-07-14T10:{i:02d}:00"}
            for i in range(100)
        ]
        state = self.state_with_history(20, history)
        pads = []

        def make_pad(rows, columns):
            pad = FakeWindow(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", return_value=0
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        self.assertEqual(1, len(screen.refreshes))
        self.assertEqual(118, pads[0].refreshes[-1][-1])
        self.assertEqual(118, pads[1].refreshes[-1][-1])

    def test_render_uses_visible_slice_of_history_table(self):
        self.assertTrue(hasattr(monitor, "render_dashboard"))
        screen = FakeWindow(30, 120)
        history = [
            {"requestId": f"h{i}", "requestTime": f"2026-07-14T09:{i:02d}:00"}
            for i in range(50)
        ]
        state = self.state_with_history(20, history)
        state.selected_row = 49
        state.row_offset = 38
        pads = []

        def make_pad(rows, columns):
            pad = FakeWindow(rows, columns)
            pads.append(pad)
            return pad

        with mock.patch.object(
            monitor.curses, "newpad", side_effect=make_pad
        ), mock.patch.object(monitor.curses, "doupdate"), mock.patch.object(
            monitor.curses, "color_pair", return_value=0
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        record_pad = pads[1]
        rendered = "".join(str(write[2]) for write in record_pad.writes)
        self.assertNotIn("09:00:00", rendered)
        self.assertIn("09:38:00", rendered)
        self.assertIn("09:49:00", rendered)

    def test_render_shows_stats_and_chart_above_table(self):
        self.assertTrue(hasattr(monitor, "render_dashboard"))
        screen = FakeWindow(30, 120)
        history = [
            {
                "requestId": f"h{i}",
                "requestTime": f"2026-07-14T09:{i:02d}:00",
                "inputTokens": 1000,
                "outputTokens": 200,
                "cacheInputTokens": 300,
                "totalTokens": 1500,
                "ip": "1.2.3.4",
                "status": "success",
            }
            for i in range(50)
        ]
        state = self.state_with_history(20, history)
        with mock.patch.object(monitor.curses, "newpad"), mock.patch.object(
            monitor.curses, "doupdate"
        ), mock.patch.object(
            monitor.curses, "color_pair", return_value=0
        ):
            monitor.render_dashboard(
                screen, state, datetime(2026, 7, 14, 10, 20), 0.0
            )

        screen_text = "".join(str(write[2]) for write in screen.writes)
        self.assertIn("缓存命中 23.1%", screen_text)
        self.assertIn("峰值", screen_text)
        self.assertIn("1.2.3.4", screen_text)


RECORD_FIELDS = (
    "requestTime", "model", "inputTokens", "outputTokens", "cacheInputTokens",
    "cacheWriteTokens", "reasoningTokens", "totalTokens", "rawCost",
    "discountRate", "discountAmount", "cost", "ip", "responseTime",
    "firstByteTime", "status",
)


def make_record(**overrides):
    record = {
        "requestTime": "2026-07-14T10:00:00",
        "model": "gpt-5-codex",
        "inputTokens": 1000,
        "outputTokens": 200,
        "cacheInputTokens": 300,
        "cacheWriteTokens": 0,
        "reasoningTokens": 50,
        "totalTokens": 1550,
        "rawCost": 0.1,
        "discountRate": 0.8,
        "discountAmount": 0.02,
        "cost": 0.08,
        "ip": "1.2.3.4",
        "responseTime": 1200,
        "firstByteTime": 300,
        "status": "success",
    }
    record.update(overrides)
    return record


class StatisticsTests(unittest.TestCase):
    def test_dashboard_state_builds_initial_derived_data(self):
        state = monitor.DashboardState(all_records=[make_record()])

        self.assertEqual(1, len(state.record_lines))
        self.assertEqual(1, state.statistics["by_ip"]["1.2.3.4"]["count"])
        self.assertTrue(state.token_buckets)

    def test_refresh_dashboard_data_builds_derived_snapshot(self):
        records = [
            make_record(ip="10.0.0.1", status="success"),
            make_record(ip="10.0.0.2", status="failed"),
        ]
        records[0]["_account_email"] = "one@example.com"
        records[1]["_account_email"] = "two@example.com"
        state = monitor.DashboardState(all_records=records)

        dashboard_module.refresh_dashboard_data(
            state,
            proxy_request_total=17,
        )

        self.assertEqual(17, state.proxy_request_total)
        self.assertEqual(
            2,
            state.statistics["by_ip"]["10.0.0.1"]["count"]
            + state.statistics["by_ip"]["10.0.0.2"]["count"],
        )
        self.assertEqual(
            {"one@example.com": 1, "two@example.com": 1},
            state.account_record_counts,
        )
        self.assertEqual(2, len(state.record_lines))
        self.assertTrue(state.token_buckets)

    def test_load_proxy_request_total_counts_requests_only(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            log_path = Path(temp_dir) / "rawchat_proxy_2026-07-14.jsonl"
            log_path.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"event": "request_received"},
                        {"event": "upstream_response"},
                        {"event": "request_received"},
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            total = dashboard_module.load_proxy_request_total(
                temp_dir,
                datetime(2026, 7, 14, 12, 0),
            )

        self.assertEqual(2, total)

    def test_compute_statistics_aggregates_tokens_and_cache(self):
        self.assertTrue(hasattr(monitor, "compute_statistics"))
        records = [
            make_record(
                inputTokens=1000, cacheInputTokens=300, cacheWriteTokens=100,
                outputTokens=200, totalTokens=1600, status="success",
            ),
            make_record(
                inputTokens=1000, cacheInputTokens=0, cacheWriteTokens=0,
                outputTokens=200, totalTokens=2000, status="failed",
            ),
        ]
        stats = monitor.compute_statistics(records)

        self.assertEqual(2800, stats["total_tokens"])
        # 输入口径 = non-cached(inputTokens) + cached(cacheInputTokens)
        self.assertEqual(2300, stats["input_tokens"])
        self.assertEqual(400, stats["output_tokens"])
        # 缓存命中率 = cacheReadTokens / (freshInput + cacheReadTokens + cacheWriteTokens)
        # freshInput = input_tokens - cache_read_tokens = 2300 - 300 = 2000
        # cacheable_input = 2000 + 300 + 100 = 2400
        # cache_hit_rate = 300 / 2400 = 0.125
        self.assertAlmostEqual(0.125, stats["cache_hit_rate"], places=6)

    def test_compute_statistics_empty_returns_zeros(self):
        self.assertTrue(hasattr(monitor, "compute_statistics"))
        stats = monitor.compute_statistics([])
        self.assertEqual(0, stats["total_tokens"])
        self.assertEqual(0, stats["input_tokens"])
        self.assertEqual(0, stats["cache_hit_rate"])
        self.assertEqual({}, stats["by_ip"])

    def test_compute_statistics_per_ip_metrics(self):
        self.assertTrue(hasattr(monitor, "compute_statistics"))
        records = [
            make_record(ip="10.0.0.1", responseTime=1000, firstByteTime=200, status="success"),
            make_record(ip="10.0.0.1", responseTime=3000, firstByteTime=400, status="success"),
            make_record(ip="10.0.0.2", responseTime=2000, firstByteTime=100, status="failed"),
        ]
        stats = monitor.compute_statistics(records)

        ip1 = stats["by_ip"]["10.0.0.1"]
        self.assertEqual(2, ip1["count"])
        self.assertAlmostEqual(2.0, ip1["avg_response"], places=4)
        self.assertAlmostEqual(0.3, ip1["avg_first_byte"], places=4)
        self.assertAlmostEqual(1.0, ip1["success_rate"], places=4)

        ip2 = stats["by_ip"]["10.0.0.2"]
        self.assertEqual(1, ip2["count"])
        self.assertAlmostEqual(0.0, ip2["success_rate"], places=4)

    def test_stats_lines_only_read_generated_dashboard_data(self):
        state = monitor.DashboardState(all_records=[make_record()])
        dashboard_module.refresh_dashboard_data(
            state,
            proxy_request_total=23,
        )

        with mock.patch.object(
            dashboard_module,
            "compute_statistics",
            side_effect=AssertionError("frontend recomputed backend statistics"),
        ), mock.patch.object(
            Path,
            "open",
            side_effect=AssertionError("frontend opened proxy log"),
        ):
            lines = monitor.build_stats_lines(state, 120)

        self.assertIn("代理请求 23", lines[0])
        self.assertNotIn("代理首字", lines[0])
        self.assertNotIn("代理响应", lines[0])


class CostChartTests(unittest.TestCase):
    def test_render_chart_displays_actual_paid_cost(self):
        buckets = dashboard_module.build_token_buckets(
            [
                make_record(
                    requestTime="2026-07-14T10:00:00",
                    inputTokens=100,
                    cacheInputTokens=50,
                    outputTokens=70,
                    rawCost=9.99,
                    cost=0.08,
                )
            ]
        )

        chart = dashboard_module.render_token_chart(
            buckets,
            bucket_minutes=5,
            width=40,
            height=8,
        )

        self.assertIn("费用峰值 $0.08000", chart[0])
        self.assertTrue(any("$" in line for line in chart[1:]))

    def test_build_chart_buckets_actual_paid_cost_by_five_minutes(self):
        self.assertTrue(hasattr(monitor, "build_token_chart"))
        # 图表口径 = API 返回的实付 cost，不是原价或 token 数。
        records = [
            make_record(
                requestTime="2026-07-14T10:00:00",
                inputTokens=100,
                cacheInputTokens=50,
                outputTokens=70,
                totalTokens=999,
                rawCost=10,
                cost=0.08,
                ip="1.1.1.1",
            ),
            make_record(
                requestTime="2026-07-14T10:02:00",
                inputTokens=200,
                cacheInputTokens=80,
                outputTokens=120,
                totalTokens=999,
                rawCost=12,
                cost=0.12,
                ip="1.1.1.1",
            ),
            make_record(
                requestTime="2026-07-14T10:06:00",
                inputTokens=400,
                cacheInputTokens=100,
                outputTokens=0,
                totalTokens=999,
                rawCost=15,
                cost=0.15,
                ip="1.1.1.1",
            ),
        ]
        chart = monitor.build_token_chart(records, bucket_minutes=5, width=40, height=8)

        self.assertTrue(chart)
        joined = "\n".join(chart)
        # 10:00~10:05 bucket = 0.08 + 0.12 = $0.20000.
        self.assertIn("费用峰值 $0.20000", joined)
        self.assertIn("10:00~10:05", joined)
        self.assertIn("*", joined)

    def test_build_chart_buckets_normalizes_mixed_timezone_records(self):
        buckets = dashboard_module.build_token_buckets(
            [
                make_record(
                    requestTime="2026-07-14T10:00:00", cost=0.08
                ),
                make_record(
                    requestTime="2026-07-14T10:01:00+00:00", cost=0.12
                ),
            ]
        )

        self.assertTrue(buckets)
        self.assertTrue(all(stamp.tzinfo is None for stamp, _ in buckets))

    def test_build_chart_empty_returns_placeholder(self):
        self.assertTrue(hasattr(monitor, "build_token_chart"))
        chart = monitor.build_token_chart([], width=40, height=8)
        self.assertEqual(["暂无图表数据"], chart)


if __name__ == "__main__":
    unittest.main()
