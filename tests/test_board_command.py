"""
E.5 / T1.D — Tests für /board Operator-Dashboard.

Prüft:
- Nicht-autorisierter User → abgewiesen
- Antwort enthält alle 9 KPIs (6 original + 3 T1.D)
- Watchdog-Status: alive/stale korrekt angezeigt
- DB-Tabelle fehlt → kein Crash (n/a oder –)
- Session-Limit-Stand korrekt angezeigt
- Funding-Warnung bei hohem Rate
- Fehlende funding_rates-Tabelle → n/a ohne Crash
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.telegram_bot import cmd_board


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_update() -> MagicMock:
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = 12345
    return update


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.args = []
    return ctx


def _make_conn(
    *,
    include_funding: bool = False,
    funding_rate: float = 0.0001,
    trades_today: int = 1,
) -> sqlite3.Connection:
    today = "2026-05-18"
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row

    trade_rows = ""
    # offener Trade (kein exit_ts)
    trade_rows += "INSERT INTO trades VALUES (1, NULL, NULL);\n"
    # heute abgeschlossene Trades
    for i in range(trades_today):
        trade_rows += (
            f"INSERT INTO trades VALUES ({i+2}, '{today}T10:00:00', -0.5);\n"
        )
    # gestern abgeschlossener Trade (zählt nicht für heute)
    trade_rows += "INSERT INTO trades VALUES (99, '2026-05-17T10:00:00', 0.3);\n"

    funding_tables = ""
    if include_funding:
        funding_tables = f"""
            CREATE TABLE funding_rates (
                id INTEGER PRIMARY KEY, asset TEXT, funding_rate REAL, funding_time TEXT
            );
            CREATE TABLE active_deployments (
                id INTEGER PRIMARY KEY, asset TEXT, strategy_key TEXT, active INTEGER
            );
            INSERT INTO active_deployments VALUES (1, 'BTC', 'donchian', 1);
            INSERT INTO funding_rates VALUES (1, 'BTC', {funding_rate}, '{today}T08:00:00');
        """

    conn.executescript(f"""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, exit_ts TEXT, pnl_r REAL);
        CREATE TABLE lab_cycles (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE negative_controls (
            id INTEGER PRIMARY KEY, closed_at TEXT
        );
        CREATE TABLE lab_discoveries (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO system_state VALUES ('daily_drawdown', '-0.0123');
        {trade_rows}
        INSERT INTO lab_cycles VALUES (5, 'completed');
        INSERT INTO negative_controls VALUES (1, NULL);
        INSERT INTO negative_controls VALUES (2, '2026-05-17T00:00:00');
        INSERT INTO lab_discoveries VALUES (1, 'approved');
        {funding_tables}
    """)
    conn.commit()
    return conn


_ALIVE_STATUS = {"alive": True, "age_min": 2.0, "source": "file_heartbeats"}
_STALE_STATUS = {"alive": False, "age_min": 20.0, "source": "file_heartbeats"}


class TestCmdBoard:
    def test_unauthorized_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=False):
            _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "autorisiert" in msg.lower()

    def test_board_contains_9_kpis(self):
        conn = _make_conn()
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", return_value=conn):
                with patch("scripts.master_watchdog.check_master_alive",
                           return_value=_ALIVE_STATUS):
                    _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        # Original 6
        assert "Live-DD" in msg
        assert "Offene Positionen" in msg
        assert "Letzter Cycle" in msg
        assert "Aktive NCs" in msg
        assert "Promotion-Kandidaten" in msg
        assert "Watchdog" in msg
        # T1.D: neue 3
        assert "Trades heute" in msg
        assert "DD heute" in msg
        assert "Funding" in msg

    def test_board_shows_correct_values(self):
        conn = _make_conn()
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", return_value=conn):
                with patch("scripts.master_watchdog.check_master_alive",
                           return_value=_ALIVE_STATUS):
                    _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "-0.0123" in msg   # daily_drawdown
        assert "#5" in msg         # cycle id
        assert "completed" in msg

    def test_watchdog_alive_shows_ok(self):
        conn = _make_conn()
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", return_value=conn):
                with patch("scripts.master_watchdog.check_master_alive",
                           return_value=_ALIVE_STATUS):
                    _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "OK" in msg

    def test_watchdog_stale_shows_alarm(self):
        conn = _make_conn()
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", return_value=conn):
                with patch("scripts.master_watchdog.check_master_alive",
                           return_value=_STALE_STATUS):
                    _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "STALE" in msg


def _run_board(conn, watchdog_status=None):
    """Hilfsfunktion: /board ausführen und Antwortnachricht zurückgeben."""
    update = _make_update()
    ws = watchdog_status or _ALIVE_STATUS
    with patch("monitor.telegram_bot._is_authorized", return_value=True):
        with patch("monitor.telegram_bot.get_connection", return_value=conn):
            with patch("scripts.master_watchdog.check_master_alive", return_value=ws):
                _run(cmd_board(update, _make_ctx()))
    return update.message.reply_text.call_args[0][0]


class TestBoardSessionLimit:
    def test_session_below_limit(self):
        conn = _make_conn(trades_today=1)
        msg = _run_board(conn)
        assert "1/3 heute" in msg
        assert "⚠️" not in msg.split("Trades heute")[1].split("\n")[0]

    def test_session_at_limit_shows_warning(self):
        conn = _make_conn(trades_today=3)
        msg = _run_board(conn)
        assert "3/3 heute" in msg
        assert "⚠️" in msg

    def test_session_zero_trades(self):
        conn = _make_conn(trades_today=0)
        msg = _run_board(conn)
        assert "0/3 heute" in msg


class TestBoardDDToday:
    def test_dd_today_field_present(self):
        conn = _make_conn(trades_today=1)
        msg = _run_board(conn)
        assert "DD heute" in msg
        assert "Kill bei" in msg

    def test_dd_today_shows_kill_threshold(self):
        conn = _make_conn(trades_today=0)
        msg = _run_board(conn)
        # DAILY_DD_KILL_R = -2.0
        assert "-2.0R" in msg


class TestBoardFunding:
    def test_no_funding_table_shows_na(self):
        conn = _make_conn(include_funding=False)
        msg = _run_board(conn)
        assert "Funding" in msg
        assert "n/a" in msg

    def test_low_funding_no_warning(self):
        conn = _make_conn(include_funding=True, funding_rate=0.0001)
        msg = _run_board(conn)
        assert "BTC" in msg
        # FUNDING_RATE_WARN_THRESHOLD = 0.0005 → 0.0001 ist kein Warn
        funding_section = msg.split("Funding")[1].split("\n")[0]
        assert "⚠️" not in funding_section

    def test_high_funding_shows_warning(self):
        conn = _make_conn(include_funding=True, funding_rate=0.0010)
        msg = _run_board(conn)
        # rate 0.001 > threshold 0.0005 → ⚠️
        funding_section = msg.split("Funding")[1].split("\n")[0]
        assert "⚠️" in funding_section
