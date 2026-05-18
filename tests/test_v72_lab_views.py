"""
T1.C — Tests für V72-Lab-Views in monitor/telegram_bot.py.

Prüft:
- build_v72_cycle_text(): leere DB → kein Crash, Cycle-ID enthalten, Queue-Breakdown
- build_v72_variants_text(): keine Variants → kein Crash, mit Variants → Fitness enthalten
- build_v72_regime_text(): keine History → kein Crash, mit Einträgen → Assets enthalten
- build_v72_nc_text(): keine NCs → kein Crash, mit NCs → Grund-Aufschlüsselung
- v72_cycle_run-Callback: Guard bei laufendem Cycle, Start bei freiem Cycle
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.telegram_bot import (
    build_v72_cycle_text,
    build_v72_variants_text,
    build_v72_regime_text,
    build_v72_nc_text,
    button_callback,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Shared-memory DB für alle Tests ──────────────────────────────────────────

_URI = "file:v72_views_test?mode=memory&cache=shared"


def _make_lab_conn(
    *,
    with_cycle: bool = False,
    cycle_status: str = "completed",
    with_queue: bool = False,
    with_variants: bool = False,
    with_fitness: bool = False,
    with_regime: bool = False,
    with_nc: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(_URI, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        DROP TABLE IF EXISTS fitness_records;
        DROP TABLE IF EXISTS strategy_variants;
        DROP TABLE IF EXISTS lab_queue;
        DROP TABLE IF EXISTS lab_cycles;
        DROP TABLE IF EXISTS regime_history;
        DROP TABLE IF EXISTS negative_controls;
        CREATE TABLE lab_cycles (
            id INTEGER PRIMARY KEY, status TEXT, cycle_start TEXT,
            cycle_end TEXT, total_pairs_queued INTEGER, total_trials_run INTEGER
        );
        CREATE TABLE lab_queue (
            id INTEGER PRIMARY KEY, cycle_id INTEGER, strategy TEXT,
            asset TEXT, status TEXT
        );
        CREATE TABLE strategy_variants (
            variant_id TEXT PRIMARY KEY, strategy TEXT, asset TEXT,
            generation INTEGER, status TEXT, fitness_score REAL, family_id TEXT
        );
        CREATE TABLE fitness_records (
            id INTEGER PRIMARY KEY, variant_id TEXT, asset TEXT,
            composite REAL, fitness REAL, cycle_id INTEGER
        );
        CREATE TABLE regime_history (
            id INTEGER PRIMARY KEY, asset TEXT, regime TEXT,
            computed_at TEXT, hurst_exponent REAL, change_detected INTEGER
        );
        CREATE TABLE negative_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, asset TEXT, no_go_reason TEXT,
            closed_at TEXT, created_at TEXT
        );
    """)
    if with_cycle:
        conn.execute(
            "INSERT INTO lab_cycles VALUES (7, ?, '2026-05-18T08:00:00', "
            "'2026-05-18T09:00:00', 5, 120)",
            (cycle_status,),
        )
    if with_queue and with_cycle:
        conn.executemany(
            "INSERT INTO lab_queue VALUES (?,7,?,?,?)",
            [
                (1, "donchian", "BTC", "completed"),
                (2, "squeeze",  "ETH", "paused_inconclusive"),
                (3, "donchian", "SOL", "running"),
            ],
        )
    if with_variants:
        conn.executemany(
            "INSERT INTO strategy_variants VALUES (?,?,?,?,?,?,?)",
            [
                ("v1", "donchian", "BTC", 1, "evaluated", 0.72, "fam1"),
                ("v2", "squeeze",  "ETH", 2, "evaluated", 0.65, "fam2"),
                ("v3", "donchian", "SOL", 1, "proposed",  None,  "fam1"),
            ],
        )
    if with_fitness and with_variants:
        conn.executemany(
            "INSERT INTO fitness_records VALUES (?,?,?,?,?,?)",
            [
                (1, "v1", "BTC", 0.68, 0.72, 7),
                (2, "v2", "ETH", 0.60, 0.65, 7),
            ],
        )
    if with_regime:
        conn.executemany(
            "INSERT INTO regime_history VALUES (?,?,?,?,?,?)",
            [
                (1, "BTC", "TREND",    "2026-05-18T07:00:00", 0.62, 0),
                (2, "ETH", "HIGH_VOL", "2026-05-18T07:30:00", 0.51, 1),
            ],
        )
    if with_nc:
        conn.executemany(
            "INSERT INTO negative_controls VALUES (?,?,?,?,?,?)",
            [
                (1, "donchian", "BTC", "signal_absent",          None, "2026-05-18T06:00:00"),
                (2, "squeeze",  "ETH", "frequency_incompatible", None, "2026-05-18T06:30:00"),
                (3, "donchian", "SOL", "operator_decision",      "2026-05-17T00:00:00", "2026-05-17T00:00:00"),
            ],
        )
    conn.commit()
    return conn


def _open_shared() -> sqlite3.Connection:
    c = sqlite3.connect(_URI, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# ── build_v72_cycle_text ──────────────────────────────────────────────────────

class TestBuildV72CycleText:
    def test_empty_db_no_crash(self):
        anchor = _make_lab_conn()  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_cycle_text()
        assert "kein Cycle" in result or "V72" in result

    def test_cycle_id_in_output(self):
        anchor = _make_lab_conn(with_cycle=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_cycle_text()
        assert "#7" in result or "7" in result

    def test_queue_breakdown_shown(self):
        anchor = _make_lab_conn(with_cycle=True, with_queue=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_cycle_text()
        assert "inconclusive" in result.lower() or "lab_decide" in result.lower()

    def test_running_cycle_shown(self):
        anchor = _make_lab_conn(with_cycle=True, cycle_status="running")  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_cycle_text()
        assert "running" in result


# ── build_v72_variants_text ───────────────────────────────────────────────────

class TestBuildV72VariantsText:
    def test_empty_db_no_crash(self):
        anchor = _make_lab_conn()  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_variants_text()
        assert "V72" in result or "keine" in result.lower() or "Variants" in result

    def test_variant_status_counts(self):
        anchor = _make_lab_conn(with_variants=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_variants_text()
        assert "evaluated" in result
        assert "proposed" in result

    def test_top_fitness_shown(self):
        anchor = _make_lab_conn(with_variants=True, with_fitness=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_variants_text()
        assert "donchian" in result
        # _p() escaped Punkte: "0.720" → "0\\.720" im MarkdownV2-String
        assert "0.720" in result or "0\\.720" in result

    def test_no_fitness_shows_fallback(self):
        anchor = _make_lab_conn(with_variants=True, with_fitness=False)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_variants_text()
        assert "keine" in result.lower() or "–" in result or "Fitness" in result


# ── build_v72_regime_text ─────────────────────────────────────────────────────

class TestBuildV72RegimeText:
    def test_empty_db_no_crash(self):
        anchor = _make_lab_conn()  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_regime_text()
        assert "V72" in result or "keine" in result.lower() or "Regime" in result

    def test_assets_in_output(self):
        anchor = _make_lab_conn(with_regime=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_regime_text()
        assert "BTC" in result
        assert "ETH" in result

    def test_regime_values_shown(self):
        anchor = _make_lab_conn(with_regime=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_regime_text()
        assert "TREND" in result
        assert "HIGH_VOL" in result or "HIGH\\_VOL" in result


# ── build_v72_nc_text ─────────────────────────────────────────────────────────

class TestBuildV72NcText:
    def test_empty_db_no_crash(self):
        anchor = _make_lab_conn()  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_nc_text()
        assert "V72" in result or "NCs" in result or "keine" in result.lower()

    def test_total_count_shown(self):
        anchor = _make_lab_conn(with_nc=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_nc_text()
        assert "3" in result  # 3 NCs gesamt

    def test_reason_breakdown_shown(self):
        anchor = _make_lab_conn(with_nc=True)  # noqa: F841
        with patch("monitor.telegram_bot._v72_conn", side_effect=_open_shared):
            result = build_v72_nc_text()
        # "kein Signal" oder "signal_absent" muss auftauchen
        assert "kein Signal" in result or "signal_absent" in result


# ── v72_cycle_run Callback ────────────────────────────────────────────────────

def _make_query_update(action: str) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = action
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_user.id = 12345
    update.message = None
    return update


class TestV72CycleRunCallback:
    def test_guard_blocks_if_running(self):
        anchor = _make_lab_conn(with_cycle=True, cycle_status="running")  # noqa: F841
        update = _make_query_update("v72_cycle_run")
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                _run(button_callback(update, MagicMock()))
        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "läuft bereits" in msg.lower() or "running" in msg.lower() or "läuft" in msg

    def test_starts_subprocess_if_free(self):
        anchor = _make_lab_conn()  # kein laufender Cycle  # noqa: F841
        update = _make_query_update("v72_cycle_run")
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("core.lab_state_db.get_lab_state_connection", side_effect=_open_shared):
                with patch("subprocess.Popen") as mock_popen:
                    _run(button_callback(update, MagicMock()))
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]  # Liste der Subprocess-Argumente
        assert any("lab_controller.py" in a for a in call_args)
        assert "run-cycle" in call_args
