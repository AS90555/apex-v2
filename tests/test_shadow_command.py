"""
E.3 — Tests für /shadow Bot-Command in monitor/telegram_bot.py.

Prüft:
- Erfolgreicher Mode-Wechsel: dry_run → shadow
- Audit-Log-Eintrag in kill_switch_events
- Schon im Shadow-Mode: Fehler-Antwort
- Deployment nicht gefunden: Fehler-Antwort
- Deployment inaktiv: Fehler-Antwort
- Nicht autorisierter User: abgewiesen
- Fehlende/ungültige Argumente: Hilfe-Text
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.telegram_bot import cmd_shadow


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_update(user_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_ctx(*args) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


_SHADOW_TEST_DB = "file:shadow_test?mode=memory&cache=shared"


def _make_conn(dep: dict | None, mode: str = "dry_run", active: int = 1) -> sqlite3.Connection:
    """Shared-memory DB — bleibt nach conn.close() erhalten solange ≥1 Verbindung offen."""
    conn = sqlite3.connect(_SHADOW_TEST_DB, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        DROP TABLE IF EXISTS active_deployments;
        DROP TABLE IF EXISTS kill_switch_events;
        CREATE TABLE active_deployments (
            id INTEGER PRIMARY KEY, strategy_key TEXT, asset TEXT,
            mode TEXT, active INTEGER
        );
        CREATE TABLE kill_switch_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, action TEXT, mode_from TEXT, mode_to TEXT,
            reason TEXT, cleared_by TEXT, asset TEXT
        );
    """)
    if dep:
        conn.execute(
            "INSERT INTO active_deployments VALUES (?,?,?,?,?)",
            (dep["id"], dep["strategy_key"], dep["asset"], mode, active),
        )
    conn.commit()
    return conn


def _open_shared() -> sqlite3.Connection:
    c = sqlite3.connect(_SHADOW_TEST_DB, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


class TestCmdShadow:
    def test_unauthorized_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=False):
            _run(cmd_shadow(update, _make_ctx("1")))
        msg = update.message.reply_text.call_args[0][0]
        assert "autorisiert" in msg.lower()

    def test_no_args_shows_usage(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            _run(cmd_shadow(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "shadow" in msg.lower()

    def test_invalid_arg_shows_usage(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            _run(cmd_shadow(update, _make_ctx("abc")))
        msg = update.message.reply_text.call_args[0][0]
        assert "shadow" in msg.lower()

    def test_deployment_not_found(self):
        anchor = _make_conn(dep=None)  # noqa: F841 — hält shared-memory-DB am Leben
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("99")))
        msg = update.message.reply_text.call_args[0][0]
        assert "nicht gefunden" in msg

    def test_already_shadow_mode_rejected(self):
        anchor = _make_conn(dep={"id": 1, "strategy_key": "strat", "asset": "BTC"}, mode="shadow")  # noqa: F841
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("1")))
        msg = update.message.reply_text.call_args[0][0]
        assert "bereits" in msg

    def test_inactive_deployment_rejected(self):
        anchor = _make_conn(dep={"id": 1, "strategy_key": "strat", "asset": "BTC"},  # noqa: F841
                            mode="dry_run", active=0)
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("1")))
        msg = update.message.reply_text.call_args[0][0]
        assert "nicht aktiv" in msg

    def test_successful_shadow_sets_mode(self):
        anchor = _make_conn(dep={"id": 1, "strategy_key": "donchian", "asset": "BTC"}, mode="dry_run")  # noqa: F841
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("1")))
        row = anchor.execute("SELECT mode FROM active_deployments WHERE id=1").fetchone()
        assert row["mode"] == "shadow"

    def test_successful_shadow_writes_audit_log(self):
        anchor = _make_conn(dep={"id": 1, "strategy_key": "donchian", "asset": "BTC"}, mode="dry_run")  # noqa: F841
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("1")))
        row = anchor.execute("SELECT * FROM kill_switch_events").fetchone()
        assert row is not None
        assert row["action"] == "shadow_set"
        assert row["mode_from"] == "dry_run"
        assert row["mode_to"] == "shadow"

    def test_successful_shadow_reply_confirms(self):
        anchor = _make_conn(dep={"id": 1, "strategy_key": "donchian", "asset": "BTC"}, mode="dry_run")  # noqa: F841
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("monitor.telegram_bot.get_connection", side_effect=_open_shared):
                _run(cmd_shadow(update, _make_ctx("1")))
        msg = update.message.reply_text.call_args[0][0]
        assert "shadow" in msg.lower()
