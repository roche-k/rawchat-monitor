"""Snapshot normalization and local Codex record persistence."""

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import RECORD_LIMIT


@dataclass(frozen=True)
class DashboardSnapshot:
    codex: dict[str, Any]
    rolling_limit: dict[str, Any] | None
    rolling_error: str | None
    fetched_at: datetime
    per_account: list[dict[str, Any]] = field(default_factory=list)


def _log_date(now: datetime) -> str:
    """日志文件名使用的本地日期（YYYY-MM-DD）。"""
    if now.tzinfo is not None:
        now = now.astimezone()
    return now.strftime("%Y-%m-%d")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _request_sort_key(record: dict[str, Any]) -> tuple[int, float]:
    parsed = _parse_datetime(record.get("requestTime"))
    if parsed is None:
        return (0, 0.0)
    try:
        return (1, parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return (0, 0.0)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _is_noise(record: dict[str, Any]) -> bool:
    """过滤 API 返回的无效记录（失败且无实际消耗）。"""
    return (
        record.get("status") == "failed"
        and int(_number(record.get("totalTokens")) or 0) == 0
    )


def record_key(record: dict[str, Any]) -> str:
    """全字段去重 key：对记录所有字段做规范化后计算稳定哈希。"""
    payload = json.dumps(
        record, sort_keys=True, ensure_ascii=False, default=str
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest


def normalize_codex_data(quota_data: Any) -> dict[str, Any]:
    """提取 Codex 数据，并按调用时间保留最新 20 条记录。"""
    services = quota_data if isinstance(quota_data, dict) else {}
    raw_codex = services.get("codex")
    codex = dict(raw_codex) if isinstance(raw_codex, dict) else {}
    raw_records = codex.get("recentRecords")
    records = (
        [dict(record) for record in raw_records if isinstance(record, dict)]
        if isinstance(raw_records, list)
        else []
    )
    records.sort(key=_request_sort_key, reverse=True)
    codex["recentRecords"] = records[:RECORD_LIMIT]
    return codex


class RecordStore:
    """当天所有 Codex 调用记录的去重存储，按天 append-only 写入 JSONL 日志。"""

    def __init__(
        self,
        log_dir: str = "logs",
        now: Any = datetime.now,
    ) -> None:
        self.log_dir = log_dir
        self.now = now
        self._keys: set[str] = set()
        self._records: list[dict[str, Any]] = []
        self._today = _log_date(self.now())
        self._load_today()

    def log_path(self) -> str:
        return os.path.join(
            self.log_dir, f"rawchat_codex_{_log_date(self.now())}.jsonl"
        )

    def _load_today(self) -> None:
        path = self.log_path()
        if not os.path.exists(path):
            return
        today = _log_date(self.now())
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if _is_noise(record):
                        continue
                    # 只加载当天的记录，忽略跨天混入的旧数据
                    rt = _parse_datetime(record.get("requestTime"))
                    if rt is None or _log_date(rt) != today:
                        continue
                    key = record_key(record)
                    if key in self._keys:
                        continue
                    self._keys.add(key)
                    self._records.append(record)
        except OSError:
            return
        self._sort()

    def seen(self, key: str) -> bool:
        return key in self._keys

    def _sort(self) -> None:
        self._records.sort(key=_request_sort_key, reverse=True)

    def all_records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def ingest(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """写入本地不存在的当天记录到日志，返回这批新记录。"""
        new_today = _log_date(self.now())
        if new_today != self._today:
            self._today = new_today
            self._keys.clear()
            self._records.clear()
            self._load_today()

        new_records: list[dict[str, Any]] = []
        today = _log_date(self.now())
        for record in records:
            if not isinstance(record, dict):
                continue
            if _is_noise(record):
                continue
            rt = _parse_datetime(record.get("requestTime"))
            if rt is None or _log_date(rt) != today:
                continue
            key = record_key(record)
            if key in self._keys:
                continue
            self._keys.add(key)
            self._records.append(record)
            new_records.append(record)
        if new_records:
            self._sort()
            self._append(new_records)
        return new_records

    def _append(self, records: list[dict[str, Any]]) -> None:
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path(), "a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, default=str)
                        + "\n"
                    )
        except OSError:
            return
