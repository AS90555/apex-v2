"""
Tests für Promotion-Gates mit framework_version='v7.2' (Phase 6).
"""
from __future__ import annotations

import sqlite3
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as _db_mod
from core.db import run_migrations


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_promo_v72.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    return db_file


def _make_disc(fw_version="v7.2", **overrides) -> dict:
    defaults = {
        "id": 1,
        "strategy": "donchian_breakout",
        "asset": "BTC",
        "framework_version": fw_version,
        "dsr_value": 0.8,
        "pbo_value": 0.1,
        "stability_score": 0.7,
        "max_drawdown": 2.0,
        "composite_score": 0.65,
        "fitness_score": 3.5,
        "study_hash": "a" * 32,
        "objective_version": "v72.0",
        # Pflichtfelder für _check_gates
        "cost_model_applied": 1,
        "pf_test_netto": 1.5,
        "n_test": 50,
        "dsr": 0.8,
        "oos_folds_n": 5,
        "backtest_funding_model": "dynamic",
        "intrabar_model": "dynamic",
        "deployment_status": "lab",
        "status": None,
        "params_json": "{}",
    }
    defaults.update(overrides)
    return defaults


def test_v72_accepted_by_promotion_gates(isolated_db):
    """framework_version='v7.2' wird von V7_REEVAL_REQUIRED-Check akzeptiert."""
    from core.db import get_connection
    conn = get_connection()
    disc = _make_disc(fw_version="v7.2")
    from scripts.run_auto_promotion import _check_gates
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "config.settings.V7_REEVAL_REQUIRED", True
    ):
        passed, failed_gates = _check_gates(disc, conn)
    conn.close()
    v7_fail = [f for f in failed_gates if "v7-Re-Eval" in f]
    assert not v7_fail, f"v7.2 sollte nicht als Re-Eval-ausstehend markiert werden, aber: {v7_fail}"


def test_v8_rejected_by_promotion_gates(isolated_db):
    """framework_version='v8' (unbekannte Future-Version) wird abgelehnt."""
    from core.db import get_connection
    conn = get_connection()
    disc = _make_disc(fw_version="v8")
    from scripts.run_auto_promotion import _check_gates
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "config.settings.V7_REEVAL_REQUIRED", True
    ):
        passed, failed_gates = _check_gates(disc, conn)
    conn.close()
    v7_fail = [f for f in failed_gates if "v7-Re-Eval" in f]
    assert v7_fail, "v8 sollte als Re-Eval-ausstehend markiert werden"
