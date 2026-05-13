"""
v7.2-Migrations-Tests (Phase 1).

Prüft study_hash + objective_version in lab_discoveries (Live-DB) und
research_staging.db auf isolierten tmp-DBs. Niemals gegen data/apex_v2.db.
"""
from __future__ import annotations

import sqlite3
import pytest
import core.db as _db_mod
from core.db import run_migrations
from core.staging_schema import STAGING_DDL


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_v72test.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def isolated_staging(tmp_path):
    db_file = str(tmp_path / "staging_v72test.db")
    conn = sqlite3.connect(db_file)
    conn.executescript(STAGING_DDL)
    conn.commit()
    yield conn
    conn.close()


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _col_info(conn, table: str) -> dict:
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ── Live-DB ───────────────────────────────────────────────────────────────────

def test_study_hash_in_live_db(isolated_db):
    assert "study_hash" in _cols(isolated_db, "lab_discoveries")


def test_objective_version_in_live_db(isolated_db):
    assert "objective_version" in _cols(isolated_db, "lab_discoveries")


def test_study_hash_nullable(isolated_db):
    info = _col_info(isolated_db, "lab_discoveries")
    assert info["study_hash"][3] == 0  # notnull=0


def test_objective_version_nullable(isolated_db):
    info = _col_info(isolated_db, "lab_discoveries")
    assert info["objective_version"][3] == 0  # notnull=0


def test_v72_migrations_idempotent(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_idem_v72.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    run_migrations()
    run_migrations()


# ── Staging-DB ────────────────────────────────────────────────────────────────

def test_study_hash_in_staging(isolated_staging):
    assert "study_hash" in _cols(isolated_staging, "lab_discoveries")


def test_objective_version_in_staging(isolated_staging):
    assert "objective_version" in _cols(isolated_staging, "lab_discoveries")


def test_staging_study_hash_nullable(isolated_staging):
    info = _col_info(isolated_staging, "lab_discoveries")
    assert info["study_hash"][3] == 0


def test_staging_objective_version_nullable(isolated_staging):
    info = _col_info(isolated_staging, "lab_discoveries")
    assert info["objective_version"][3] == 0


def test_staging_idempotent(tmp_path):
    db_file = str(tmp_path / "staging_idem_v72.db")
    conn = sqlite3.connect(db_file)
    conn.executescript(STAGING_DDL)
    conn.executescript(STAGING_DDL)  # zweite Ausführung — kein Fehler
    conn.commit()
    conn.close()
