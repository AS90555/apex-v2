"""
T1.B — Tests für /lab_decide Bot-Command.

Prüft:
- nicht autorisiert → abgewiesen
- fehlende Argumente → Hilfe-Text
- ungültige Decision → Fehler
- Queue-ID nicht gefunden → Fehler
- falscher Status (nicht paused_inconclusive) → Fehler mit aktuellem Status
- full_run → Status auf 'queued' gesetzt + governance_event
- skip → Status auf 'skipped' gesetzt
- archive → Status auf 'archived', NC-Eintrag erstellt
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.telegram_bot import cmd_lab_decide


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_update(user_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_ctx(*args) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


_URI = "file:lab_decide_test?mode=memory&cache=shared"


def _make_conn(queue_status: str = "paused_inconclusive") -> sqlite3.Connection:
    conn = sqlite3.connect(_URI, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(f"""
        DROP TABLE IF EXISTS governance_audit_log;
        DROP TABLE IF EXISTS negative_controls;
        DROP TABLE IF EXISTS lab_queue;
        CREATE TABLE lab_queue (
            id INTEGER PRIMARY KEY, strategy TEXT, asset TEXT, status TEXT
        );
        CREATE TABLE negative_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL, asset TEXT NOT NULL, study_hash TEXT NOT NULL,
            no_go_reason TEXT NOT NULL, closed_at TEXT, closed_reason TEXT,
            closed_by TEXT, created_at TEXT
        );
        CREATE TABLE governance_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, entity_type TEXT, entity_id INTEGER,
            actor TEXT, reason TEXT, metadata TEXT, created_at TEXT
        );
        INSERT INTO lab_queue VALUES (42, 'donchian', 'BTC', '{queue_status}');
    """)
    conn.commit()
    return conn


def _open_shared() -> sqlite3.Connection:
    c = sqlite3.connect(_URI, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


class TestCmdLabDecide:
    def test_unauthorized_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=False):
            _run(cmd_lab_decide(update, _make_ctx("42", "full_run")))
        msg = update.message.reply_text.call_args[0][0]
        assert "autorisiert" in msg.lower()

    def test_no_args_shows_usage(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            _run(cmd_lab_decide(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "lab_decide" in msg.lower() or "nutzung" in msg.lower()

    def test_invalid_decision(self):
        update = _make_update()
        conn = _make_conn()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", return_value=conn):
                _run(cmd_lab_decide(update, _make_ctx("42", "run_it")))
        msg = update.message.reply_text.call_args[0][0]
        assert "ungültig" in msg.lower() or "erlaubt" in msg.lower()

    def test_queue_id_not_found(self):
        update = _make_update()
        conn = _make_conn()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", return_value=conn):
                _run(cmd_lab_decide(update, _make_ctx("999", "full_run")))
        msg = update.message.reply_text.call_args[0][0]
        assert "nicht gefunden" in msg.lower()

    def test_wrong_status_rejected(self):
        update = _make_update()
        conn = _make_conn(queue_status="completed")
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", return_value=conn):
                _run(cmd_lab_decide(update, _make_ctx("42", "full_run")))
        msg = update.message.reply_text.call_args[0][0]
        assert "completed" in msg

    def test_full_run_sets_queued(self):
        update = _make_update()
        anchor = _make_conn()  # hält shared-memory-DB am Leben
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                _run(cmd_lab_decide(update, _make_ctx("42", "full_run")))
        row = anchor.execute("SELECT status FROM lab_queue WHERE id=42").fetchone()
        assert row["status"] == "queued"

    def test_skip_sets_skipped(self):
        update = _make_update()
        anchor = _make_conn()  # noqa: F841
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                _run(cmd_lab_decide(update, _make_ctx("42", "skip")))
        row = anchor.execute("SELECT status FROM lab_queue WHERE id=42").fetchone()
        assert row["status"] == "skipped"

    def test_archive_creates_negative_control(self):
        update = _make_update()
        anchor = _make_conn()  # noqa: F841
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                _run(cmd_lab_decide(update, _make_ctx("42", "archive")))
        nc = anchor.execute("SELECT * FROM negative_controls WHERE strategy='donchian'").fetchone()
        assert nc is not None
        assert nc["no_go_reason"] == "operator_decision"
        assert nc["created_at"] is not None

    def test_governance_event_logged(self):
        update = _make_update()
        anchor = _make_conn()  # noqa: F841
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                _run(cmd_lab_decide(update, _make_ctx("42", "full_run")))
        row = anchor.execute("SELECT * FROM governance_audit_log").fetchone()
        assert row is not None
        assert row["event_type"] == "operator_lab_decide"
