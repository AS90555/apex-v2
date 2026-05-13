"""
Tests für v7.2-Sync-Integrität in run_staging_sync.py (Phase 6).
"""
from __future__ import annotations

import sqlite3
import pytest

from core.staging_schema import STAGING_DDL


def _make_row(**overrides) -> dict:
    """Minimale v7.2-konforme Staging-Zeile."""
    defaults = {
        "cost_model_applied": 1,
        "pf_test_netto": 1.5,
        "n_test": 50,
        "dsr_value": 0.8,
        "dsr": 0.8,
        "pbo_value": 0.1,
        "stability_score": 0.7,
        "backtest_funding_model": "dynamic",
        "intrabar_model": "dynamic",
        "framework_version": "v7.2",
        "study_hash": "a" * 32,
        "objective_version": "v72.0",
    }
    defaults.update(overrides)
    return defaults


# Import der zu testenden Funktion
from scripts.run_staging_sync import _check_integrity


def test_v72_passes_all_gates():
    row = _make_row()
    ok, reason = _check_integrity(row)
    assert ok, f"Sollte PASS sein, aber: {reason}"


def test_v72_missing_study_hash():
    row = _make_row(study_hash=None)
    ok, reason = _check_integrity(row)
    assert not ok
    assert "study_hash missing" in reason


def test_v72_empty_study_hash():
    row = _make_row(study_hash="")
    ok, reason = _check_integrity(row)
    assert not ok
    assert "study_hash missing" in reason


def test_v72_missing_objective_version():
    row = _make_row(objective_version=None)
    ok, reason = _check_integrity(row)
    assert not ok
    assert "objective_version missing" in reason


def test_v72_dsr_too_low():
    row = _make_row(dsr_value=0.3)
    ok, reason = _check_integrity(row)
    assert not ok
    assert "dsr_value" in reason


def test_v72_pbo_too_high():
    row = _make_row(pbo_value=0.5)
    ok, reason = _check_integrity(row)
    assert not ok
    assert "pbo_value" in reason


def test_v72_stability_too_low():
    row = _make_row(stability_score=0.2)
    ok, reason = _check_integrity(row)
    assert not ok
    assert "stability_score" in reason


def test_non_v72_row_skips_v72_checks():
    """v1-Zeilen werden nach den alten Regeln geprüft, nicht nach v7.2-Regeln."""
    row = _make_row(framework_version="v1", study_hash=None, objective_version=None)
    ok, reason = _check_integrity(row)
    # v1 schlägt nicht wegen fehlendem study_hash fehl
    assert "study_hash" not in reason
