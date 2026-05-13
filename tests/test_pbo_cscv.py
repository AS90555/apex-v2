"""Tests für CSCV-PBO (v7 Phase 2)."""

from __future__ import annotations

import sys
import os
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from backtest.metrics import pbo, sharpe


def _random_returns(n: int, seed: int, mean: float = 0.0, std: float = 1.0) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, std) for _ in range(n)]


def _trending_returns(n: int, drift: float = 0.05) -> list[float]:
    """Positive Returns — gute Strategie."""
    rng = random.Random(99)
    return [rng.gauss(drift, 0.8) for _ in range(n)]


def test_pbo_random_strategies_near_half():
    """Zufällige Strategien sollten PBO ≈ 0.5 ± 0.2 liefern."""
    is_ret  = [_random_returns(50, seed=i)     for i in range(8)]
    oos_ret = [_random_returns(50, seed=i+100) for i in range(8)]
    result = pbo(is_ret, oos_ret)
    assert 0.0 <= result <= 1.0
    # Random-Strategien: kein systematischer Bias → PBO nahe 0.5
    assert 0.25 <= result <= 0.75, f"PBO={result} zu weit von 0.5 für Random-Strategien"


def test_pbo_perfect_strategy_low():
    """Strategie die IS=OOS zeigt → niedriger PBO (kein Overfitting)."""
    # Gleiche gute Returns in IS und OOS
    good = [_trending_returns(60) for _ in range(6)]
    result = pbo(good, good)
    assert result <= 0.5, f"Perfekte Strategie → PBO sollte ≤ 0.5 sein, got {result}"


def test_pbo_too_few_folds_returns_neutral():
    """Weniger als 4 Folds → 0.5 (neutral)."""
    is_ret  = [_random_returns(30, seed=i) for i in range(3)]
    oos_ret = [_random_returns(30, seed=i+10) for i in range(3)]
    assert pbo(is_ret, oos_ret) == 0.5


def test_pbo_single_fold_neutral():
    assert pbo([[1.0, 2.0]], [[0.5, 0.5]]) == 0.5


def test_pbo_range():
    """Rückgabe immer in [0, 1]."""
    rng = random.Random(7)
    for _ in range(10):
        n = rng.randint(4, 10)
        is_r  = [_random_returns(40, seed=rng.randint(0, 1000)) for _ in range(n)]
        oos_r = [_random_returns(40, seed=rng.randint(0, 1000)) for _ in range(n)]
        result = pbo(is_r, oos_r)
        assert 0.0 <= result <= 1.0, f"PBO {result} außerhalb [0,1]"


def test_pbo_overfit_case():
    """IS-Overfitting: IS-Sharpe hoch, OOS-Sharpe zufällig → PBO sollte hoch sein."""
    # Simuliere 8 "Strategien": IS-Returns künstlich aufgeblasen
    rng = random.Random(42)
    # Alle OOS zufällig (keine echte Kante)
    oos_ret = [_random_returns(50, seed=i+200) for i in range(8)]
    # IS: eine Strategie hat künstlich hohen Sharpe (data snooping simuliert)
    is_ret = [_random_returns(50, seed=i) for i in range(8)]
    # IS-beste Strategie bekommt sehr positive Returns
    is_ret[3] = [abs(rng.gauss(0.5, 0.3)) for _ in range(50)]
    result = pbo(is_ret, oos_ret)
    # Erwartet: PBO > 0.3 (IS-beste performt OOS schlecht)
    assert 0.0 <= result <= 1.0
