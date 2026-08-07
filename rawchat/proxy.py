"""Local HTTP proxy forwarding requests through an available RawChat source."""

import http.server
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from .config import (
    UPSTREAM_CONNECT_TIMEOUT,
    UPSTREAM_READ_TIMEOUT,
    ProxyConfig,
)
from .records import _log_date, _number
from .sources import (
    SourcePool,
    SourceState,
    _contains_quota_exhausted_message,
    _parse_release_at,
    is_quota_error,
)


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class RawChatProxyServer:
    """将 Codex Responses 请求透传到当前 RawChat source。"""

    def __init__(
        self,
        source_pool: SourcePool,
        upstream_base_url: str,
        host: str = "127.0.0.1",
        port: int = 0,
        event_log_dir: str | os.PathLike[str] | None = None,
        proxy: ProxyConfig | None = None,
    ) -> None:
        self.source_pool = source_pool
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.host = host
        self.port = port
        self.proxy = proxy
        self.event_log_dir = (
            Path(event_log_dir) if event_log_dir is not None else None
        )
        self.base_url: str | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._event_log_lock = threading.Lock()

        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_request(self)

            def do_POST(self) -> None:
                owner._handle_request(self)

            def do_PUT(self) -> None:
                owner._handle_request(self)

            def do_PATCH(self) -> None:
                owner._handle_request(self)

            def do_DELETE(self) -> None:
                owner._handle_request(self)

            def do_OPTIONS(self) -> None:
                owner._handle_request(self)

            def log_message(self, *_args: Any) -> None:
                return

        self._handler_class = Handler

    def _log_event(self, event: str, **fields: Any) -> None:
        if self.event_log_dir is None:
            return
        path = self.event_log_dir / (
            f"rawchat_proxy_{_log_date(datetime.now())}.jsonl"
        )
        payload = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        try:
            self.event_log_dir.mkdir(parents=True, exist_ok=True)
            with self._event_log_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                os.chmod(path, 0o600)
        except OSError:
            return

    def start(self) -> None:
        if self._started:
            raise RuntimeError("代理已经启动过")
        self._started = True
        try:
            server = http.server.ThreadingHTTPServer(
                (self.host, self.port), self._handler_class
            )
            server.daemon_threads = True
            self._server = server
            self.port = server.server_port
            self.base_url = f"http://{self.host}:{self.port}"
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="rawchat-proxy",
                daemon=True,
            )
            self._thread.start()
            self._log_event("proxy_started", port=self.port)
        except Exception:
            if self._server is not None:
                self._server.server_close()
                self._server = None
            self._thread = None
            raise

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=1.0)
        self._server = None
        self._thread = None

    @staticmethod
    def _request_body(handler: http.server.BaseHTTPRequestHandler) -> bytes | None:
        length_text = handler.headers.get("Content-Length")
        if length_text is None:
            if handler.command in {"GET", "HEAD", "OPTIONS"}:
                return b""
            return None
        try:
            length = int(length_text)
        except (TypeError, ValueError):
            return None
        if length < 0:
            return None
        try:
            body = handler.rfile.read(length)
        except Exception:
            handler.close_connection = True
            return None
        return body if len(body) == length else None

    @staticmethod
    def _forward_headers(
        handler: http.server.BaseHTTPRequestHandler,
        source: SourceState,
        body: bytes,
    ) -> dict[str, str]:
        excluded = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "authorization",
        }
        headers = {
            name: value
            for name, value in handler.headers.items()
            if name.lower() not in excluded
        }
        headers["Authorization"] = f"Bearer {source.api_key}"
        headers["Accept-Encoding"] = "identity"
        return headers

    @staticmethod
    def _safe_response_headers(
        headers: requests.structures.CaseInsensitiveDict[str],
        body_length: int | None = None,
    ) -> dict[str, str]:
        safe: dict[str, str] = {}
        for name, value in headers.items():
            lower = name.lower()
            if lower in _HOP_BY_HOP_HEADERS or lower in {
                "content-encoding",
                "content-length",
            }:
                continue
            safe[name] = value
        if body_length is not None:
            safe["Content-Length"] = str(body_length)
        return safe

    @staticmethod
    def _send_buffered(
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        headers: requests.structures.CaseInsensitiveDict[str] | dict[str, str],
        body: bytes,
    ) -> None:
        try:
            handler.send_response(status)
            for name, value in RawChatProxyServer._safe_response_headers(
                headers, len(body)
            ).items():
                handler.send_header(name, value)
            handler.end_headers()
            if body:
                handler.wfile.write(body)
        except Exception:
            handler.close_connection = True

    @staticmethod
    def _send_json(
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        message: str,
    ) -> None:
        body = json.dumps(
            {"error": message}, ensure_ascii=False
        ).encode("utf-8")
        RawChatProxyServer._send_buffered(
            handler, status, {"Content-Type": "application/json"}, body
        )

    @staticmethod
    def _send_stream(
        handler: http.server.BaseHTTPRequestHandler,
        response: requests.Response,
    ) -> tuple[float, bool]:
        content_length = _number(response.headers.get("Content-Length"))
        length = (
            int(content_length)
            if content_length is not None
            and content_length >= 0
            and content_length.is_integer()
            else None
        )
        response_headers = RawChatProxyServer._safe_response_headers(
            response.headers, length
        )
        if length is None:
            response_headers["Connection"] = "close"
            handler.close_connection = True
        bytes_sent = 0
        try:
            handler.send_response(response.status_code)
            for name, value in response_headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            raw = getattr(response, "raw", None)
            read1 = getattr(raw, "read1", None)
            content_consumed = getattr(response, "_content_consumed", False)
            if callable(read1) and not content_consumed:
                while True:
                    chunk = read1(8192, decode_content=True)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    bytes_sent += len(chunk)
                    if length is not None and bytes_sent >= length:
                        break
            else:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
                        bytes_sent += len(chunk)
                        if length is not None and bytes_sent >= length:
                            break
        except Exception:
            handler.close_connection = True
            return time.monotonic(), False
        finally:
            try:
                response.close()
            except Exception:
                pass
        complete = length is None or bytes_sent >= length
        if not complete:
            handler.close_connection = True
        return time.monotonic(), complete

    @staticmethod
    def _error_category(body: bytes) -> str:
        text = body.decode("utf-8", "replace")
        lower_text = text.lower()
        if _contains_quota_exhausted_message(text):
            return "quota"
        if any(
            phrase in text
            for phrase in (
                "rate limit",
                "rate_limit",
                "too many requests",
                "请求频率",
                "请求过于频繁",
            )
        ):
            return "rate_limit"
        if "no available channel" in lower_text:
            return "channel_unavailable"
        if "model price" in lower_text and "temporarily unavailable" in lower_text:
            return "model_unavailable"
        return "unknown"

    @staticmethod
    def _extract_input_fields(body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        model = data.get("model")
        if not isinstance(model, str) or not model:
            return {}
        return {"model": model}

    def _handle_request(
        self, handler: http.server.BaseHTTPRequestHandler
    ) -> None:
        body = self._request_body(handler)
        if body is None:
            self._log_event(
                "invalid_request_body",
                method=handler.command,
                path=handler.path.partition("?")[0],
            )
            self._send_json(handler, 400, "invalid request body")
            return

        path = handler.path.partition("?")[0]
        request_event_fields: dict[str, Any] = {
            "method": handler.command,
            "path": path,
            "available_sources": self.source_pool.available_labels(),
        }
        input_fields = self._extract_input_fields(body)
        if input_fields:
            request_event_fields["input"] = input_fields
        self._log_event("request_received", **request_event_fields)
        source = self.source_pool.choose()
        if source is None:
            self._log_event("no_available_source", method=handler.command, path=path)
            self._send_json(handler, 503, "no available RawChat source")
            return

        excluded: set[str] = set()
        last_retryable_error: tuple[
            int, requests.structures.CaseInsensitiveDict[str], bytes
        ] | None = None
        for attempt in range(1, 3):
            source = self.source_pool.choose(excluded)
            if source is None:
                self._log_event(
                    "no_available_source",
                    method=handler.command,
                    path=path,
                    attempt=attempt,
                )
                if last_retryable_error is not None:
                    self._send_buffered(handler, *last_retryable_error)
                else:
                    self._send_json(handler, 503, "no available RawChat source")
                return
            excluded.add(source.email)
            headers = self._forward_headers(handler, source, body)
            url = f"{self.upstream_base_url}{handler.path}"
            started_at = time.monotonic()
            proxies = (
                self.proxy.requests_proxies() if self.proxy is not None else None
            )
            try:
                with requests.Session() as session:
                    response = session.request(
                        handler.command,
                        url,
                        headers=headers,
                        data=body,
                        stream=True,
                        proxies=proxies,
                        timeout=(
                            UPSTREAM_CONNECT_TIMEOUT,
                            UPSTREAM_READ_TIMEOUT,
                        ),
                    )
                    first_byte_at = time.monotonic()
                    quota_candidate = response.status_code in {402, 403, 429}
                    error_body = b""
                    if 400 <= response.status_code < 600:
                        try:
                            error_body = response.content
                        except Exception:
                            error_body = b""
                            handler.close_connection = True
                    quota_error = (
                        quota_candidate
                        and is_quota_error(response.status_code, error_body)
                    )
                    error_category = (
                        self._error_category(error_body) if error_body else None
                    )
                    retryable_error = quota_error or (
                        response.status_code == 429
                        and error_category == "rate_limit"
                    )
                    switch_reason = (
                        "quota"
                        if quota_error
                        else "rate_limit"
                        if retryable_error
                        else None
                    )
                    if quota_candidate:
                        if retryable_error:
                            last_retryable_error = (
                                response.status_code,
                                response.headers,
                                error_body,
                            )
                            try:
                                response.close()
                            except Exception:
                                handler.close_connection = True
                            response_finished_at = time.monotonic()
                            self._log_event(
                                "upstream_response",
                                source=self.source_pool.source_label(source.email),
                                attempt=attempt,
                                status=response.status_code,
                                quota_error=quota_error,
                                switching=retryable_error,
                                switch_reason=switch_reason,
                                error_category=error_category,
                                first_byte_time_ms=round(
                                    (first_byte_at - started_at) * 1000, 3
                                ),
                                response_time_ms=round(
                                    (response_finished_at - started_at) * 1000, 3
                                ),
                                response_complete=True,
                            )
                            release_at = _parse_release_at(error_body)
                            if quota_error:
                                self.source_pool.mark_quota_exhausted(
                                    source.email,
                                    f"upstream {switch_reason} error",
                                    release_at,
                                )
                                source_event = "source_marked_unavailable"
                            else:
                                self.source_pool.mark_refresh_failed(
                                    source.email,
                                    f"upstream {switch_reason} error",
                                )
                                source_event = "source_refresh_failed"
                            self._log_event(
                                source_event,
                                source=self.source_pool.source_label(source.email),
                                attempt=attempt,
                                reason=switch_reason,
                            )
                            continue
                        self._send_buffered(
                            handler,
                            response.status_code,
                            response.headers,
                            error_body,
                        )
                        try:
                            response.close()
                        except Exception:
                            handler.close_connection = True
                        response_finished_at = time.monotonic()
                        self._log_event(
                            "upstream_response",
                            source=self.source_pool.source_label(source.email),
                            attempt=attempt,
                            status=response.status_code,
                            quota_error=quota_error,
                            switching=retryable_error,
                            switch_reason=switch_reason,
                            error_category=error_category,
                            first_byte_time_ms=round(
                                (first_byte_at - started_at) * 1000, 3
                            ),
                            response_time_ms=round(
                                (response_finished_at - started_at) * 1000, 3
                            ),
                            response_complete=True,
                        )
                        return

                    if 200 <= response.status_code < 400:
                        self.source_pool.mark_success(source.email)
                    response_finished_at, response_complete = self._send_stream(
                        handler, response
                    )
                    self._log_event(
                        "upstream_response",
                        source=self.source_pool.source_label(source.email),
                        attempt=attempt,
                        status=response.status_code,
                        quota_error=quota_error,
                        switching=retryable_error,
                        switch_reason=switch_reason,
                        error_category=error_category,
                        first_byte_time_ms=round(
                            (first_byte_at - started_at) * 1000, 3
                        ),
                        response_time_ms=round(
                            (response_finished_at - started_at) * 1000, 3
                        ),
                        response_complete=response_complete,
                    )
                    return
            except (OSError, requests.RequestException, Urllib3HTTPError) as exc:
                self._log_event(
                    "upstream_request_error",
                    source=self.source_pool.source_label(source.email),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                self._send_json(handler, 502, "RawChat upstream unavailable")
                return

        if last_retryable_error is not None:
            self._send_buffered(handler, *last_retryable_error)
        else:
            self._send_json(handler, 503, "no available RawChat source")
