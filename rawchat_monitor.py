#!/usr/bin/env python3
"""Compatibility facade and executable entry point for RawChat Monitor.

The implementation is split by responsibility under :mod:`rawchat`.
Existing users can continue importing this module or running this script.
"""

import curses
import requests
import sys
import time
import types

from rawchat import client as _client_module
from rawchat import config as _config_module
from rawchat import dashboard as _dashboard_module
from rawchat import proxy as _proxy_module
from rawchat import records as _records_module
from rawchat import runtime as _runtime_module

from rawchat.client import (
    MultiAccountClient,
    RawChatClient,
    RawChatError,
    RefreshCancelled,
    RefreshEngine,
    RefreshOutcome,
    RefreshWorker,
    collect_snapshot,
)
from rawchat.codex_config import CodexConfigManager
from rawchat.config import (
    ACCOUNT_REQUEST_GAP,
    API_BASE_URL,
    BALANCE_URL,
    BASE_URL,
    CHART_BUCKET_MINUTES,
    CHART_RESERVED,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_SUCCESS,
    COLOR_WARNING,
    DEFAULT_ACCOUNTS_FILE,
    GETME_URL,
    HEADERS,
    LOGIN_URL,
    LOG_DIR,
    MAX_CHART_WIDTH,
    MIN_COLS,
    MIN_ROWS,
    ProxyConfig,
    PROXY_HEALTHCHECK_URL,
    PROXY_HEALTHCHECK_TIMEOUT,
    QUOTA_URL,
    RECORD_LIMIT,
    RECORDS_URL,
    REFRESH_INTERVAL,
    REQUEST_TIMEOUT,
    ROLLING_LIMIT_URL,
    STATS_RESERVED,
    UPSTREAM_CONNECT_TIMEOUT,
    UPSTREAM_READ_TIMEOUT,
    WORKER_STOP_TIMEOUT,
    _require_socks,
    build_xray_config,
    load_accounts,
    load_proxy_config,
    tomllib,
)
from rawchat.dashboard import (
    DashboardState,
    ScreenLayout,
    TABLE_COLUMNS,
    TABLE_WIDTH,
    _balance_text,
    _color,
    _dict,
    _records,
    _render_scrollbar,
    _rolling_text,
    _safe_addnstr,
    _safe_pad_refresh,
    _subs_text,
    _table_line,
    apply_outcome,
    build_stats_lines,
    build_summary_lines,
    build_token_chart,
    char_width,
    compute_statistics,
    display_width,
    fit_cell,
    fmt_cost,
    fmt_discount,
    fmt_duration,
    fmt_request_time,
    fmt_tokens,
    footer_text,
    footer_view,
    handle_key,
    init_curses,
    layout_for_size,
    load_proxy_latency_matches,
    load_proxy_metrics,
    match_proxy_latencies,
    record_values,
    render_dashboard,
    slice_display,
    table_header_line,
    table_record_line,
    total_input_tokens,
    total_io_tokens,
    total_output_tokens,
    uncached_input_tokens,
    uncached_output_tokens,
)
from rawchat.proxy import RawChatProxyServer, _codex_quota_headers
from rawchat.records import (
    DashboardSnapshot,
    RecordStore,
    _log_date,
    _number,
    _parse_datetime,
    _request_sort_key,
    normalize_codex_data,
    record_key,
)
from rawchat.runtime import (
    MonitorRuntime,
    build_runtime,
    default_runtime_factory,
    default_worker_factory,
    drain_refresh_results,
    handle_key_for_screen,
    main,
    parse_args,
    run_dashboard,
)
from rawchat.sources import (
    ApiKeyCache,
    QUOTA_EXHAUSTED_MESSAGE,
    QUOTA_KEYWORDS,
    SourcePool,
    SourceState,
    _contains_quota_exhausted_message,
    _parse_release_at,
    is_quota_error,
    is_quota_exhausted,
)


_CONFIG_MODULES = (
    _config_module,
    _client_module,
    _dashboard_module,
    _proxy_module,
    _records_module,
    _runtime_module,
)
_CONFIG_NAMES = frozenset(
    name for name in vars(_config_module) if name.isupper()
)


class _CompatibilityFacadeModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _CONFIG_NAMES:
            for module in _CONFIG_MODULES:
                if name in vars(module):
                    setattr(module, name, value)


sys.modules[__name__].__class__ = _CompatibilityFacadeModule


if __name__ == "__main__":
    main()
