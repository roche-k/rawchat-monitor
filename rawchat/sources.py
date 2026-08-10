"""Account key caching, source selection, and quota classification."""

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .records import _number, _parse_datetime


QUOTA_KEYWORDS = (
    "quota",
    "insufficient",
    "exhausted",
    "credit",
)

QUOTA_EXHAUSTED_MESSAGE = "您当前的 Codex 额度已用完，请返回网页端查看明细。"


def _contains_quota_exhausted_message(value: Any) -> bool:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return any(_contains_quota_exhausted_message(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_quota_exhausted_message(item) for item in value)
    return QUOTA_EXHAUSTED_MESSAGE in str(value or "")


class ApiKeyCache:
    """跨进程复用 RawChat Codex key，不保存账号密码。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._keys: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if isinstance(keys, dict):
            self._keys = {
                str(email): str(key)
                for email, key in keys.items()
                if isinstance(email, str) and isinstance(key, str) and key
            }

    def get(self, email: str) -> str | None:
        return self._keys.get(email)

    def set(self, email: str, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        self._keys[email] = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(
                {"version": 1, "keys": self._keys},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.path)
        os.chmod(self.path, 0o600)


@dataclass
class SourceState:
    email: str
    password: str
    api_key: str | None = None
    quota_available: bool = True
    refresh_failed: bool = False
    reason: str | None = None
    refresh_error: str | None = None
    release_at: datetime | None = None
    rolling_limit: dict[str, Any] | None = None
    rolling_fetched_at: datetime | None = None

    @property
    def status(self) -> str:
        if not self.quota_available:
            return "exhausted"
        if self.refresh_failed:
            return "refresh_failed"
        return "active"


class SourcePool:
    """按配置顺序选择带 key 且配额可用的 RawChat source。"""

    def __init__(
        self,
        accounts: list[dict[str, str]],
        keys: dict[str, str] | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("至少需要一个账号")
        cached = keys or {}
        self._sources = [
            SourceState(
                email=str(account["email"]),
                password=str(account["password"]),
                api_key=cached.get(str(account["email"])),
            )
            for account in accounts
        ]
        self._current_email: str | None = None
        self._lock = threading.RLock()

    def choose(self, excluded: set[str] | None = None) -> SourceState | None:
        excluded = excluded or set()
        now = datetime.now()
        with self._lock:
            sources = sorted(
                self._sources,
                key=lambda source: source.email != self._current_email,
            )
            for source in sources:
                if not source.api_key:
                    continue
                if source.email in excluded:
                    continue
                if not source.quota_available:
                    if source.release_at is not None and now >= source.release_at:
                        source.quota_available = True
                        source.reason = None
                        source.release_at = None
                    else:
                        continue
                return source
        return None

    def set_key(self, email: str, key: str) -> None:
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    source.api_key = key
                    return

    def set_rolling_snapshot(
        self,
        email: str,
        rolling: dict[str, Any],
        fetched_at: datetime,
    ) -> None:
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    source.rolling_limit = dict(rolling)
                    source.rolling_fetched_at = fetched_at
                    return

    def get_rolling_snapshot(
        self, email: str
    ) -> tuple[dict[str, Any] | None, datetime | None]:
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    return source.rolling_limit, source.rolling_fetched_at
        return None, None

    def update_quota(
        self,
        email: str,
        exhausted: bool,
        reason: str = "",
        release_at: datetime | None = None,
    ) -> None:
        if release_at is not None and release_at.tzinfo is not None:
            release_at = release_at.astimezone().replace(tzinfo=None)
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    source.quota_available = not exhausted
                    source.refresh_failed = False
                    source.reason = reason or None
                    source.refresh_error = None
                    source.release_at = release_at if exhausted else None
                    return

    def mark_refresh_failed(self, email: str, reason: str = "") -> None:
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    source.refresh_failed = True
                    source.refresh_error = reason or "refresh failed"
                    return

    def mark_quota_exhausted(
        self,
        email: str,
        reason: str,
        release_at: datetime | None = None,
    ) -> None:
        self.update_quota(email, True, reason, release_at)

    def mark_success(self, email: str) -> None:
        with self._lock:
            for source in self._sources:
                if source.email == email:
                    self._current_email = email
                    source.refresh_failed = False
                    source.refresh_error = None
                    return

    def current_email(self) -> str | None:
        with self._lock:
            return self._current_email

    def available_emails(self) -> list[str]:
        with self._lock:
            return [
                source.email
                for source in self._sources
                if source.api_key and source.quota_available
            ]

    def account_count(self) -> int:
        with self._lock:
            return len(self._sources)

    def source_label(self, email: str) -> str:
        with self._lock:
            for index, source in enumerate(self._sources, start=1):
                if source.email == email:
                    return f"account-{index}"
        return "unknown"

    def available_labels(self) -> list[str]:
        with self._lock:
            return [
                f"account-{index}"
                for index, source in enumerate(self._sources, start=1)
                if source.api_key and source.quota_available
            ]


def _quota_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_quota_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_quota_text(item) for item in value)
    return str(value or "")


def is_quota_exhausted(codex: Any, rolling: Any = None) -> bool:
    if not isinstance(codex, dict):
        return False
    text_parts = [
        _quota_text(codex.get(field))
        for field in (
            "error",
            "errorMessage",
            "message",
            "reason",
            "disabledReason",
        )
    ]
    subscriptions = codex.get("subscriptions")
    if isinstance(subscriptions, dict):
        text_parts.extend(
            _quota_text(subscriptions.get(field))
            for field in (
                "error",
                "errorMessage",
                "message",
                "reason",
                "disabledReason",
            )
        )
    if any(_contains_quota_exhausted_message(part) for part in text_parts):
        return True

    if isinstance(subscriptions, dict):
        billing_type = str(subscriptions.get("billingType") or "").lower()
        if billing_type in {"amount", "usd", "money"}:
            fields = ("remainingAmount", "remainingUsd")
        elif billing_type in {"count", "request", "requests"}:
            fields = ("remainingCount",)
        else:
            fields = ("remainingCount", "remainingAmount", "remainingUsd")
        for field in fields:
            value = _number(subscriptions.get(field))
            if value is not None and value <= 0:
                return True
        if billing_type in {"amount", "usd", "money", ""}:
            used_amount = _number(subscriptions.get("usedAmount"))
            amount_limit = _number(subscriptions.get("amountLimit"))
            if amount_limit is None:
                amount_limit = _number(subscriptions.get("limit"))
            if (
                used_amount is not None
                and amount_limit is not None
                and used_amount >= amount_limit
            ):
                return True

    window = rolling.get("window") if isinstance(rolling, dict) else None
    if isinstance(window, dict):
        remaining = _number(window.get("remainingUsd"))
        if remaining is not None and remaining <= 0:
            return True

    rolling_text = ""
    if isinstance(window, dict):
        rolling_text = " ".join(
            _quota_text(window.get(field))
            for field in ("error", "message", "reason", "disabledReason")
        )
    return bool(
        isinstance(rolling, dict)
        and rolling.get("enabled")
        and isinstance(window, dict)
        and window.get("isLimited")
        and _contains_quota_exhausted_message(rolling_text)
    )


def is_quota_error(status: int, body: bytes) -> bool:
    if status == 402:
        return True
    if status == 403:
        return _contains_quota_exhausted_message(body)
    if status != 429:
        return False
    text = body.decode("utf-8", "replace").lower()
    return any(keyword in text for keyword in QUOTA_KEYWORDS)


def _parse_release_at(body: bytes) -> datetime | None:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    release_str = None
    if isinstance(data, dict):
        release_str = data.get("releaseAt") or data.get("release_at")
        if not release_str:
            window = data.get("window")
            if isinstance(window, dict):
                release_str = window.get("releaseAt") or window.get("release_at")
    if not isinstance(release_str, str) or not release_str:
        return None
    try:
        parsed = _parse_datetime(release_str)
        if parsed is not None:
            return parsed + timedelta(minutes=1)
    except (ValueError, TypeError):
        pass
    return None
