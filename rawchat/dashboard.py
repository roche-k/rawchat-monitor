"""Terminal dashboard presentation, state, and rendering helpers."""

import curses
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import RefreshOutcome
from .config import (
    CHART_BUCKET_MINUTES,
    CHART_RESERVED,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_SUCCESS,
    COLOR_WARNING,
    LOG_DIR,
    MAX_CHART_WIDTH,
    MIN_COLS,
    MIN_ROWS,
    STATS_RESERVED,
)
from .records import (
    DashboardSnapshot,
    RecordStore,
    _log_date,
    _number,
    _parse_datetime,
)


def fmt_cost(cost: Any) -> str:
    """格式化费用"""
    number = _number(cost)
    return "-" if number is None else f"${number:.5f}"


def fmt_tokens(n: Any) -> str:
    """格式化 token 数"""
    number = _number(n)
    if number is None:
        return "-"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.2f}K"
    return f"{number:,.0f}"


def total_input_tokens(record: dict[str, Any]) -> int:
    """输入总量 = uncached(inputTokens) + cached(cacheInputTokens)"""
    return int(_number(record.get("inputTokens")) or 0) + int(
        _number(record.get("cacheInputTokens")) or 0
    )


def total_output_tokens(record: dict[str, Any]) -> int:
    """输出总量 = outputTokens（API 无独立 cached-output 字段）"""
    return int(_number(record.get("outputTokens")) or 0)


def uncached_input_tokens(record: dict[str, Any]) -> int:
    """未缓存输入 = inputTokens（与网页表格一致）"""
    return int(_number(record.get("inputTokens")) or 0)


def uncached_output_tokens(record: dict[str, Any]) -> int:
    """未缓存输出 = outputTokens（与网页表格一致）"""
    return int(_number(record.get("outputTokens")) or 0)


def total_io_tokens(record: dict[str, Any]) -> int:
    """图表单条口径 = (input + cacheInput) + output"""
    return total_input_tokens(record) + total_output_tokens(record)


def fmt_duration(value: Any) -> str:
    """按网页规则把毫秒格式化为秒。"""
    number = _number(value)
    return "-" if number is None or number < 0 else f"{number / 1000:.2f}s"


def fmt_discount(rate: Any, amount: Any) -> str:
    """格式化网页中的随机折扣与优惠金额。"""
    rate_number = _number(rate)
    amount_number = _number(amount)
    if (
        rate_number is None
        or rate_number <= 0
        or amount_number is None
        or amount_number <= 0
    ):
        return "-"
    discount = rate_number * 10
    discount_text = (
        f"{discount:.0f}" if discount.is_integer() else f"{discount:.1f}"
    )
    return f"{discount_text}折 (-${amount_number:.5f})"


def fmt_request_time(value: Any) -> str:
    """将接口时间转换为适合表格的本地时间。"""
    parsed = _parse_datetime(value)
    if parsed is None:
        return "-"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%m-%d %H:%M:%S")


def record_values(record: dict[str, Any]) -> tuple[str, ...]:
    """返回与网页 Codex 调用表一致的可见字段（含账户列）。"""
    status = record.get("status")
    status_text = {"success": "成功", "failed": "失败"}.get(status, "-")
    account = str(record.get("_account_email") or "-")
    return (
        fmt_request_time(record.get("requestTime")),
        str(record.get("model") or "-"),
        fmt_tokens(uncached_input_tokens(record)),
        fmt_tokens(uncached_output_tokens(record)),
        fmt_tokens(record.get("cacheInputTokens")),
        fmt_tokens(record.get("cacheWriteTokens")),
        fmt_tokens(record.get("reasoningTokens")),
        fmt_tokens(record.get("totalTokens")),
        fmt_cost(record.get("rawCost")),
        fmt_discount(
            record.get("discountRate"), record.get("discountAmount")
        ),
        fmt_cost(record.get("cost")),
        fmt_duration(record.get("responseTime")),
        fmt_duration(record.get("firstByteTime")),
        status_text,
        account,
        str(record.get("ip") or "-"),
    )


TABLE_COLUMNS = (
    ("时间", 19, "left"),
    ("模型", 32, "left"),
    ("输入", 10, "right"),
    ("输出", 10, "right"),
    ("缓存输入", 12, "right"),
    ("缓存写入", 12, "right"),
    ("推理", 10, "right"),
    ("总计", 10, "right"),
    ("原价", 12, "right"),
    ("折扣", 21, "right"),
    ("实付", 12, "right"),
    ("响应耗时", 10, "right"),
    ("首字耗时", 10, "right"),
    ("状态", 8, "center"),
    ("账户", 24, "left"),
    ("IP", 15, "left"),
)


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: Any) -> int:
    return sum(char_width(char) for char in str(text))


def slice_display(text: Any, start: int, width: int) -> str:
    """按终端显示列截取文本，不切开宽字符。"""
    if width <= 0:
        return ""
    position = 0
    used = 0
    visible: list[str] = []
    for char in str(text):
        size = char_width(char)
        next_position = position + size
        if next_position <= start:
            position = next_position
            continue
        if position < start:
            position = next_position
            continue
        if used + size > width:
            break
        visible.append(char)
        used += size
        position = next_position
    return "".join(visible)


def fit_cell(value: Any, width: int, align: str = "left") -> str:
    text = str(value)
    kept: list[str] = []
    used = 0
    for char in text:
        size = char_width(char)
        if used + size > width:
            break
        kept.append(char)
        used += size
    clipped = "".join(kept)
    padding = " " * max(0, width - used)
    if align == "right":
        return padding + clipped
    if align == "center":
        left = len(padding) // 2
        return padding[:left] + clipped + padding[left:]
    return clipped + padding


def _table_line(values: tuple[str, ...] | list[str]) -> str:
    cells = [
        fit_cell(value, width, align)
        for value, (_, width, align) in zip(values, TABLE_COLUMNS)
    ]
    return " | ".join(cells)


def table_header_line() -> str:
    return _table_line([column[0] for column in TABLE_COLUMNS])


def table_record_line(record: dict[str, Any]) -> str:
    return _table_line(record_values(record))


TABLE_WIDTH = display_width(table_header_line())


@dataclass
class DashboardState:
    snapshot: DashboardSnapshot | None = None
    error: str | None = None
    failure_count: int = 0
    refreshing: bool = False
    selected_row: int = 0
    row_offset: int = 0
    column_offset: int = 0
    last_success: datetime | None = None
    next_refresh_at: float = 0.0
    all_records: list[dict[str, Any]] = field(default_factory=list)
    source_pool: Any = None
    statistics: dict[str, Any] = field(default_factory=dict)
    account_record_counts: dict[str, int] = field(default_factory=dict)
    unassigned_record_count: int = 0
    record_lines: list[str] = field(default_factory=list)
    token_buckets: list[tuple[datetime, float]] = field(default_factory=list)
    proxy_request_total: int = 0
    proxy_avg_first_byte_ms: float | None = None
    proxy_avg_response_ms: float | None = None
    proxy_config: Any = None

    def __post_init__(self) -> None:
        refresh_dashboard_data(self)


def _records(snapshot: DashboardSnapshot | None) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    records = snapshot.codex.get("recentRecords")
    return records if isinstance(records, list) else []


def compute_statistics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合历史记录的 token 总量、读缓存命中率、总金额与按 IP 分组指标。"""
    input_tokens = 0
    output_tokens = 0
    cache_hits = 0
    cache_write_hits = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    total_cost = 0.0
    by_ip: dict[str, dict[str, Any]] = {}

    for record in records:
        input_tokens += total_input_tokens(record)
        output_tokens += total_output_tokens(record)
        total_cost += float(_number(record.get("cost")) or 0)
        if (_number(record.get("cacheInputTokens")) or 0) > 0:
            cache_hits += 1
        if (_number(record.get("cacheWriteTokens")) or 0) > 0:
            cache_write_hits += 1
        cache_read_tokens += int(_number(record.get("cacheInputTokens")) or 0)
        cache_write_tokens += int(_number(record.get("cacheWriteTokens")) or 0)

        ip = str(record.get("ip") or "-")
        bucket = by_ip.setdefault(
            ip,
            {
                "count": 0,
                "resp_sum": 0.0,
                "first_byte_sum": 0.0,
                "success": 0,
            },
        )
        bucket["count"] += 1
        bucket["resp_sum"] += (_number(record.get("responseTime")) or 0) / 1000.0
        bucket["first_byte_sum"] += (
            _number(record.get("firstByteTime")) or 0
        ) / 1000.0
        if record.get("status") == "success":
            bucket["success"] += 1

    fresh_input = input_tokens - cache_read_tokens
    cacheable_input = fresh_input + cache_read_tokens + cache_write_tokens
    cache_hit_rate = (cache_read_tokens / cacheable_input) if cacheable_input else 0.0
    total_tokens = input_tokens + output_tokens + cache_write_tokens
    by_ip_summary: dict[str, dict[str, Any]] = {}
    for ip, bucket in by_ip.items():
        count = bucket["count"]
        by_ip_summary[ip] = {
            "count": count,
            "avg_response": bucket["resp_sum"] / count if count else 0.0,
            "avg_first_byte": (
                bucket["first_byte_sum"] / count if count else 0.0
            ),
            "success_rate": bucket["success"] / count if count else 0.0,
        }

    return {
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_hit_rate": cache_hit_rate,
        "by_ip": by_ip_summary,
    }


def build_token_buckets(
    records: list[dict[str, Any]],
    bucket_minutes: int = 5,
) -> list[tuple[datetime, float]]:
    """按时间桶聚合记录实付金额（保留兼容函数名）。"""
    buckets: dict[datetime, float] = {}
    for record in records:
        parsed = _parse_datetime(record.get("requestTime"))
        if parsed is None:
            continue
        bucket = parsed.replace(
            minute=(parsed.minute // bucket_minutes) * bucket_minutes,
            second=0,
            microsecond=0,
        )
        buckets[bucket] = buckets.get(bucket, 0.0) + float(
            _number(record.get("cost")) or 0
        )
    return sorted(buckets.items(), key=lambda item: item[0])


def load_proxy_request_total(
    log_dir: str | Path,
    now: datetime | None = None,
) -> int:
    """当天代理请求总数（兼容旧接口）。"""
    return load_proxy_metrics(log_dir, now)[0]


def load_proxy_metrics(
    log_dir: str | Path,
    now: datetime | None = None,
) -> tuple[int, float | None, float | None]:
    """聚合当天代理事件日志，返回 (请求总数, 首字延迟均值ms, 响应延迟均值ms)。

    延迟来自本地代理实测的 upstream_response 事件（first_byte_time_ms /
    response_time_ms），仅统计完整转发的响应。
    """
    path = Path(log_dir) / (
        f"rawchat_proxy_{_log_date(now or datetime.now())}.jsonl"
    )
    total = 0
    first_byte_values: list[float] = []
    response_values: list[float] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "request_received":
                    total += 1
                    continue
                if event.get("event") != "upstream_response":
                    continue
                if event.get("response_complete") is not True:
                    continue
                first_byte = _number(event.get("first_byte_time_ms"))
                response = _number(event.get("response_time_ms"))
                if first_byte is not None:
                    first_byte_values.append(first_byte)
                if response is not None:
                    response_values.append(response)
    except OSError:
        return 0, None, None
    avg_first_byte = (
        sum(first_byte_values) / len(first_byte_values)
        if first_byte_values
        else None
    )
    avg_response = (
        sum(response_values) / len(response_values) if response_values else None
    )
    return total, avg_first_byte, avg_response


def refresh_dashboard_data(
    state: DashboardState,
    proxy_request_total: int | None = None,
    proxy_metrics: tuple[float | None, float | None] | None = None,
) -> None:
    """Build backend-derived dashboard data outside the rendering path."""
    records = state.all_records if state.all_records else _records(state.snapshot)
    state.statistics = compute_statistics(records)
    state.record_lines = [table_record_line(record) for record in records]
    state.token_buckets = build_token_buckets(records, CHART_BUCKET_MINUTES)
    state.account_record_counts = {}
    state.unassigned_record_count = 0
    for record in records:
        email = str(record.get("_account_email") or "")
        if email:
            state.account_record_counts[email] = (
                state.account_record_counts.get(email, 0) + 1
            )
        else:
            state.unassigned_record_count += 1
    if proxy_request_total is not None:
        state.proxy_request_total = max(0, int(proxy_request_total))
    if proxy_metrics is not None:
        avg_first_byte, avg_response = proxy_metrics
        state.proxy_avg_first_byte_ms = avg_first_byte
        state.proxy_avg_response_ms = avg_response


def render_token_chart(
    buckets: list[tuple[datetime, float]],
    bucket_minutes: int = 5,
    width: int = 60,
    height: int = 10,
) -> list[str]:
    """Render precomputed actual-cost buckets as an ASCII chart."""
    if not buckets or width <= 4 or height <= 3:
        return ["暂无图表数据"]
    times = [stamp for stamp, _ in buckets]
    values = [total for _, total in buckets]
    max_value = max(values)

    gutter = max(7, len(fmt_cost(max_value)) + 1)
    plot_width = max(2, width - gutter)
    plot_height = max(1, height - 3)
    peak_line = (
        f"费用峰值 {fmt_cost(max_value)} | 桶 {bucket_minutes}min | "
        f"点 {len(buckets)} | {times[0].strftime('%H:%M')}~{times[-1].strftime('%H:%M')}"
    )

    x_count = len(buckets)
    columns: list[float] = []
    column_times: list[datetime] = []
    for column in range(plot_width):
        index = (
            round(column * (x_count - 1) / (plot_width - 1))
            if plot_width > 1
            else 0
        )
        columns.append(values[index])
        column_times.append(times[index])

    lines: list[str] = []
    for row in range(plot_height, 0, -1):
        threshold = max_value * row / plot_height
        cells = "".join(
            "*" if max_value > 0 and value >= threshold else " "
            for value in columns
        )
        lines.append(f"{fmt_cost(threshold):>{gutter - 1}}|{cells}")

    lines.append(" " * (gutter - 1) + "+" + "-" * plot_width)
    label_line = " " * gutter + " " * plot_width
    ticks = max(2, plot_width // 18)
    for tick in range(ticks):
        column = (
            round(tick * (plot_width - 1) / (ticks - 1))
            if ticks > 1
            else 0
        )
        stamp = column_times[column].strftime("%H:%M")
        prefix = label_line[: gutter + column]
        suffix = label_line[gutter + column + 5 :]
        label_line = prefix + stamp[:5].rjust(5) + suffix
    lines.append(label_line)

    return [peak_line] + lines


def build_token_chart(
    records: list[dict[str, Any]],
    bucket_minutes: int = 5,
    width: int = 60,
    height: int = 10,
) -> list[str]:
    """Aggregate raw records and render an actual-cost chart."""
    return render_token_chart(
        build_token_buckets(records, bucket_minutes),
        bucket_minutes=bucket_minutes,
        width=width,
        height=height,
    )


def apply_outcome(
    state: DashboardState,
    outcome: RefreshOutcome,
    store: RecordStore | None = None,
) -> None:
    """原子应用刷新结果，并尽量保持当前选中的 requestId。"""
    old_records = _records(state.snapshot)
    selected_id = None
    if old_records and 0 <= state.selected_row < len(old_records):
        selected_id = old_records[state.selected_row].get("requestId")

    state.snapshot = outcome.snapshot
    state.error = outcome.error
    state.failure_count = outcome.failure_count
    state.refreshing = False
    if outcome.error is None and outcome.snapshot is not None:
        state.last_success = outcome.snapshot.fetched_at

    if store is not None:
        store.ingest(_records(state.snapshot))
        state.all_records = store.all_records()
    else:
        state.all_records = list(_records(state.snapshot))
    request_total, avg_first_byte, avg_response = load_proxy_metrics(LOG_DIR)
    refresh_dashboard_data(
        state,
        proxy_request_total=request_total,
        proxy_metrics=(avg_first_byte, avg_response),
    )

    new_records = _records(state.snapshot)
    if selected_id is not None:
        for index, record in enumerate(new_records):
            if record.get("requestId") == selected_id:
                state.selected_row = index
                break
        else:
            state.selected_row = min(
                state.selected_row, max(0, len(new_records) - 1)
            )
    else:
        state.selected_row = min(
            state.selected_row, max(0, len(new_records) - 1)
        )


def handle_key(
    state: DashboardState,
    key: int,
    record_count: int,
    visible_rows: int,
    screen_width: int,
) -> str | None:
    """更新导航状态，命令键返回 refresh 或 quit。"""
    last_row = max(0, record_count - 1)
    visible_rows = max(1, visible_rows)
    if key == curses.KEY_UP:
        state.selected_row -= 1
    elif key == curses.KEY_DOWN:
        state.selected_row += 1
    elif key == curses.KEY_PPAGE:
        state.selected_row -= visible_rows
    elif key == curses.KEY_NPAGE:
        state.selected_row += visible_rows
    elif key == curses.KEY_HOME:
        state.selected_row = 0
    elif key == curses.KEY_END:
        state.selected_row = last_row
    elif key == curses.KEY_LEFT:
        state.column_offset -= 8
    elif key == curses.KEY_RIGHT:
        state.column_offset += 8
    elif key in (ord("r"), ord("R")):
        return "refresh"
    elif key in (ord("q"), ord("Q")):
        return "quit"

    state.selected_row = min(last_row, max(0, state.selected_row))
    max_row_offset = max(0, record_count - visible_rows)
    if state.selected_row < state.row_offset:
        state.row_offset = state.selected_row
    elif state.selected_row >= state.row_offset + visible_rows:
        state.row_offset = state.selected_row - visible_rows + 1
    state.row_offset = min(max_row_offset, max(0, state.row_offset))
    horizontal_width = max(TABLE_WIDTH, display_width(footer_text(state)))
    state.column_offset = min(
        max(0, horizontal_width - screen_width),
        max(0, state.column_offset),
    )
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rolling_text(rolling: dict[str, Any] | None) -> str:
    if not rolling:
        return "无数据"
    if not rolling.get("enabled"):
        return rolling.get("disabledReason") or "未启用"
    window = _dict(rolling.get("window"))
    rule = _dict(rolling.get("rule"))
    status = "已限速" if window.get("isLimited") else "正常"
    window_hours = _number(rule.get("windowHours"))
    window_text = f"{window_hours:g}h " if window_hours is not None else ""
    request_count = _number(window.get("requestCount"))
    request_text = (
        f" 请求 {int(request_count)}" if request_count is not None else ""
    )
    used_text = (
        f" 已用 {fmt_cost(window.get('usedUsd'))}"
        if _number(window.get("usedUsd")) is not None
        else ""
    )
    release = (
        f" 解除 {fmt_request_time(window.get('releaseAt'))}"
        if window.get("isLimited") and window.get("releaseAt")
        else ""
    )
    return (
        f"{window_text}{status}"
        f"{used_text} "
        f"剩余 {fmt_cost(window.get('remainingUsd'))}"
        f"{request_text}"
        f"{release}"
    )


def _subs_text(subscriptions: dict[str, Any] | None) -> str:
    if not subscriptions:
        return "-"
    sub = _dict(subscriptions)
    name = sub.get("subTypeName") or "-"
    expire = sub.get("expireTime")
    expire_text = fmt_request_time(expire) if expire else "未知"
    return f"{name} 到期 {expire_text}"


def _balance_text(
    balance: dict[str, Any] | None,
    subscriptions: dict[str, Any] | None = None,
) -> str:
    if not balance and not subscriptions:
        return "-"
    billing = _dict(balance)
    subscription = _dict(subscriptions)

    used_amount = _number(subscription.get("usedAmount"))
    remaining_amount = _number(subscription.get("remainingAmount"))
    amount_limit = _number(subscription.get("amountLimit"))
    if amount_limit is None:
        amount_limit = _number(subscription.get("limit"))
    if any(
        value is not None
        for value in (used_amount, remaining_amount, amount_limit)
    ):
        fields: list[str] = []
        if amount_limit is not None:
            fields.append(f"总额 {fmt_cost(amount_limit)}")
        if used_amount is not None:
            fields.append(f"已用 {fmt_cost(used_amount)}")
        if remaining_amount is not None:
            fields.append(f"剩余 {fmt_cost(remaining_amount)}")
        return "，".join(fields)

    temporary = _number(billing.get("temporaryBalance"))
    long_term = _number(billing.get("balance"))
    fields = []
    if temporary is not None:
        fields.append(f"临时额度:{temporary:.2f}")
    if long_term is not None:
        fields.append(f"长期余额:{long_term:.2f}")
    return "，".join(fields) if fields else "-"


def build_summary_lines(
    state: DashboardState,
    wall_now: datetime,
    monotonic_now: float,
) -> list[str]:
    snapshot = state.snapshot
    records = _records(snapshot)
    if state.refreshing:
        connection = "刷新中"
    elif state.error:
        connection = f"错误 {state.failure_count}/3"
    elif snapshot:
        connection = "已连接"
    else:
        connection = "等待连接"
    last_success = (
        state.last_success.strftime("%H:%M:%S")
        if state.last_success
        else "-"
    )
    countdown = max(0, int(state.next_refresh_at - monotonic_now))
    proxy_status = (
        state.proxy_config.status_text()
        if state.proxy_config is not None
        else "代理未配置 | 当前直连"
    )

    per_account = snapshot.per_account if snapshot else []
    if not per_account:
        total_records = len(state.all_records) if state.all_records else len(records)
        return [
            f"{connection} | {proxy_status} | 上次 {last_success} | 下次 {countdown}s | "
            f"记录 {total_records} (当天)"
        ]

    total = len(per_account)
    lines: list[str] = []
    for index, account in enumerate(per_account):
        rolling = _rolling_text(account.get("rolling_limit"))
        subscriptions = _subs_text(account.get("subscriptions"))
        balance = _balance_text(
            account.get("balance"), account.get("subscriptions")
        )
        account_count = state.account_record_counts.get(account["email"], 0)
        if total == 1 and account_count == 0:
            account_count = state.unassigned_record_count
        lines.append(
            f"{connection} | {proxy_status} | 账号 {index + 1}/{total} | {account['email']} | "
            f"上次 {last_success} | 下次 {countdown}s | "
            f"记录 {account_count} (当天) | "
            f"滚动窗口 {rolling} | 余额 {balance} | 套餐 {subscriptions}"
        )
    if state.source_pool is not None:
        current = state.source_pool.current_email()
        if current:
            lines.append(f"当前使用账户: {current}")
    return lines


def build_stats_lines(state: DashboardState, max_width: int) -> list[str]:
    """Format statistics generated by the backend dashboard-data refresh."""
    stats = state.statistics
    token_line = (
        f"Tokens 总计 {fmt_tokens(stats['total_tokens'])} | "
        f"输入 {fmt_tokens(stats['input_tokens'])} | "
        f"输出 {fmt_tokens(stats['output_tokens'])} | "
        f"缓存命中 {stats['cache_hit_rate'] * 100:.1f}% | "
        f"总金额 {fmt_cost(stats['total_cost'])} | "
        f"代理请求 {state.proxy_request_total}"
    )
    if (
        state.proxy_avg_first_byte_ms is not None
        or state.proxy_avg_response_ms is not None
    ):
        first_byte_text = (
            f"{state.proxy_avg_first_byte_ms / 1000:.2f}s"
            if state.proxy_avg_first_byte_ms is not None
            else "-"
        )
        response_text = (
            f"{state.proxy_avg_response_ms / 1000:.2f}s"
            if state.proxy_avg_response_ms is not None
            else "-"
        )
        token_line += (
            f" | 代理首字均 {first_byte_text} | 代理响应均 {response_text}"
        )
    lines = [token_line]
    ip_items = sorted(
        stats["by_ip"].items(),
        key=lambda item: item[1]["count"],
        reverse=True,
    )
    ip_slots = STATS_RESERVED - 1
    for ip, metrics in ip_items[:ip_slots]:
        lines.append(
            f"  {ip} | 请求 {metrics['count']} | "
            f"响应均 {metrics['avg_response']:.2f}s | "
            f"首字均 {metrics['avg_first_byte']:.2f}s | "
            f"成功率 {metrics['success_rate'] * 100:.0f}%"
        )
    if len(ip_items) > ip_slots:
        lines.append(f"  … 其余 {len(ip_items) - ip_slots} 个 IP")
    return lines[:STATS_RESERVED]


@dataclass(frozen=True)
class ScreenLayout:
    header_rows: int
    table_header_y: int
    records_y: int
    visible_rows: int
    footer_y: int
    stats_rows: int
    chart_rows: int
    chart_y: int


def layout_for_size(
    rows: int, columns: int, summary_rows: int = 1
) -> ScreenLayout | None:
    if rows < MIN_ROWS or columns < MIN_COLS:
        return None
    footer_y = rows - 1
    stats_rows = STATS_RESERVED
    chart_rows = CHART_RESERVED
    table_header_y = summary_rows + stats_rows + chart_rows
    records_y = table_header_y + 1
    if records_y >= footer_y:
        return None
    return ScreenLayout(
        header_rows=summary_rows,
        table_header_y=table_header_y,
        records_y=records_y,
        visible_rows=footer_y - records_y,
        footer_y=footer_y,
        stats_rows=stats_rows,
        chart_rows=chart_rows,
        chart_y=summary_rows + stats_rows,
    )


def footer_text(state: DashboardState) -> str:
    records = state.all_records if state.all_records else _records(state.snapshot)
    if records and 0 <= state.selected_row < len(records):
        selected = records[state.selected_row]
        if selected.get("status") != "success" and selected.get("errorMessage"):
            return f"错误: {selected['errorMessage']}"
    if state.error:
        return f"刷新错误 ({state.failure_count}/3): {state.error}"
    return "↑↓ 选择  ←→ 横移  PgUp/PgDn 翻页  Home/End 首尾  r 刷新  q 退出"


def footer_view(state: DashboardState, width: int) -> str:
    return slice_display(footer_text(state), state.column_offset, width)


def _color(pair: int) -> int:
    try:
        return curses.color_pair(pair)
    except curses.error:
        return 0


def _safe_addnstr(
    window: Any,
    y: int,
    x: int,
    text: str,
    length: int,
    attr: int = 0,
) -> None:
    if length <= 0:
        return
    try:
        window.addnstr(y, x, text, length, attr)
    except curses.error:
        pass


def _safe_pad_refresh(pad: Any, *args: int) -> None:
    try:
        pad.noutrefresh(*args)
    except curses.error:
        pass


def render_dashboard(
    stdscr: Any,
    state: DashboardState,
    wall_now: datetime,
    monotonic_now: float,
) -> None:
    rows, columns = stdscr.getmaxyx()
    stdscr.erase()
    summary_lines = build_summary_lines(state, wall_now, monotonic_now)
    layout = layout_for_size(rows, columns, len(summary_lines))
    if layout is None:
        warning = (
            f"终端太小: 当前 {columns}x{rows}，至少需要 "
            f"{MIN_COLS}x{MIN_ROWS}"
        )
        _safe_addnstr(
            stdscr, 0, 0, warning, max(1, columns - 1), _color(COLOR_WARNING)
        )
        try:
            stdscr.noutrefresh()
        except curses.error:
            pass
        try:
            curses.doupdate()
        except curses.error:
            pass
        return

    records = state.all_records if state.all_records else _records(state.snapshot)
    handle_key(
        state,
        -1,
        len(records),
        layout.visible_rows,
        max(1, columns - 1),
    )
    for y, line in enumerate(summary_lines):
        attr = _color(COLOR_ERROR) if y == 0 and state.error else 0
        _safe_addnstr(stdscr, y, 0, line, columns - 1, attr)

    stats_lines = build_stats_lines(state, columns - 1)
    for y, line in enumerate(stats_lines):
        _safe_addnstr(
            stdscr,
            layout.header_rows + y,
            0,
            line,
            columns - 1,
            _color(COLOR_HEADER),
        )

    chart = render_token_chart(
        state.token_buckets,
        bucket_minutes=CHART_BUCKET_MINUTES,
        width=min(MAX_CHART_WIDTH, columns - 2),
        height=layout.chart_rows,
    )
    for offset, line in enumerate(chart):
        _safe_addnstr(
            stdscr,
            layout.chart_y + offset,
            0,
            line,
            columns - 1,
        )

    full_footer = footer_text(state)
    footer = footer_view(state, columns - 1)
    footer_attr = (
        _color(COLOR_ERROR)
        if full_footer.startswith(("错误:", "刷新错误"))
        else 0
    )
    _safe_addnstr(
        stdscr, layout.footer_y, 0, footer, columns - 1, footer_attr
    )

    body_height = min(layout.visible_rows, max(1, len(records)))
    has_scrollbar = len(records) > body_height and body_height > 0
    table_max_col = columns - 2 if has_scrollbar else columns - 1
    _render_scrollbar(
        stdscr,
        state,
        len(records),
        body_height,
        layout.records_y,
        columns - 1,
    )
    try:
        stdscr.noutrefresh()
    except curses.error:
        pass

    pad_width = max(TABLE_WIDTH + 1, columns)
    table_width = table_max_col + 1
    table_offset = min(state.column_offset, max(0, TABLE_WIDTH - table_width))
    try:
        header_pad = curses.newpad(1, pad_width)
    except curses.error:
        return
    _safe_addnstr(
        header_pad,
        0,
        0,
        table_header_line(),
        TABLE_WIDTH,
        _color(COLOR_HEADER) | curses.A_BOLD,
    )
    _safe_pad_refresh(
        header_pad,
        0,
        table_offset,
        layout.table_header_y,
        0,
        layout.table_header_y,
        table_max_col,
    )

    try:
        record_pad = curses.newpad(body_height, pad_width)
    except curses.error:
        return
    if records:
        visible_end = min(len(records), state.row_offset + body_height)
        for pad_row, record_index in enumerate(
            range(state.row_offset, visible_end)
        ):
            record = records[record_index]
            status = record.get("status")
            if status == "success":
                attr = _color(COLOR_SUCCESS)
            elif status == "failed":
                attr = _color(COLOR_ERROR)
            else:
                attr = 0
            if record_index == state.selected_row:
                attr |= curses.A_REVERSE
            _safe_addnstr(
                record_pad,
                pad_row,
                0,
                state.record_lines[record_index],
                TABLE_WIDTH,
                attr,
            )
    else:
        _safe_addnstr(record_pad, 0, 0, "暂无调用记录", TABLE_WIDTH)

    _safe_pad_refresh(
        record_pad,
        0,
        table_offset,
        layout.records_y,
        0,
        layout.records_y + body_height - 1,
        table_max_col,
    )
    try:
        curses.doupdate()
    except curses.error:
        pass


def _render_scrollbar(
    stdscr: Any,
    state: DashboardState,
    record_count: int,
    body_height: int,
    records_y: int,
    max_col: int,
) -> None:
    """在记录区最右列绘制滚动条（仅在可滚动时）。"""
    if record_count <= body_height or body_height <= 0:
        return
    max_row_offset = record_count - body_height
    thumb_size = max(1, body_height * body_height // record_count)
    thumb_top = (state.row_offset * (body_height - thumb_size)) // max_row_offset
    thumb_top = min(max(0, thumb_top), body_height - thumb_size)
    for offset in range(body_height):
        y = records_y + offset
        char = "█" if thumb_top <= offset < thumb_top + thumb_size else "│"
        _safe_addnstr(stdscr, y, max_col, char, 1, _color(COLOR_HEADER))


def init_curses(stdscr: Any) -> None:
    stdscr.keypad(True)
    stdscr.timeout(100)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(COLOR_ERROR, curses.COLOR_RED, -1)
            curses.init_pair(COLOR_SUCCESS, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_WARNING, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
    except curses.error:
        pass
