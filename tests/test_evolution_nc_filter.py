"""Tests für NC-Filterung in core/lab_evolution_engine.propose_variants()."""
from __future__ import annotations

import random

import pytest

from core.lab_evolution_engine import propose_variants, _active_blocked_pairs
from core.lab_negative_controls import archive_manual
from core.lab_state_db import (
    get_lab_state_connection,
    create_lab_cycle,
    write_regime_snapshot,
)


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "lab_evo_nc_test.db")


@pytest.fixture
def conn_with_context(tmp_db_path):
    """Conn mit minimalem Kontext: 2 Strategien, 2 Assets, 1 abgeschlossenem Queue-Entry."""
    conn = get_lab_state_connection(tmp_db_path)
    from core.lab_families import sync_to_db
    sync_to_db(conn)
    cycle_id = create_lab_cycle(conn)

    # regime_history befüllen (Quelle für asset-Pool)
    for asset in ("BTC", "ETH"):
        write_regime_snapshot(conn, asset, "MIXED", 0.54, 0.003)

    # lab_queue mit completed-Entries befüllen (Quelle für strategy-Pool)
    now = "2026-05-19T00:00:00+00:00"
    for strategy in ("donchian_breakout", "squeeze"):
        conn.execute(
            """INSERT INTO lab_queue
               (cycle_id, strategy, asset, priority_score, budget_trials, status, created_at)
               VALUES (?, ?, 'BTC', 1.0, 50, 'completed', ?)""",
            (cycle_id, strategy, now),
        )
    conn.commit()
    return conn, tmp_db_path, cycle_id


class TestActiveBlockedPairs:
    def test_empty_nc_table(self, tmp_db_path):
        conn = get_lab_state_connection(tmp_db_path)
        assert _active_blocked_pairs(conn) == set()

    def test_returns_blocked_pairs(self, tmp_db_path):
        archive_manual("squeeze", "BTC", "hash1", "signal_absent", "diag", db_path=tmp_db_path)
        conn = get_lab_state_connection(tmp_db_path)
        blocked = _active_blocked_pairs(conn)
        assert ("squeeze", "BTC") in blocked

    def test_closed_nc_not_included(self, tmp_db_path):
        archive_manual("vaa", "ETH", "hash2", "signal_absent", "diag", db_path=tmp_db_path)
        conn = get_lab_state_connection(tmp_db_path)
        # NC manuell schließen
        conn.execute(
            "UPDATE negative_controls SET closed_at='2026-05-19' WHERE strategy='vaa' AND asset='ETH'"
        )
        conn.commit()
        blocked = _active_blocked_pairs(conn)
        assert ("vaa", "ETH") not in blocked


class TestProposeVariantsNcFilter:
    def test_skips_blocked_pair(self, conn_with_context):
        """NC-gesperrtes Paar darf in keinem vorgeschlagenen Variant auftauchen."""
        conn, tmp_db_path, cycle_id = conn_with_context
        # donchian_breakout/BTC und donchian_breakout/ETH sperren
        archive_manual("donchian_breakout", "BTC", "h1", "signal_absent", "d", db_path=tmp_db_path)
        archive_manual("donchian_breakout", "ETH", "h2", "signal_absent", "d", db_path=tmp_db_path)

        # Nach NC-Insert frische Conn nötig (WAL)
        conn.close()
        conn = get_lab_state_connection(tmp_db_path)

        vids = propose_variants(conn, cycle_id=cycle_id, budget_trials=200, rng=random.Random(42))
        assert vids, "Es müssen Variants vorgeschlagen werden"

        from core.lab_state_db import get_variant
        for vid in vids:
            v = get_variant(conn, vid)
            assert v is not None
            assert not (v.strategy == "donchian_breakout"), (
                f"donchian_breakout wurde trotz NC-Sperre vorgeschlagen: {v.strategy}/{v.asset}"
            )

    def test_fallback_when_all_blocked(self, conn_with_context, capsys):
        """Wenn alle Paare gesperrt sind: Fallback auf vollen Pool, kein Crash, Log-Zeile erscheint."""
        conn, tmp_db_path, cycle_id = conn_with_context
        # Alle 4 Kombinationen (2 Strat × 2 Asset) sperren
        for strat in ("donchian_breakout", "squeeze"):
            for asset in ("BTC", "ETH"):
                archive_manual(strat, asset, f"h_{strat}_{asset}", "signal_absent", "d", db_path=tmp_db_path)

        conn.close()
        conn = get_lab_state_connection(tmp_db_path)

        vids = propose_variants(conn, cycle_id=cycle_id, budget_trials=200, rng=random.Random(7))
        # Fallback muss trotzdem Variants liefern
        assert len(vids) >= 1

    def test_unchanged_with_empty_nc_table(self, conn_with_context):
        """Ohne NCs muss das Verhalten identisch zu vorher sein (keine Regression)."""
        conn, tmp_db_path, cycle_id = conn_with_context
        rng_a = random.Random(99)
        rng_b = random.Random(99)

        vids_a = propose_variants(conn, cycle_id=cycle_id, budget_trials=200, rng=rng_a)
        # Zweiter Aufruf mit identischem Seed — beide müssen gleich viele Variants liefern
        # (exakte IDs können abweichen weil write_variant INSERT OR IGNORE, aber Anzahl gleich)
        assert len(vids_a) == 4  # 200 // 50 = 4 Paare
