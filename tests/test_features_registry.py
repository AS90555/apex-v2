"""Tests für features/registry.py — v7 Phase 1."""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from features.registry import compute_max_lookback, all_strategy_lookbacks


def test_ema200_strategies_have_high_lookback():
    """Strategien mit EMA-200 müssen mindestens 210+Puffer liefern."""
    for s in ("squeeze", "mean_reversion", "vaa", "kdt", "ema_pullback", "orb"):
        lb = compute_max_lookback(s)
        assert lb >= 250, f"{s}: erwartet ≥250, got {lb}"


def test_short_strategies_have_smaller_lookback():
    """Strategien ohne EMA-200 dürfen kleiner sein, aber mind. 50."""
    for s in ("donchian_breakout", "dual_donchian", "asian_fade"):
        lb = compute_max_lookback(s)
        assert 50 <= lb < 210, f"{s}: erwartet 50..209, got {lb}"


def test_unknown_strategy_returns_global_max():
    """Unbekannte Strategie → globaler Max-Lookback (mit Puffer)."""
    lb = compute_max_lookback("nonexistent_strategy_xyz")
    assert lb >= 252  # 210 * 1.20 = 252


def test_orb_lookback_covers_vol_sma():
    """ORB nutzt vol_sma_20_5m (min_candles=21) → Lookback muss das abdecken."""
    lb = compute_max_lookback("orb")
    assert lb >= 21


def test_minimum_floor():
    """Rückgabe ist immer mindestens 50."""
    lb = compute_max_lookback("asian_fade")
    assert lb >= 50


def test_all_strategy_lookbacks_returns_dict():
    result = all_strategy_lookbacks()
    assert isinstance(result, dict)
    assert len(result) >= 10
    for k, v in result.items():
        assert isinstance(k, str)
        assert isinstance(v, int)
        assert v >= 50


def test_safety_factor_applied():
    """compute_max_lookback gibt Wert mit mind. 20% Puffer gegenüber Basis."""
    import math
    from features.registry import _STRATEGY_LOOKBACK, _SAFETY_FACTOR
    for strategy, base in _STRATEGY_LOOKBACK.items():
        expected_min = max(50, math.ceil(base * _SAFETY_FACTOR))
        actual = compute_max_lookback(strategy)
        assert actual == expected_min, f"{strategy}: {actual} != {expected_min}"
