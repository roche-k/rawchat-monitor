"""Command-line setup and lifecycle for the RawChat monitor."""

import argparse
import curses
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import (
    MultiAccountClient,
    RefreshEngine,
    RefreshOutcome,
    RefreshWorker,
)
from .codex_config import CodexConfigManager
from .config import (
    BASE_URL,
    DEFAULT_ACCOUNTS_FILE,
    LOG_DIR,
    REFRESH_INTERVAL,
    _require_socks,
    load_accounts,
    load_proxy_config,
)
from .dashboard import (
    DashboardState,
    _records,
    apply_outcome,
    build_summary_lines,
    handle_key,
    init_curses,
    layout_for_size,
    load_proxy_metrics,
    load_proxy_latency_matches,
    render_dashboard,
)
from .proxy import RawChatProxyServer
from .records import RecordStore
from .sources import ApiKeyCache, SourcePool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RawChat Codex monitor")
    parser.add_argument(
        "--proxy-port",
        type=int,
        default=int(os.environ.get("RAWCHAT_PROXY_PORT", "15722")),
        help="local proxy port",
    )
    parser.add_argument(
        "--upstream-url",
        default=os.environ.get("RAWCHAT_UPSTREAM_URL", BASE_URL),
        help="RawChat upstream base URL",
    )
    parser.add_argument(
        "accounts_file",
        nargs="?",
        help="external TOML file containing [[accounts]] credentials",
    )
    parser.add_argument(
        "--key-cache",
        default=os.environ.get(
            "RAWCHAT_KEY_CACHE",
            str(Path.home() / ".cache/rawchat-monitor/api_keys.json"),
        ),
        help="API key cache path",
    )
    parser.add_argument(
        "--codex-config",
        default=os.environ.get(
            "RAWCHAT_CODEX_CONFIG",
            str(Path.home() / ".codex/config.toml"),
        ),
        help="Codex config path",
    )
    parser.add_argument(
        "--no-apply-codex-config",
        dest="apply_codex_config",
        action="store_false",
        default=True,
        help="do not take over the Codex config",
    )
    return parser.parse_args(argv)


class MonitorRuntime:
    """拥有共享 source pool 的代理、刷新 worker 和配置生命周期。"""

    def __init__(
        self,
        worker: RefreshWorker,
        proxy: RawChatProxyServer,
        config_manager: CodexConfigManager,
        apply_codex_config: bool = True,
    ) -> None:
        self.worker = worker
        self.proxy = proxy
        self.config_manager = config_manager
        self.apply_codex_config = apply_codex_config
        self.source_pool = proxy.source_pool
        self._started = False
        self._stopped = False
        self._config_applied = False

    def start(self) -> bool:
        if self._started:
            raise RuntimeError("monitor runtime 已经启动")
        self.proxy.start()
        try:
            self.worker.start()
            refreshing = self.worker.request_refresh()
        except Exception:
            self.proxy.stop()
            raise
        self._started = True
        return refreshing

    def handle_outcome(self, outcome: RefreshOutcome) -> str | None:
        if (
            self.apply_codex_config
            and not self._config_applied
            and outcome.error is None
            and outcome.snapshot is not None
        ):
            try:
                self.config_manager.apply()
            except Exception as exc:
                return f"配置接管失败: {exc}"
            else:
                self._config_applied = True
        return None

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.worker.stop()
        self.proxy.stop()


def build_runtime(args: argparse.Namespace) -> MonitorRuntime:
    accounts_path = args.accounts_file or DEFAULT_ACCOUNTS_FILE
    accounts = load_accounts(accounts_path)
    proxy_config = load_proxy_config(accounts_path)
    key_cache = ApiKeyCache(args.key_cache)
    cached_keys = {
        account["email"]: key_cache.get(account["email"])
        for account in accounts
        if key_cache.get(account["email"])
    }
    source_pool = SourcePool(
        accounts,
        keys={email: key for email, key in cached_keys.items() if key},
    )
    client = MultiAccountClient(
        accounts,
        key_cache=key_cache,
        source_pool=source_pool,
        proxy=proxy_config,
    )
    worker = RefreshWorker(RefreshEngine(client))
    proxy = RawChatProxyServer(
        source_pool,
        args.upstream_url,
        port=args.proxy_port,
        event_log_dir=LOG_DIR,
        proxy=proxy_config,
    )
    if proxy_config is not None:
        try:
            _require_socks(proxy_config)
        except RuntimeError:
            proxy_config.mark_failed(reason="PySocks 未安装")
    config_manager = CodexConfigManager(
        args.codex_config,
        port=args.proxy_port,
    )
    return MonitorRuntime(
        worker,
        proxy,
        config_manager,
        apply_codex_config=args.apply_codex_config,
    )


def default_runtime_factory() -> MonitorRuntime:
    return build_runtime(parse_args([]))


def default_worker_factory() -> RefreshWorker:
    accounts = load_accounts(DEFAULT_ACCOUNTS_FILE)
    proxy_config = load_proxy_config(DEFAULT_ACCOUNTS_FILE)
    if proxy_config is not None:
        try:
            _require_socks(proxy_config)
        except RuntimeError:
            proxy_config.mark_failed(reason="PySocks 未安装")
    return RefreshWorker(
        RefreshEngine(MultiAccountClient(accounts, proxy=proxy_config))
    )


def drain_refresh_results(
    worker: RefreshWorker,
    state: DashboardState,
    store: RecordStore | None = None,
    on_outcome: Any = None,
) -> bool:
    changed = False
    while True:
        outcome = worker.get_result()
        if outcome is None:
            return changed
        apply_outcome(state, outcome, store=store)
        changed = True
        if callable(on_outcome):
            try:
                callback_error = on_outcome(outcome)
            except Exception as exc:
                callback_error = f"刷新结果处理失败: {exc}"
            if callback_error:
                state.error = str(callback_error)
                state.failure_count = max(1, state.failure_count)


def handle_key_for_screen(
    state: DashboardState, key: int, screen_size: tuple[int, int]
) -> str | None:
    rows, columns = screen_size
    summary_count = len(build_summary_lines(state, datetime.now(), 0.0))
    layout = layout_for_size(rows, columns, summary_count)
    visible_rows = layout.visible_rows if layout else 1
    history = state.all_records if state.all_records else _records(state.snapshot)
    return handle_key(
        state,
        key,
        len(history),
        visible_rows,
        max(1, columns - 1),
    )


def run_dashboard(
    stdscr: Any,
    worker_factory: Any = None,
    runtime_factory: Any = None,
) -> None:
    init_curses(stdscr)
    runtime = None
    if worker_factory is None:
        runtime = (runtime_factory or default_runtime_factory)()
        worker = runtime.worker
    else:
        worker = worker_factory()
    store = RecordStore(log_dir=LOG_DIR)
    started_at = time.monotonic()
    initial_records = store.all_records()
    proxy_request_total, proxy_avg_first_byte_ms, proxy_avg_response_ms = (
        load_proxy_metrics(LOG_DIR)
    )
    proxy_latency_by_record_key = load_proxy_latency_matches(
        LOG_DIR,
        initial_records,
        datetime.now(),
        runtime.source_pool if runtime is not None else None,
    )
    state = DashboardState(
        next_refresh_at=started_at + REFRESH_INTERVAL,
        all_records=initial_records,
        source_pool=runtime.source_pool if runtime is not None else None,
        proxy_request_total=proxy_request_total,
        proxy_avg_first_byte_ms=proxy_avg_first_byte_ms,
        proxy_avg_response_ms=proxy_avg_response_ms,
        proxy_latency_by_record_key=proxy_latency_by_record_key,
        proxy_config=(
            getattr(runtime.proxy, "proxy", None)
            if runtime is not None
            else None
        ),
    )
    if runtime is not None:
        state.refreshing = runtime.start()
    else:
        worker.start()
        state.refreshing = worker.request_refresh()

    needs_render = True
    last_render_second: int | None = None
    last_screen_size: tuple[int, int] | None = None
    try:
        while True:
            if drain_refresh_results(
                worker,
                state,
                store=store,
                on_outcome=runtime.handle_outcome if runtime else None,
            ):
                needs_render = True
            monotonic_now = time.monotonic()
            if monotonic_now >= state.next_refresh_at and not state.refreshing:
                state.refreshing = worker.request_refresh()
                if state.refreshing:
                    state.next_refresh_at = monotonic_now + REFRESH_INTERVAL
                    needs_render = True

            screen_size = stdscr.getmaxyx()
            render_second = int(monotonic_now)
            if (
                render_second != last_render_second
                or screen_size != last_screen_size
            ):
                needs_render = True
            if needs_render:
                render_dashboard(stdscr, state, datetime.now(), monotonic_now)
                needs_render = False
                last_render_second = render_second
                last_screen_size = screen_size

            key = stdscr.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                try:
                    curses.update_lines_cols()
                except (AttributeError, curses.error):
                    pass
                needs_render = True
                last_screen_size = None
                continue
            action = handle_key_for_screen(state, key, screen_size)
            if action == "quit":
                return
            needs_render = True
            if action == "refresh" and not state.refreshing:
                state.refreshing = worker.request_refresh()
                if state.refreshing:
                    state.next_refresh_at = monotonic_now + REFRESH_INTERVAL
    finally:
        if runtime is not None:
            runtime.stop()
        else:
            worker.stop()


def main() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "需要在交互式终端中运行 rawchat_monitor.py",
            file=sys.stderr,
        )
        raise SystemExit(2)
    args = parse_args()
    try:
        curses.wrapper(
            lambda stdscr: run_dashboard(
                stdscr,
                runtime_factory=lambda: build_runtime(args),
            )
        )
    except KeyboardInterrupt:
        pass
