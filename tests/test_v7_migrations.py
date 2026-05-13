"""
v7-Migrations-Tests (Phase 5).

Prüft alle neuen Spalten/Tabellen aus Phase 3+4 auf einer isolierten
tmp-DB — nie gegen data/apex_v2.db.
"""
from __future__ import annotations

import sqlite3
import pytest
import core.db as _db_mod
from core.db import run_migrations


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_v7test.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


# ── Phase 3 ──────────────────────────────────────────────────────────────────

def test_lab_discoveries_lab_config_hash(isolated_db):
    assert "lab_config_hash" in _cols(isolated_db, "lab_discoveries")


def test_lab_discoveries_composite_weights_hash(isolated_db):
    assert "composite_weights_hash" in _cols(isolated_db, "lab_discoveries")


# ── Phase 4 ──────────────────────────────────────────────────────────────────

def test_trades_signal_to_fill_ms(isolated_db):
    assert "signal_to_fill_ms" in _cols(isolated_db, "trades")


def test_asset_execution_calibration_exists(isolated_db):
    assert "asset_execution_calibration" in _tables(isolated_db)


def test_asset_execution_calibration_cols(isolated_db):
    expected = {
        "asset", "slippage_slope_bps_per_ms", "r_squared",
        "recommended_tolerance_bps", "n_samples", "updated_at",
    }
    cols = _cols(isolated_db, "asset_execution_calibration")
    missing = expected - cols
    assert not missing, f"Fehlende Spalten in asset_execution_calibration: {missing}"


# ── Idempotenz ────────────────────────────────────────────────────────────────

def test_v7_migrations_idempotent(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_idem_v7.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    run_migrations()
    run_migrations()


# ── Nullable-Default ─────────────────────────────────────────────────────────

def test_lab_config_hash_nullable(isolated_db):
    """lab_config_hash-Spalte existiert und hat NULL-Default."""
    cols = {r[1] for r in isolated_db.execute("PRAGMA table_info(lab_discoveries)").fetchall()}
    assert "lab_config_hash" in cols
    # Spalte ist nullable: kein NOT NULL in Definition
    info = {r[1]: r for r in isolated_db.execute("PRAGMA table_info(lab_discoveries)").fetchall()}
    assert info["lab_config_hash"][3] == 0  # notnull=0


def test_signal_to_fill_ms_nullable(isolated_db):
    """signal_to_fill_ms-Spalte existiert und ist nullable."""
    cols = {r[1] for r in isolated_db.execute("PRAGMA table_info(trades)").fetchall()}
    assert "signal_to_fill_ms" in cols
    info = {r[1]: r for r in isolated_db.execute("PRAGMA table_info(trades)").fetchall()}
    assert info["signal_to_fill_ms"][3] == 0  # notnull=0
