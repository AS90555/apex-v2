"""Tests für Latenz-Slippage-Regression (v7 Phase 4)."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import pytest
from scripts.run_latency_slippage_regression import (
    _linear_regression,
    _confidence_interval_95,
    _recommended_tolerance,
    run_regression,
    MIN_SAMPLES,
)


def _make_rows(latencies, slippages):
    """Erstellt sqlite3.Row-ähnliche Dicts."""
    return [{"signal_to_fill_ms": l, "slippage_bps": s}
            for l, s in zip(latencies, slippages)]


# ── _linear_regression ────────────────────────────────────────────────────────

def test_perfect_linear_fit():
    """y = 0.5x + 2 → slope=0.5, intercept=2, R²=1."""
    x = list(range(1, 101))
    y = [0.5 * xi + 2 for xi in x]
    slope, intercept, r2, _ = _linear_regression(x, y)
    assert abs(slope - 0.5) < 1e-6
    assert abs(intercept - 2.0) < 1e-6
    assert abs(r2 - 1.0) < 1e-6


def test_regression_slope_approx_half():
    """Synthese: slippage = 0.5 * latency + noise → Slope ≈ 0.5 ± 0.05."""
    import random
    rng = random.Random(42)
    n = 100
    x = [float(rng.randint(100, 5000)) for _ in range(n)]
    y = [0.5 * xi + rng.gauss(0, 10) for xi in x]
    slope, _, _, _ = _linear_regression(x, y)
    assert abs(slope - 0.5) < 0.1


def test_constant_x_returns_zero_slope():
    """Alle x gleich → slope=0."""
    x = [100.0] * 20
    y = [float(i) for i in range(20)]
    slope, _, _, _ = _linear_regression(x, y)
    assert slope == 0.0


def test_empty_returns_zeros():
    assert _linear_regression([], []) == (0.0, 0.0, 0.0, 0.0)


def test_r_squared_noisy_near_zero():
    """Unkorrellierte Daten → R² nahe 0."""
    import random
    rng = random.Random(7)
    x = [rng.uniform(0, 1000) for _ in range(100)]
    y = [rng.uniform(0, 100) for _ in range(100)]
    _, _, r2, _ = _linear_regression(x, y)
    assert r2 < 0.2


# ── _confidence_interval_95 ───────────────────────────────────────────────────

def test_ci_contains_slope():
    import random
    rng = random.Random(42)
    x = [float(rng.randint(100, 5000)) for _ in range(100)]
    y = [0.5 * xi + rng.gauss(0, 10) for xi in x]
    slope, _, _, stderr = _linear_regression(x, y)
    lo, hi = _confidence_interval_95(slope, stderr, 100)
    assert lo <= slope <= hi
    assert abs(slope - 0.5) < (hi - lo)  # 0.5 liegt im Intervall


def test_ci_wider_for_small_n():
    """Kleineres n → breiteres CI (t=2.5 statt 2.0)."""
    lo_big, hi_big = _confidence_interval_95(0.5, 0.05, 100)
    lo_sm, hi_sm = _confidence_interval_95(0.5, 0.05, 10)
    assert (hi_sm - lo_sm) > (hi_big - lo_big)


# ── _recommended_tolerance ────────────────────────────────────────────────────

def test_tolerance_minimum_5_bps():
    assert _recommended_tolerance(0.0, 0.0, 100.0) >= 5.0


def test_tolerance_maximum_50_bps():
    assert _recommended_tolerance(1.0, 100.0, 1000.0) <= 50.0


def test_tolerance_includes_10_bps_buffer():
    """slope=0, intercept=0 → expected=0, tolerance=10 (Puffer)."""
    tol = _recommended_tolerance(0.0, 0.0, 500.0)
    assert tol == 10.0


# ── run_regression mit Mock-Conn ──────────────────────────────────────────────

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        class _Cur:
            def __init__(self, rows):
                self._rows = rows
            def fetchall(self):
                return self._rows
        return _Cur(self._rows)


def test_run_regression_insufficient_data():
    conn = _FakeConn([{"signal_to_fill_ms": 100.0, "slippage_bps": 5.0}])
    result = run_regression("BTCUSDT", conn)
    assert result is None


def test_run_regression_returns_dict():
    import random
    rng = random.Random(42)
    rows = [
        {"signal_to_fill_ms": float(rng.randint(100, 5000)),
         "slippage_bps": float(rng.gauss(10, 3))}
        for _ in range(MIN_SAMPLES + 10)
    ]
    conn = _FakeConn(rows)
    result = run_regression("BTCUSDT", conn)
    assert result is not None
    assert result["asset"] == "BTCUSDT"
    assert result["n_samples"] == MIN_SAMPLES + 10
    assert "slope_bps_per_ms" in result
    assert "r_squared" in result
    assert "recommended_tolerance_bps" in result
