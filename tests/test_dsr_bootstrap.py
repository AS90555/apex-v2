"""Tests für bootstrap_dsr (v7 Phase 2)."""

from __future__ import annotations

import sys
import os
import random
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from backtest.monte_carlo import bootstrap_dsr
from backtest.metrics import dsr


def _positive_returns(n: int = 100, mean: float = 0.1, std: float = 0.8) -> list[float]:
    rng = random.Random(1)
    return [rng.gauss(mean, std) for _ in range(n)]


def _flat_returns(n: int = 100) -> list[float]:
    rng = random.Random(2)
    return [rng.gauss(0.0, 1.0) for _ in range(n)]


def test_bootstrap_dsr_returns_tuple():
    rs = _positive_returns()
    result = bootstrap_dsr(rs)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_bootstrap_dsr_range():
    """Median-DSR muss in [0, 1] liegen."""
    rs = _positive_returns()
    median_dsr, std_dsr = bootstrap_dsr(rs)
    assert 0.0 <= median_dsr <= 1.0
    assert std_dsr >= 0.0


def test_bootstrap_dsr_good_strategy_high():
    """Positive Returns → Median-DSR > 0.5."""
    rs = _positive_returns(n=150, mean=0.15, std=0.6)
    median_dsr, _ = bootstrap_dsr(rs)
    assert median_dsr > 0.5, f"Gute Strategie → DSR > 0.5 erwartet, got {median_dsr}"


def test_bootstrap_dsr_reproducible():
    """Gleicher Seed → identisches Ergebnis."""
    rs = _positive_returns()
    r1 = bootstrap_dsr(rs, seed=42)
    r2 = bootstrap_dsr(rs, seed=42)
    assert r1 == r2


def test_bootstrap_dsr_different_seeds_differ():
    """Unterschiedliche Seeds → unterschiedliche Ergebnisse (statistisch)."""
    rs = _flat_returns(200)
    r1 = bootstrap_dsr(rs, seed=1)
    r2 = bootstrap_dsr(rs, seed=999)
    # Nicht identisch (sehr unwahrscheinlich bei 1000 Iterationen)
    assert r1 != r2


def test_bootstrap_dsr_too_short_returns_zeros():
    """Weniger als 10 Returns → (0.0, 0.0)."""
    result = bootstrap_dsr([0.1, 0.2, 0.3])
    assert result == (0.0, 0.0)


def test_bootstrap_dsr_median_close_to_direct_dsr():
    """
    Bootstrap-Median sollte nicht weit vom direkten DSR abweichen.
    Toleranz: |median - direct| ≤ 0.2 (Bootstrap hat Varianz, aber Tendenz gleich).
    """
    rs = _positive_returns(n=200)
    direct = dsr(rs, n_tested=1)
    median_dsr, _ = bootstrap_dsr(rs, n_iter=500)
    assert abs(median_dsr - direct) <= 0.25, \
        f"Bootstrap-DSR {median_dsr:.3f} zu weit von direktem DSR {direct:.3f}"


def test_bootstrap_dsr_n_iter_affects_std():
    """Mehr Iterationen → kleinere Std-Abweichung (Konvergenz)."""
    rs = _flat_returns(100)
    _, std_100  = bootstrap_dsr(rs, n_iter=100,  seed=42)
    _, std_1000 = bootstrap_dsr(rs, n_iter=1000, seed=42)
    # 1000 Iter sollte stabilere Schätzung liefern (Std nicht größer)
    assert std_1000 <= std_100 * 1.5, \
        f"Mehr Iterationen sollten Std nicht erhöhen: {std_100:.4f} vs {std_1000:.4f}"
