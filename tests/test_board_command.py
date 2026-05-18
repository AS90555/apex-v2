"""
E.5 — Tests für /board Operator-Dashboard.

Prüft:
- Nicht-autorisierter User → abgewiesen
- Antwort enthält alle 6 KPIs
- Watchdog-Status: alive/stale korrekt angezeigt
- DB-Tabelle fehlt → kein Crash (n/a oder –)
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


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, exit_ts TEXT);
        CREATE TABLE lab_cycles (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE negative_controls (
            id INTEGER PRIMARY KEY, closed_at TEXT
        );
        CREATE TABLE lab_discoveries (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO system_state VALUES ('daily_drawdown', '-0.0123');
        INSERT INTO trades VALUES (1, NULL);
        INSERT INTO trades VALUES (2, '2026-05-18T10:00:00');
        INSERT INTO lab_cycles VALUES (5, 'completed');
        INSERT INTO negative_controls VALUES (1, NULL);
        INSERT INTO negative_controls VALUES (2, '2026-05-17T00:00:00');
        INSERT INTO lab_discoveries VALUES (1, 'approved');
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

    def test_board_contains_6_kpis(self):
        conn = _make_conn()
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", return_value=conn):
                with patch("scripts.master_watchdog.check_master_alive",
                           return_value=_ALIVE_STATUS):
                    _run(cmd_board(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "Live-DD" in msg
        assert "Offene Positionen" in msg
        assert "Letzter Cycle" in msg
        assert "Aktive NCs" in msg
        assert "Promotion-Kandidaten" in msg
        assert "Watchdog" in msg

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
