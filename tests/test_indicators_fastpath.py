"""Phase-8-Tests: NumPy-Fast-Path vs Pure-Python — numerische Äquivalenz."""
from __future__ import annotations

import math
import os
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOLERANCE = 1e-10


def _rng_values(n: int, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(100.0, 10.0) for _ in range(n)]


def _rng_candles(n: int, seed: int = 99) -> list[dict]:
    rng = random.Random(seed)
    candles = []
    price = 100.0
    for i in range(n):
        o = price + rng.gauss(0, 0.5)
        h = o + abs(rng.gauss(0, 1.0))
        l = o - abs(rng.gauss(0, 1.0))
        c = o + rng.gauss(0, 0.3)
        candles.append({"time": i, "open": o, "high": h, "low": l, "close": c, "volume": 1000})
        price = c
    return candles


# ── stdev Fast-Path ───────────────────────────────────────────────────────────

def test_stdev_numpy_vs_pure_python():
    """NumPy- und Pure-Python-stdev stimmen auf 1e-10 überein."""
    import features.indicators as ind

    for seed in range(50):
        values = _rng_values(100, seed)
        period = 20

        # Pure-Python-Referenz
        subset   = values[-period:]
        mean_val = sum(subset) / period
        variance = sum((x - mean_val) ** 2 for x in subset) / (period - 1)
        ref = math.sqrt(variance)

        # Modul-Funktion (nutzt NumPy wenn vorhanden)
        result = ind.stdev(values, period)
        assert abs(result - ref) < TOLERANCE, \
            f"seed={seed}: stdev {result} != ref {ref} (diff={abs(result-ref):.2e})"


def test_stdev_zero_std_case():
    """Konstante Werte → stdev=0."""
    import features.indicators as ind
    values = [5.0] * 30
    assert ind.stdev(values, 20) == pytest.approx(0.0, abs=1e-12)


def test_stdev_short_series():
    """Zu kurze Series → 0.0 ohne Exception."""
    import features.indicators as ind
    assert ind.stdev([1.0, 2.0], period=10) == 0.0
    assert ind.stdev([1.0], period=1) == 0.0


# ── atr_wilder Fast-Path ──────────────────────────────────────────────────────

def _atr_pure(candles: list[dict], period: int) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def test_atr_numpy_vs_pure_python():
    """NumPy- und Pure-Python-ATR stimmen auf 1e-10 überein."""
    import features.indicators as ind

    for seed in range(50):
        candles = _rng_candles(200, seed)
        period  = 14
        ref     = _atr_pure(candles, period)
        result  = ind.atr_wilder(candles, period)
        assert abs(result - ref) < TOLERANCE, \
            f"seed={seed}: atr {result} != ref {ref} (diff={abs(result-ref):.2e})"


def test_atr_too_short():
    import features.indicators as ind
    candles = _rng_candles(5)
    assert ind.atr_wilder(candles, period=14) == 0.0


# ── bollinger_bands Konsistenz ────────────────────────────────────────────────

def test_bollinger_uses_stdev():
    """bollinger_bands nutzt stdev() — indirekt NumPy wenn verfügbar."""
    import features.indicators as ind

    for seed in range(50):
        values = _rng_values(200, seed)
        period = 20
        upper, mid, lower = ind.bollinger_bands(values, period, mult=2.0)

        expected_mid = sum(values[-period:]) / period
        expected_std = ind.stdev(values, period)
        assert abs(mid - expected_mid) < 1e-10
        assert abs(upper - (expected_mid + 2 * expected_std)) < 1e-10
        assert abs(lower - (expected_mid - 2 * expected_std)) < 1e-10


def test_bollinger_symmetric():
    """Upper/Lower gleichweit von Mid entfernt."""
    import features.indicators as ind
    values = _rng_values(100, seed=3)
    upper, mid, lower = ind.bollinger_bands(values, period=20, mult=2.0)
    assert abs((upper - mid) - (mid - lower)) < 1e-10


# ── Performance-Vergleich (kein Assert, nur Logging) ─────────────────────────

def test_numpy_faster_than_pure_python(capsys):
    """NumPy-Fast-Path soll bei großen Serien schneller sein."""
    try:
        import numpy as np
    except ImportError:
        pytest.skip("NumPy nicht verfügbar")

    import features.indicators as ind

    n = 5000
    values  = _rng_values(n, seed=0)
    candles = _rng_candles(n, seed=0)
    period  = 20

    # NumPy-Timing
    t0 = time.perf_counter()
    for _ in range(100):
        ind.stdev(values, period)
    t_numpy = time.perf_counter() - t0

    # Pure-Python-Timing
    t0 = time.perf_counter()
    for _ in range(100):
        subset   = values[-period:]
        mean_val = sum(subset) / period
        variance = sum((x - mean_val) ** 2 for x in subset) / (period - 1)
        math.sqrt(variance)
    t_pure = time.perf_counter() - t0

    with capsys.disabled():
        print(f"\n[Perf] stdev×100: NumPy={t_numpy*1000:.1f}ms  Pure={t_pure*1000:.1f}ms")

    # Kein hartes Assert — NumPy-Overhead bei kleinen Listen kann Pure-Python überholen;
    # Test bestätigt nur, dass beide Pfade fehlerfrei laufen.
    assert t_numpy < 10.0 and t_pure < 10.0  # Plausibilitäts-Check
