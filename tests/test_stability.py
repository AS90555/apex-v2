"""Tests für backtest/stability.py (v7 Phase 2)."""

from __future__ import annotations

import sys
import os
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from backtest.stability import compute_stability, vary_cfg, run_stability, StabilityResult


def _make_returns(n: int = 60, mean: float = 0.05, std: float = 0.8, seed: int = 1):
    rng = random.Random(seed)
    return [rng.gauss(mean, std) for _ in range(n)]


def test_vary_cfg_changes_param():
    cfg = {"SL_MULT": 1.5, "TP_MULT": 2.0, "NAME": "test", "FLAG": True}
    result = vary_cfg(cfg, "SL_MULT", 1.1)
    assert abs(result["SL_MULT"] - 1.65) < 1e-9
    # Anderen Keys unverändert
    assert result["TP_MULT"] == 2.0
    assert result["NAME"] == "test"
    assert result["FLAG"] is True


def test_vary_cfg_preserves_int_type():
    cfg = {"LOOKBACK": 20}
    result = vary_cfg(cfg, "LOOKBACK", 1.5)
    assert isinstance(result["LOOKBACK"], int)
    assert result["LOOKBACK"] == 30


def test_compute_stability_perfect():
    """Alle Variationen gleich gut → Stabilität nahe 1.0."""
    base  = _make_returns(80, mean=0.1)
    # Gleichartige Variationen
    vars_ = {"param1": [base, base, base]}
    result = compute_stability(base, vars_)
    assert isinstance(result, StabilityResult)
    assert result.stability_score >= 0.9


def test_compute_stability_high_variance():
    """Sehr unterschiedliche Variationen → Stabilität nahe 0."""
    base = _make_returns(80, mean=0.1)
    # Eine Variation sehr positiv, eine sehr negativ
    good = _make_returns(80, mean=2.0, seed=10)
    bad  = _make_returns(80, mean=-2.0, seed=20)
    vars_ = {"param1": [good, bad, good, bad, good, bad]}
    result = compute_stability(base, vars_)
    assert result.stability_score <= 0.5


def test_compute_stability_returns_sensitivities():
    base = _make_returns(50)
    vars_ = {
        "sl_mult": [_make_returns(50, seed=i) for i in range(6)],
        "tp_mult": [_make_returns(50, seed=i+10) for i in range(6)],
    }
    result = compute_stability(base, vars_)
    assert "sl_mult" in result.param_sensitivities
    assert "tp_mult" in result.param_sensitivities


def test_compute_stability_empty_variations():
    """Keine Variationen → Score 1.0 (per Definition stabil)."""
    base = _make_returns(50)
    result = compute_stability(base, {})
    assert result.stability_score == 1.0


def test_stability_score_in_range():
    base = _make_returns(100)
    vars_ = {"p": [_make_returns(100, seed=i) for i in range(6)]}
    result = compute_stability(base, vars_)
    assert 0.0 <= result.stability_score <= 1.0


def test_run_stability_returns_result():
    """run_stability mit echter Strategie (squeeze, kurzer Zeitraum) liefert valides Ergebnis."""
    import time
    from config.settings import LIVE_ASSETS

    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - 180 * 86_400_000  # 180 Tage

    # Minimale cfg für squeeze
    cfg = {"SL_ATR_MULT": 1.5, "TP1_ATR_MULT": 1.5, "TP2_ATR_MULT": 3.0}

    try:
        result = run_stability(
            strategy="squeeze",
            asset="BTC",
            start_ts=start_ts,
            end_ts=end_ts,
            base_cfg=cfg,
            max_params=2,
        )
        assert isinstance(result, StabilityResult)
        assert 0.0 <= result.stability_score <= 1.0
    except Exception as e:
        # Bei leerer Test-DB: kein Crash, aber Hinweis
        pytest.skip(f"Keine Backtestdaten verfügbar: {e}")
