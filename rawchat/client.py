"""RawChat API clients and asynchronous refresh orchestration."""

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from .config import (
    ACCOUNT_REQUEST_GAP,
    BALANCE_URL,
    GETME_URL,
    HEADERS,
    LOGIN_URL,
    ProxyConfig,
    QUOTA_URL,
    RECORD_LIMIT,
    RECORDS_URL,
    REQUEST_TIMEOUT,
    ROLLING_LIMIT_URL,
    WORKER_STOP_TIMEOUT,
)
from .records import (
    DashboardSnapshot,
    _number,
    _request_sort_key,
    normalize_codex_data,
    record_key,
)
from .sources import (
    ApiKeyCache,
    SourcePool,
    _parse_release_at,
    is_quota_error,
    is_quota_exhausted,
)


class RawChatError(RuntimeError):
    """RawChat 请求或响应错误。"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RefreshCancelled(RuntimeError):
    """刷新在退出过程中被取消。"""


class RawChatClient:
    """只负责 RawChat Codex 数据请求的客户端。"""

    def __init__(
        self,
        session: requests.Session | None = None,
        email: str = "",
        password: str = "",
        proxy: ProxyConfig | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.email = email
        self.password = password
        self.proxy = proxy
        if proxy is not None:
            self.session.proxies.update(proxy.requests_proxies())

    def _payload(
        self, method: str, url: str, label: str, **kwargs: Any
    ) -> Any:
        response: requests.Response | None = None
        try:
            response = getattr(self.session, method)(
                url, timeout=REQUEST_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            envelope = response.json()
        except requests.HTTPError as exc:
            try:
                body = response.content if response is not None else b""
            except Exception:
                body = b""
            if not isinstance(body, bytes):
                body = str(body).encode("utf-8", "replace")
            status_code = (
                response.status_code
                if response is not None and isinstance(response.status_code, int)
                else None
            )
            raise RawChatError(
                f"{label}: {exc}", status_code=status_code, body=body
            ) from exc
        except (requests.RequestException, ValueError) as exc:
            raise RawChatError(f"{label}: {exc}") from exc

        if not isinstance(envelope, dict) or envelope.get("code") != 1:
            message = (
                envelope.get("msg")
                if isinstance(envelope, dict)
                else "invalid response"
            )
            raise RawChatError(f"{label}: {message or 'unknown error'}")
        return envelope.get("data")

    def login(self) -> None:
        self._payload(
            "post",
            LOGIN_URL,
            "登录失败",
            json={"userToken": self.email, "password": self.password},
        )

    def fetch_codex(self) -> dict[str, Any]:
        data = self._payload("get", QUOTA_URL, "配额请求失败")
        codex = data.get("codex") if isinstance(data, dict) else None
        if not isinstance(codex, dict):
            raise RawChatError("配额响应缺少 Codex 数据")
        return codex

    def fetch_records(self) -> dict[str, Any]:
        """使用 /records 端点获取调用记录（与网页默认行为一致：pageSize=20）。"""
        data = self._payload(
            "get",
            RECORDS_URL,
            "调用记录请求失败",
            params={"page": 1, "pageSize": RECORD_LIMIT},
        )
        if not isinstance(data, dict):
            raise RawChatError("调用记录响应格式无效")
        return data

    def fetch_user_token(self) -> str:
        data = self._payload("get", GETME_URL, "用户信息请求失败")
        token = data.get("userToken") if isinstance(data, dict) else None
        if not token:
            raise RawChatError("用户信息响应缺少 userToken")
        return str(token)

    def fetch_rolling_limit(self, user_token: str) -> dict[str, Any]:
        data = self._payload(
            "post",
            ROLLING_LIMIT_URL,
            "滚动窗口请求失败",
            json={"userToken": user_token},
        )
        if not isinstance(data, dict):
            raise RawChatError("滚动窗口响应格式无效")
        return data

    def fetch_balance(self) -> dict[str, Any]:
        data = self._payload("get", BALANCE_URL, "余额请求失败")
        if not isinstance(data, dict):
            raise RawChatError("余额响应格式无效")
        return data

    def close(self) -> None:
        self.session.close()


class MultiAccountClient:
    """管理多个 RawChatClient 实例，聚合多账号数据。"""

    def __init__(
        self,
        accounts: list[dict[str, str]],
        key_cache: ApiKeyCache | None = None,
        source_pool: SourcePool | None = None,
        proxy: ProxyConfig | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("至少需要一个账号")
        self.key_cache = key_cache
        cached_keys = {
            str(account["email"]): key_cache.get(str(account["email"]))
            for account in accounts
            if key_cache is not None
            and key_cache.get(str(account["email"]))
        }
        self.source_pool = source_pool or SourcePool(
            accounts,
            keys={email: key for email, key in cached_keys.items() if key},
        )
        if source_pool is not None:
            for email, key in cached_keys.items():
                if key:
                    source_pool.set_key(email, key)
        self.clients = [
            RawChatClient(
                email=account["email"],
                password=account["password"],
                proxy=proxy,
            )
            for account in accounts
        ]
        self._authenticated = False
        self._codex_by_email: dict[str, dict[str, Any]] = {}

    def login_all(self) -> None:
        errors: list[RawChatError] = []
        successful = 0
        for index, client in enumerate(self.clients):
            if index > 0:
                time.sleep(ACCOUNT_REQUEST_GAP)
            try:
                client.login()
                successful += 1
            except RawChatError as exc:
                errors.append(exc)
        self._authenticated = successful > 0
        if not successful and errors:
            raise errors[-1]

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def fetch_all_codex(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """聚合所有账号的 codex 数据，数值字段求和，records 合并去重排序。"""
        all_records: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        subscriptions: list[dict[str, Any]] = []
        account_errors: list[RawChatError] = []
        quota_successes = 0
        total_requests = 0
        total_tokens = 0
        total_cost = 0.0
        last_request_time: str | None = None
        per_account: list[dict[str, Any]] = []
        self._codex_by_email = {}

        for index, client in enumerate(self.clients):
            if index > 0:
                time.sleep(ACCOUNT_REQUEST_GAP)

            account_subs: dict[str, Any] | None = None
            account_requests = 0

            try:
                codex = client.fetch_codex()
            except RawChatError as exc:
                account_errors.append(exc)
                if exc.status_code is not None and is_quota_error(
                    exc.status_code, exc.body
                ):
                    self.source_pool.mark_quota_exhausted(
                        client.email,
                        "upstream quota error",
                        _parse_release_at(exc.body),
                    )
                else:
                    self.source_pool.mark_refresh_failed(client.email, str(exc))
                per_account.append({
                    "subscriptions": None,
                    "email": client.email,
                    "request_count": 0,
                    "error": str(exc),
                    "balance": None,
                })
                continue
            quota_successes += 1
            api_key = codex.get("apiKey")
            if isinstance(api_key, str) and api_key:
                if (
                    self.key_cache is not None
                    and self.key_cache.get(client.email) != api_key
                ):
                    self.key_cache.set(client.email, api_key)
                self.source_pool.set_key(client.email, api_key)
            self._codex_by_email[client.email] = dict(codex)
            self.source_pool.update_quota(
                client.email,
                is_quota_exhausted(codex),
                "quota data",
            )
            subs = codex.get("subscriptions")
            if isinstance(subs, dict):
                account_subs = dict(subs)
                subscriptions.append(account_subs)

            usage = codex.get("currentUsage")
            if isinstance(usage, dict):
                account_requests = int(_number(usage.get("totalRequests")) or 0)
                total_requests += account_requests
                total_tokens += int(_number(usage.get("totalTokens")) or 0)
                total_cost += float(_number(usage.get("totalCost")) or 0)
                latest_request_time = usage.get("lastRequestTime")
                if latest_request_time:
                    last_request_time = last_request_time or str(latest_request_time)

            try:
                records_data = client.fetch_records()
            except RawChatError as exc:
                self.source_pool.mark_refresh_failed(client.email, str(exc))
                records_data = {"items": []}
            records = (
                records_data.get("items")
                if isinstance(records_data, dict)
                else None
            )
            if isinstance(records, list):
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    merged_record = dict(record)
                    merged_record["_account_email"] = client.email
                    key = record_key(merged_record)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_records.append(merged_record)

            balance: dict[str, Any] | None = None
            try:
                balance = client.fetch_balance()
            except RawChatError as exc:
                self.source_pool.mark_refresh_failed(client.email, str(exc))

            per_account.append({
                "subscriptions": account_subs,
                "email": client.email,
                "request_count": account_requests,
                "balance": balance,
            })

        if not quota_successes and account_errors:
            raise account_errors[-1]

        all_records.sort(key=_request_sort_key, reverse=True)
        aggregated = {
            "recentRecords": all_records[:RECORD_LIMIT],
            "subscriptions": subscriptions,
            "currentUsage": {
                "totalRequests": total_requests,
                "totalTokens": total_tokens,
                "totalCost": total_cost,
                "lastRequestTime": last_request_time,
            },
        }
        return aggregated, per_account

    def fetch_rolling_limits(self) -> list[dict[str, Any] | None]:
        """遍历所有账号各自获取滚动窗口数据。"""
        results: list[dict[str, Any] | None] = []
        for index, client in enumerate(self.clients):
            if index > 0:
                time.sleep(ACCOUNT_REQUEST_GAP)
            try:
                token = client.fetch_user_token()
                rolling = client.fetch_rolling_limit(token)
                if isinstance(rolling, dict):
                    codex = self._codex_by_email.get(client.email)
                    if isinstance(codex, dict):
                        self.source_pool.update_quota(
                            client.email,
                            is_quota_exhausted(codex, rolling),
                            "quota data",
                        )
                    results.append(rolling)
                else:
                    results.append(None)
            except RawChatError as exc:
                if exc.status_code is not None and is_quota_error(
                    exc.status_code, exc.body
                ):
                    self.source_pool.mark_quota_exhausted(
                        client.email,
                        "upstream quota error",
                        _parse_release_at(exc.body),
                    )
                else:
                    self.source_pool.mark_refresh_failed(client.email, str(exc))
                results.append(None)
        return results

    def close(self) -> None:
        for client in self.clients:
            client.close()


def collect_snapshot(
    client: MultiAccountClient,
    previous: DashboardSnapshot | None = None,
    now: Any = datetime.now,
    cancelled: Any = lambda: False,
) -> DashboardSnapshot:
    """采集完整快照（多账号聚合）；滚动窗口失败不影响主配额数据。"""
    if cancelled():
        raise RefreshCancelled()
    aggregated, per_account = client.fetch_all_codex()
    codex = normalize_codex_data({"codex": aggregated})
    if cancelled():
        raise RefreshCancelled()
    try:
        rolling_limits = client.fetch_rolling_limits()
    except RawChatError:
        rolling_limits = [None] * len(per_account)
    for index, rolling in enumerate(rolling_limits):
        if index < len(per_account):
            per_account[index]["rolling_limit"] = rolling
    return DashboardSnapshot(codex, None, None, now(), per_account=per_account)


@dataclass(frozen=True)
class RefreshOutcome:
    snapshot: DashboardSnapshot | None
    error: str | None
    failure_count: int


class RefreshEngine:
    """管理登录、连续失败和最后一次成功快照。"""

    def __init__(self, client: MultiAccountClient, now: Any = datetime.now) -> None:
        self.client = client
        self.now = now
        self.authenticated = bool(getattr(client, "authenticated", False))
        self.failure_count = 0
        self.last_snapshot: DashboardSnapshot | None = None
        self.cancelled: Any = lambda: False

    def _collect(self) -> DashboardSnapshot:
        if self.cancelled():
            raise RefreshCancelled()
        if not self.authenticated:
            self.client.login_all()
            self.authenticated = True
        if self.cancelled():
            raise RefreshCancelled()
        return collect_snapshot(
            self.client,
            self.last_snapshot,
            self.now,
            self.cancelled,
        )

    def refresh(self) -> RefreshOutcome:
        try:
            snapshot = self._collect()
        except RawChatError as exc:
            self.failure_count += 1
            if self.failure_count < 3:
                return RefreshOutcome(
                    self.last_snapshot, str(exc), self.failure_count
                )

            self.authenticated = False
            try:
                snapshot = self._collect()
            except RawChatError as retry_exc:
                return RefreshOutcome(
                    self.last_snapshot,
                    str(retry_exc),
                    self.failure_count,
                )

        self.last_snapshot = snapshot
        self.failure_count = 0
        return RefreshOutcome(snapshot, None, 0)

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


class RefreshWorker:
    """在唯一后台线程中串行执行刷新。"""

    def __init__(self, engine: RefreshEngine) -> None:
        self.engine = engine
        self._commands: queue.Queue[str | None] = queue.Queue()
        self._results: queue.Queue[RefreshOutcome] = queue.Queue()
        self._lock = threading.Lock()
        self._pending = False
        self._stopped = False
        self._stop_event = threading.Event()
        self.engine.cancelled = self._stop_event.is_set
        self._thread = threading.Thread(
            target=self._run, name="rawchat-refresh", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def request_refresh(self) -> bool:
        with self._lock:
            if self._stopped or self._pending:
                return False
            self._pending = True
        self._commands.put("refresh")
        return True

    def get_result(self) -> RefreshOutcome | None:
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        while True:
            command = self._commands.get()
            if command is None or self._stop_event.is_set():
                return
            try:
                outcome = self.engine.refresh()
            except RefreshCancelled:
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                failure_count = getattr(self.engine, "failure_count", 0)
                try:
                    failure_count = max(1, int(failure_count))
                except (TypeError, ValueError):
                    failure_count = 1
                outcome = RefreshOutcome(
                    getattr(self.engine, "last_snapshot", None),
                    str(exc) or type(exc).__name__,
                    failure_count,
                )
            if self._stop_event.is_set():
                return
            self._results.put(outcome)
            with self._lock:
                self._pending = False

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_event.set()
        close = getattr(self.engine, "close", None)
        if callable(close):
            close()
        self._commands.put(None)
        self._thread.join(timeout=WORKER_STOP_TIMEOUT)
