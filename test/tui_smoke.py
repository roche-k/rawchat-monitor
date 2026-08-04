import curses
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rawchat_monitor as monitor


def smoke(stdscr):
    records = [
        {
            "requestId": str(index),
            "requestTime": f"2026-07-14T10:{index:02d}:00",
            "model": "gpt-5-codex",
            "inputTokens": 1000 + index,
            "outputTokens": 200,
            "cacheInputTokens": 300,
            "cacheWriteTokens": 0,
            "reasoningTokens": 50,
            "totalTokens": 1550 + index,
            "rawCost": 0.1,
            "discountRate": 0.8,
            "discountAmount": 0.02,
            "cost": 0.08,
            "ip": "127.0.0.1",
            "responseTime": 1200,
            "firstByteTime": 300,
            "status": "success",
            "errorMessage": "",
        }
        for index in range(20)
    ]
    snapshot = monitor.DashboardSnapshot(
        {
            "subscriptions": None,
            "currentUsage": None,
            "recentRecords": records,
        },
        None,
        None,
        datetime(2026, 7, 14, 10, 20),
    )
    state = monitor.DashboardState(
        snapshot=snapshot,
        last_success=snapshot.fetched_at,
        next_refresh_at=60.0,
    )
    monitor.init_curses(stdscr)
    curses.resizeterm(30, 120)
    monitor.render_dashboard(stdscr, state, snapshot.fetched_at, 0.0)
    curses.resizeterm(35, 160)
    monitor.render_dashboard(stdscr, state, snapshot.fetched_at, 0.0)


if __name__ == "__main__":
    curses.wrapper(smoke)
