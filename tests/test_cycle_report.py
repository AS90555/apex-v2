"""
E.4 — Tests für aggregierten Cycle-Report (_send_cycle_report).

Prüft:
- Bericht enthält Cycle-ID, Queue-Statistiken, Negative-Controls-Anzahl, Top-Variant
- Keine Top-Variant → "–" im Bericht
- Genau 1 Telegram-Nachricht (kein Spam)
- DB-Fehler → Fallback-Nachricht (kein Crash)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.lab_controller import _send_cycle_report


def _make_lab_conn(cycle_id: int = 1) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(f"""
        CREATE TABLE lab_cycles (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE lab_queue (
            id INTEGER PRIMARY KEY, cycle_id INTEGER, strategy TEXT, asset TEXT,
            status TEXT, skip_reason TEXT
        );
        CREATE TABLE negative_controls (
            id INTEGER PRIMARY KEY, strategy TEXT, asset TEXT, study_hash TEXT,
            closed_at TEXT, closed_reason TEXT, closed_by TEXT, created_at TEXT
        );
        CREATE TABLE fitness_records (
            id INTEGER PRIMARY KEY, variant_id TEXT, asset TEXT, composite REAL,
            cycle_id INTEGER
        );
        CREATE TABLE strategy_variants (
            variant_id TEXT PRIMARY KEY, strategy_key TEXT, asset TEXT
        );
        INSERT INTO lab_cycles VALUES ({cycle_id}, 'completed');
    """)
    conn.commit()
    return conn


def _add_queue_entries(conn, cycle_id, statuses):
    for i, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO lab_queue VALUES (?,?,?,?,?,?)",
            (i + 1, cycle_id, "donchian", "BTC", status, None),
        )
    conn.commit()


def _add_fitness(conn, cycle_id, score=0.72):
    conn.execute(
        "INSERT INTO strategy_variants VALUES ('v1','donchian_breakout','BTC')"
    )
    conn.execute(
        "INSERT INTO fitness_records VALUES (1,'v1','BTC',?,?)", (score, cycle_id)
    )
    conn.commit()


class TestSendCycleReport:
    def test_report_sent_once(self):
        """Genau 1 Telegram-Nachricht pro Cycle."""
        conn = _make_lab_conn()
        _add_queue_entries(conn, 1, ["completed", "completed", "blocked"])
        with patch("scripts.lab_controller.get_lab_state_connection", return_value=conn):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 1)
        mock_tg.assert_called_once()

    def test_report_contains_cycle_id(self):
        conn = _make_lab_conn(cycle_id=7)
        _add_queue_entries(conn, 7, ["completed"])
        with patch("scripts.lab_controller.get_lab_state_connection", return_value=conn):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 7)
        msg = mock_tg.call_args[0][0]
        assert "#7" in msg

    def test_report_contains_queue_stats(self):
        """Bericht enthält Queue-Zahlen: done/total, inconclusive, blockiert."""
        conn = _make_lab_conn()
        _add_queue_entries(conn, 1, ["completed", "completed", "paused_inconclusive", "blocked"])
        with patch("scripts.lab_controller.get_lab_state_connection", return_value=conn):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 1)
        msg = mock_tg.call_args[0][0]
        assert "2/4" in msg  # 2 done / 4 total
        assert "1 inconclusive" in msg
        assert "1 blockiert" in msg

    def test_report_with_top_variant(self):
        """Top-Variant im Bericht mit Score."""
        conn = _make_lab_conn()
        _add_queue_entries(conn, 1, ["completed"])
        _add_fitness(conn, 1, score=0.72)
        with patch("scripts.lab_controller.get_lab_state_connection", return_value=conn):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 1)
        msg = mock_tg.call_args[0][0]
        assert "donchian_breakout" in msg
        assert "0.720" in msg

    def test_report_no_top_variant_shows_dash(self):
        """Keine Fitness-Einträge → "–" im Bericht."""
        conn = _make_lab_conn()
        _add_queue_entries(conn, 1, ["completed"])
        with patch("scripts.lab_controller.get_lab_state_connection", return_value=conn):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 1)
        msg = mock_tg.call_args[0][0]
        assert "–" in msg

    def test_db_error_sends_fallback(self):
        """DB-Fehler → Fallback-Nachricht, kein Crash."""
        with patch("scripts.lab_controller.get_lab_state_connection",
                   side_effect=Exception("DB kaputt")):
            with patch("scripts.lab_controller._send_telegram") as mock_tg:
                _send_cycle_report("dummy.db", 1)
        mock_tg.assert_called_once()
        msg = mock_tg.call_args[0][0]
        assert "#1" in msg
