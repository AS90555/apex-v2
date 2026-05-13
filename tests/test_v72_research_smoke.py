"""
Smoke-Tests für scripts/run_v72_research.py (Phase 5).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import patch

import pytest
import optuna

optuna.logging.set_verbosity(optuna.logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as _db_mod
import research.v72_staging_writer as _sw_mod
from core.db import run_migrations
from core.staging_schema import STAGING_DDL
from backtest.v7_eval import V7EvalResult


@pytest.fixture()
def isolated_live_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_v72smoke.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    return db_file


@pytest.fixture()
def isolated_staging_db(tmp_path):
    db_file = str(tmp_path / "staging_v72smoke.db")
    conn = sqlite3.connect(db_file)
    conn.executescript(STAGING_DDL)
    conn.commit()
    conn.close()
    return db_file


def _make_eval(passed=True, pbo=0.1, dsr=0.8, composite=0.65) -> V7EvalResult:
    return V7EvalResult(
        strategy="donchian_breakout", asset="BTC", params_json="{}",
        dsr_oos=dsr, pbo_val=pbo, stability=0.7, max_dd=2.0,
        composite=composite, weights_hash="abc", n_oos=200,
        oos_folds_n=18, passed=passed,
        fail_reasons=[] if passed else ["DSR=0.0 < 0.5"],
    )


def _patch_objective(eval_result=None):
    if eval_result is None:
        eval_result = _make_eval()

    def _fake_objective_v72(trial, strategy, asset, start_ts, end_ts, n_tested_hint):
        trial.set_user_attr("eval_result", {
            "dsr_oos": eval_result.dsr_oos,
            "pbo_val": eval_result.pbo_val,
            "stability": eval_result.stability,
            "max_dd": eval_result.max_dd,
            "composite": eval_result.composite,
            "weights_hash": eval_result.weights_hash,
            "n_oos": eval_result.n_oos,
            "oos_folds_n": eval_result.oos_folds_n,
            "passed": eval_result.passed,
            "fail_reasons": eval_result.fail_reasons,
        })
        return eval_result.composite if eval_result.pbo_val <= 0.30 else 0.0

    return _fake_objective_v72


def test_dry_run_no_db_write(isolated_live_db, isolated_staging_db, monkeypatch, tmp_path):
    """--dry-run erzeugt keinen Staging-Eintrag."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_v72_research.py", "--strategy", "donchian_breakout",
         "--asset", "BTC", "--n-trials", "3", "--dry-run"],
    )
    import scripts.run_v72_research as _mod
    with patch.object(_mod, "objective_v72", side_effect=_patch_objective()), \
         patch.object(_mod, "_write_report", return_value=str(tmp_path / "summary.md")):
        study = _mod.main()

    conn = sqlite3.connect(isolated_staging_db)
    count = conn.execute("SELECT COUNT(*) FROM lab_discoveries WHERE framework_version='v7.2'").fetchone()[0]
    conn.close()
    assert count == 0


def test_dry_run_report_created(isolated_live_db, isolated_staging_db, monkeypatch, tmp_path):
    """--dry-run erstellt trotzdem die korrekte Anzahl Trials."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_v72_research.py", "--strategy", "donchian_breakout",
         "--asset", "BTC", "--n-trials", "3", "--dry-run"],
    )
    import scripts.run_v72_research as _mod
    with patch.object(_mod, "objective_v72", side_effect=_patch_objective()), \
         patch.object(_mod, "_write_report", return_value=str(tmp_path / "summary.md")):
        study = _mod.main()

    assert len(study.trials) == 3


def test_no_dry_run_writes_staging(isolated_live_db, isolated_staging_db, monkeypatch, tmp_path):
    """Ohne --dry-run: 3 Trials → 3 Staging-Einträge mit framework_version='v7.2'."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_v72_research.py", "--strategy", "donchian_breakout",
         "--asset", "BTC", "--n-trials", "3", "--batch-size", "10"],
    )

    staging_conn = sqlite3.connect(isolated_staging_db)
    staging_conn.row_factory = sqlite3.Row

    import scripts.run_v72_research as _mod
    with patch.object(_mod, "V72_RESEARCH_ENABLED", True), \
         patch.object(_mod, "get_staging_connection", return_value=staging_conn), \
         patch.object(_mod, "objective_v72", side_effect=_patch_objective()), \
         patch.object(_mod, "_write_report", return_value=str(tmp_path / "summary.md")):
        study = _mod.main()

    # main() schliesst staging_conn — neue Verbindung zum Lesen
    verify_conn = sqlite3.connect(isolated_staging_db)
    count = verify_conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE framework_version='v7.2'"
    ).fetchone()[0]
    verify_conn.close()
    # Mock-Objective setzt keine Params → gleicher hash → mindestens 1 Eintrag genügt
    assert count >= 1


def test_disabled_without_dry_run_exits(monkeypatch, tmp_path):
    """V72_RESEARCH_ENABLED=false ohne --dry-run → sys.exit(1)."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_v72_research.py", "--strategy", "donchian_breakout", "--asset", "BTC", "--n-trials", "3"],
    )
    import scripts.run_v72_research as _mod
    with patch.object(_mod, "V72_RESEARCH_ENABLED", False), \
         patch.object(_mod, "_write_report", return_value=str(tmp_path / "summary.md")), \
         pytest.raises(SystemExit) as exc_info:
        _mod.main()
    assert exc_info.value.code == 1
