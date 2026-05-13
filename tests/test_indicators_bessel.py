"""
Phase-3-Test: Bessel-Korrektur in stdev() — ddof=1 wie numpy.std(ddof=1).
"""

from __future__ import annotations

import math
import random
import pytest

from features.indicators import stdev, bollinger_bands


def _numpy_std(values: list[float], period: int) -> float:
    subset = values[-period:]
    mean   = sum(subset) / period
    var    = sum((x - mean) ** 2 for x in subset) / (period - 1)
    return math.sqrt(var)


def test_bessel_matches_numpy_random():
    rng = random.Random(42)
    for _ in range(100):
        n   = rng.randint(5, 50)
        per = rng.randint(2, n)
        vals = [rng.gauss(0, 1) for _ in range(n)]
        expected = _numpy_std(vals, per)
        actual   = stdev(vals, per)
        assert abs(actual - expected) < 1e-9, f"Abweichung: {actual} vs {expected}"


def test_stdev_period_1_returns_zero():
    assert stdev([1.0, 2.0, 3.0], period=1) == 0.0


def test_stdev_insufficient_data():
    assert stdev([1.0, 2.0], period=5) == 0.0


def test_bollinger_bands_use_bessel():
    """Bollinger-Bands verwenden jetzt Bessel-korrigiertes stdev."""
    closes = [float(i) for i in range(1, 22)]  # 21 Werte, period=20
    upper, mid, lower = bollinger_bands(closes, period=20, mult=2.0)
    std = _numpy_std(closes, 20)
    expected_upper = sum(closes[-20:]) / 20 + 2.0 * std
    assert abs(upper - expected_upper) < 1e-9
