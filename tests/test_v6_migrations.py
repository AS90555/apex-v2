"""
Phase-2-Test: V6 Schema-Migrationen — alle Spalten und Tabellen vorhanden.
"""

from __future__ import annotations

import pytest
from core.db import get_connection, get_staging_connection, run_migrations


V6_LAB_DISC_COLS = {
    "framework_version", "dsr_value", "pbo_value", "max_drawdown",
    "calmar_ratio", "stability_score", "composite_score", "oos_folds_n",
    "re_evaluated_at", "backtest_slippage_assumption",
    "backtest_funding_model", "intrabar_model",
}

V6_TRADE_COLS = {
    "signal_price", "fill_price", "slippage_bps", "slippage_measured_at",
    "funding_cost_actual", "market_impact_check", "spread_at_execution_bps",
    "order_type_used", "ioc_fill_ratio", "ioc_tolerance_used_bps",
    "liquidity_score_at_execution",
}

NEW_TABLES = {"funding_rates", "asset_liquidity_metrics", "execution_audit_log"}


def test_lab_discoveries_v6_cols():
    conn = get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_discoveries)").fetchall()}
    conn.close()
    missing = V6_LAB_DISC_COLS - cols
    assert not missing, f"Fehlende lab_discoveries-Spalten: {missing}"


def test_trades_v6_cols():
    conn = get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    conn.close()
    missing = V6_TRADE_COLS - cols
    assert not missing, f"Fehlende trades-Spalten: {missing}"


def test_new_tables_exist():
    conn = get_connection()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    missing = NEW_TABLES - tables
    assert not missing, f"Fehlende Tabellen: {missing}"


def test_idempotency_index_exists():
    conn = get_connection()
    indexes = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert "idx_lab_disc_idempotent" in indexes


def test_migrations_idempotent():
    """run_migrations() darf kein Error beim zweiten Aufruf werfen."""
    run_migrations()
    run_migrations()


def test_staging_connection():
    """get_staging_connection() erstellt Staging-DB mit korrektem Schema."""
    staging = get_staging_connection()
    tables = {r[0] for r in staging.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    staging.close()
    assert "lab_discoveries" in tables
    assert "lab_window_results" in tables


def test_staging_sync_pass(tmp_path, monkeypatch):
    """Discovery mit allen Pass-Bedingungen wird in Haupt-DB übertragen."""
    import os
    # Staging-DB in tmp
    staging_path = str(tmp_path / "staging_test.db")
    monkeypatch.setattr("core.db.STAGING_DB_PATH", staging_path)

    from core.db import get_staging_connection as gsc
    staging = gsc()
    staging.execute("""
        INSERT INTO lab_discoveries
        (discovered_at, params_hash, strategy, asset, params_json,
         n_test, pf_test_netto, cost_model_applied, dsr, framework_version,
         backtest_funding_model, intrabar_model, sync_status)
        VALUES ('2026-01-01','hash_pass','squeeze','BTC','{}',
                50, 1.40, 1, 0.55, 'v1', 'static', 'static', 'pending')
    """)
    staging.commit()
    staging.close()

    # Sync ausführen
    import scripts.run_staging_sync as rss
    monkeypatch.setattr(rss, "get_staging_connection", gsc)
    synced, rejected = rss.sync_once()

    assert synced == 1
    assert rejected == 0


def test_staging_sync_reject_low_pf(tmp_path, monkeypatch):
    """Discovery mit zu niedrigem pf_test_netto wird abgelehnt."""
    staging_path = str(tmp_path / "staging_reject.db")
    monkeypatch.setattr("core.db.STAGING_DB_PATH", staging_path)

    from core.db import get_staging_connection as gsc
    staging = gsc()
    staging.execute("""
        INSERT INTO lab_discoveries
        (discovered_at, params_hash, strategy, asset, params_json,
         n_test, pf_test_netto, cost_model_applied, dsr, sync_status)
        VALUES ('2026-01-01','hash_fail','squeeze','ETH','{}',
                50, 0.80, 1, 0.55, 'pending')
    """)
    staging.commit()
    staging.close()

    import scripts.run_staging_sync as rss
    monkeypatch.setattr(rss, "get_staging_connection", gsc)
    synced, rejected = rss.sync_once()

    assert synced == 0
    assert rejected == 1


def test_staging_sync_dedup(tmp_path, monkeypatch):
    """Doppelter Sync gleicher Discovery schreibt nur 1 Zeile in Haupt-DB."""
    staging_path = str(tmp_path / "staging_dedup.db")
    monkeypatch.setattr("core.db.STAGING_DB_PATH", staging_path)

    from core.db import get_staging_connection as gsc
    # Erst Discovery schreiben + einmal syncen
    staging = gsc()
    staging.execute("""
        INSERT INTO lab_discoveries
        (discovered_at, params_hash, strategy, asset, params_json,
         n_test, pf_test_netto, cost_model_applied, dsr, sync_status)
        VALUES ('2026-01-01','hash_dedup','squeeze','SOL','{}',
                60, 1.35, 1, 0.60, 'pending')
    """)
    staging.commit()
    staging.close()

    import scripts.run_staging_sync as rss
    monkeypatch.setattr(rss, "get_staging_connection", gsc)

    rss.sync_once()

    # Zweiten Sync-Versuch: Status='synced' → kein doppelter INSERT
    synced2, _ = rss.sync_once()
    assert synced2 == 0

    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE params_hash='hash_dedup'"
    ).fetchone()[0]
    conn.close()
    assert count == 1
