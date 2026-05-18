"""
Tests für backtest/v7_eval.py (Phase 2 v7.1).

Prüft: evaluate_v7() liefert V7EvalResult mit allen Pflichtfeldern,
deterministisch identische Ergebnisse bei gleichen Inputs, Gate-Reasons.
"""
from __future__ import annotations

import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as _db_mod
from core.db import run_migrations
from backtest.v7_eval import evaluate_v7, V7EvalResult


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_v7eval.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    return db_file


def test_evaluate_v7_returns_result(isolated_db):
    """evaluate_v7() gibt immer ein V7EvalResult zurück — kein Crash bei leerer DB."""
    result = evaluate_v7("squeeze", "BTC", {}, int(time.time() * 1000) - 10_000, int(time.time() * 1000))
    assert isinstance(result, V7EvalResult)
    assert result.strategy == "squeeze"
    assert result.asset == "BTC"


def test_evaluate_v7_all_fields_populated(isolated_db):
    """Alle Pflichtfelder sind nach evaluate_v7() gesetzt."""
    now_ms = int(time.time() * 1000)
    r = evaluate_v7("ema_pullback", "ETH", {}, now_ms - 10_000, now_ms)
    assert r.dsr_oos >= 0.0
    assert 0.0 <= r.pbo_val <= 1.0
    assert r.stability >= 0.0
    assert r.max_dd >= 0.0
    assert 0.0 <= r.composite <= 1.0
    assert isinstance(r.weights_hash, str)
    assert isinstance(r.fail_reasons, list)
    assert isinstance(r.passed, bool)
    json.loads(r.params_json)


def test_evaluate_v7_deterministic(isolated_db):
    """Gleiche Inputs → identische Ergebnisse (Determinismus)."""
    now_ms = int(time.time() * 1000)
    params = {"fast": 10, "slow": 30}
    r1 = evaluate_v7("donchian_breakout", "SOL", params, now_ms - 5_000, now_ms)
    r2 = evaluate_v7("donchian_breakout", "SOL", params, now_ms - 5_000, now_ms)
    assert r1.dsr_oos == r2.dsr_oos
    assert r1.pbo_val == r2.pbo_val
    assert r1.composite == r2.composite
    assert r1.weights_hash == r2.weights_hash


def test_evaluate_v7_fail_reasons_on_empty_db(isolated_db):
    """Bei leerer DB (0 Trades) liefert Gate-Check Fail-Reasons."""
    now_ms = int(time.time() * 1000)
    r = evaluate_v7("squeeze", "BTC", {}, now_ms - 10_000, now_ms)
    assert not r.passed
    assert len(r.fail_reasons) > 0


def test_evaluate_v7_params_json_reflects_input(isolated_db):
    """params_json im Result entspricht den übergebenen Params."""
    now_ms = int(time.time() * 1000)
    params = {"length": 20, "mult": 1.5}
    r = evaluate_v7("bb_kc_squeeze", "BTC", params, now_ms - 5_000, now_ms)
    loaded = json.loads(r.params_json)
    assert loaded == params


# ── P3.5 — PBO-Fallback expliziter Reject-Grund ───────────────────────────────

class TestPboInsufficientFolds:
    def test_insufficient_folds_explicit_fail_reason(self, isolated_db):
        """
        P3.5: Weniger als OOS_FOLDS_MIN_V7 Folds → expliziter pbo_insufficient-Grund.
        Kein stilles Durchrutschen durch pbo=0.5 als Fallback.
        """
        now_ms = int(time.time() * 1000)
        # Leere DB → 0 Folds → pbo_insufficient
        r = evaluate_v7("donchian_breakout", "BTC", {}, now_ms - 10_000, now_ms)

        assert not r.passed, "Leere DB muss Gate-Reject liefern"
        has_pbo_insufficient = any("pbo_insufficient" in reason for reason in r.fail_reasons)
        has_oos_folds = any("oos_folds_n" in reason for reason in r.fail_reasons)
        assert has_pbo_insufficient or has_oos_folds, (
            f"Kein expliziter Fold-Mangel-Grund in fail_reasons: {r.fail_reasons}"
        )

    def test_insufficient_folds_not_pbo_value_gate(self, isolated_db):
        """
        P3.5: Bei unzureichenden Folds muss der Reject-Grund 'pbo_insufficient_folds'
        lauten — nicht nur 'PBO=0.500 > 0.30' (zufälliger Treffer durch Fallback-Wert).
        """
        now_ms = int(time.time() * 1000)
        r = evaluate_v7("donchian_breakout", "BTC", {}, now_ms - 10_000, now_ms)

        assert not r.passed
        # Kein "PBO=0.500" als einziger Grund (wäre zufälliger Treffer)
        pbo_value_reason = [reason for reason in r.fail_reasons if reason.startswith("PBO=")]
        pbo_insuff_reason = [reason for reason in r.fail_reasons if "pbo_insufficient" in reason]
        assert not (pbo_value_reason and not pbo_insuff_reason), (
            f"Reject über PBO-Wert statt über expliziten Fold-Mangel-Grund: {r.fail_reasons}"
        )

    def test_stability_score_formula_documented(self):
        """P3.5: stability_score-Docstring enthält Formel-Beschreibung."""
        from backtest.metrics import stability_score
        doc = stability_score.__doc__ or ""
        assert "std" in doc.lower() or "formel" in doc.lower(), (
            "stability_score-Docstring muss die Formel beschreiben"
        )
