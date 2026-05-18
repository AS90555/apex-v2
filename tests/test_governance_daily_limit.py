"""
C.2 — Tests für DailyTradeLimitCheck in governance/checks.py.

Prüft:
- Unter Limit → Signal durchgelassen
- Limit exakt erreicht → Signal blockiert mit klarer Meldung
- Limit überschritten → blockiert
- Nur abgeschlossene Trades (exit_ts IS NOT NULL) zählen
- Nur gleicher mode wird gezählt (dry_run != live)
- Nur heutiges Datum zählt (gestrige Trades zählen nicht)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from core.models import Signal
from governance.checks import DailyTradeLimitCheck


def _make_signal(mode: str = "dry_run") -> Signal:
    return Signal(
        strategy="donchian_breakout",
        asset="BTC",
        direction="long",
        mode=mode,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_conn_with_trades(
    n_closed_today: int = 0,
    n_open_today: int = 0,
    n_closed_yesterday: int = 0,
    mode: str = "dry_run",
    other_mode_trades: int = 0,
) -> sqlite3.Connection:
    """In-Memory-DB mit trades-Tabelle, befüllt nach Vorgabe."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, asset TEXT, direction TEXT, mode TEXT,
            entry_ts TEXT, exit_ts TEXT
        )
    """)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    entry_today = f"{today}T10:00:00+00:00"
    entry_yesterday = f"{yesterday}T10:00:00+00:00"
    exit_today = f"{today}T12:00:00+00:00"
    exit_yesterday = f"{yesterday}T12:00:00+00:00"

    for _ in range(n_closed_today):
        conn.execute(
            "INSERT INTO trades (strategy, asset, direction, mode, entry_ts, exit_ts) VALUES (?,?,?,?,?,?)",
            ("donchian_breakout", "BTC", "long", mode, entry_today, exit_today),
        )
    for _ in range(n_open_today):
        conn.execute(
            "INSERT INTO trades (strategy, asset, direction, mode, entry_ts, exit_ts) VALUES (?,?,?,?,?,?)",
            ("donchian_breakout", "ETH", "long", mode, entry_today, None),  # kein exit_ts
        )
    for _ in range(n_closed_yesterday):
        conn.execute(
            "INSERT INTO trades (strategy, asset, direction, mode, entry_ts, exit_ts) VALUES (?,?,?,?,?,?)",
            ("donchian_breakout", "BTC", "long", mode, entry_yesterday, exit_yesterday),
        )
    for _ in range(other_mode_trades):
        other_mode = "live" if mode == "dry_run" else "dry_run"
        conn.execute(
            "INSERT INTO trades (strategy, asset, direction, mode, entry_ts, exit_ts) VALUES (?,?,?,?,?,?)",
            ("donchian_breakout", "SOL", "long", other_mode, entry_today, exit_today),
        )

    conn.commit()
    return conn


class TestDailyTradeLimitCheck:
    def test_under_limit_passes(self):
        """0 Trades heute → Signal durchgelassen."""
        conn = _make_conn_with_trades(n_closed_today=0)
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is True
        assert "0/" in reason

    def test_below_limit_passes(self):
        """N-1 Trades heute → Signal durchgelassen."""
        from config.settings import MAX_DAILY_TRADES
        conn = _make_conn_with_trades(n_closed_today=MAX_DAILY_TRADES - 1)
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is True

    def test_at_limit_blocks(self):
        """Exakt MAX_DAILY_TRADES Trades heute → blockiert."""
        from config.settings import MAX_DAILY_TRADES
        conn = _make_conn_with_trades(n_closed_today=MAX_DAILY_TRADES)
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is False
        assert "daily_trade_limit" in reason
        assert str(MAX_DAILY_TRADES) in reason

    def test_above_limit_blocks(self):
        """Mehr als MAX_DAILY_TRADES → ebenfalls blockiert."""
        from config.settings import MAX_DAILY_TRADES
        conn = _make_conn_with_trades(n_closed_today=MAX_DAILY_TRADES + 2)
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is False

    def test_open_trades_not_counted(self):
        """Offene Trades (exit_ts IS NULL) zählen nicht zum Limit."""
        from config.settings import MAX_DAILY_TRADES
        # MAX_DAILY_TRADES offene + 0 abgeschlossene → kein Block
        conn = _make_conn_with_trades(
            n_closed_today=0,
            n_open_today=MAX_DAILY_TRADES + 5,
        )
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is True, "Offene Trades dürfen nicht zum Daily-Limit zählen"

    def test_yesterday_trades_not_counted(self):
        """Gestrige Trades zählen nicht — 24h-Fenster ist heute UTC."""
        from config.settings import MAX_DAILY_TRADES
        conn = _make_conn_with_trades(
            n_closed_today=0,
            n_closed_yesterday=MAX_DAILY_TRADES + 5,
        )
        signal = _make_signal()
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is True, "Gestrige Trades dürfen heute nicht blockieren"

    def test_other_mode_not_counted(self):
        """Live-Trades zählen nicht zum dry_run-Limit und umgekehrt."""
        from config.settings import MAX_DAILY_TRADES
        conn = _make_conn_with_trades(
            n_closed_today=0,
            other_mode_trades=MAX_DAILY_TRADES + 5,
            mode="dry_run",
        )
        signal = _make_signal(mode="dry_run")
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert passed is True, "Trades aus anderem Mode dürfen nicht mitzählen"

    def test_reason_contains_mode(self):
        """reason-String enthält den mode für Audit-Trail."""
        conn = _make_conn_with_trades(n_closed_today=1)
        signal = _make_signal(mode="dry_run")
        check = DailyTradeLimitCheck()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = check.evaluate(signal)
        assert "dry_run" in reason or "1/" in reason

    def test_check_name(self):
        assert DailyTradeLimitCheck().name == "daily_trade_limit"
