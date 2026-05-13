"""Tests für Funding-bewusstes Sizing (v7 Phase 4)."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from governance.sizing import compute_position_size, _funding_adjustment
from config.settings import RISK_USDT


def _base_size(sl_dist: float) -> float:
    return RISK_USDT / sl_dist


def test_no_funding_size_unchanged():
    """funding=0 → Size unverändert (exakt RISK_USDT/sl_dist)."""
    size = compute_position_size(
        asset="BTCUSDT", entry_price=50000.0, sl_distance=500.0,
        capital=RISK_USDT * 20, expected_funding_8h=0.0,
    )
    expected = _base_size(500.0)
    assert abs(size - expected) < 1e-6


def test_small_funding_reduces_size():
    """funding=0.001 (8h), 8h Halten → size leicht reduziert."""
    size_no_fund = compute_position_size(
        asset="ETHUSDT", entry_price=3000.0, sl_distance=30.0,
        capital=RISK_USDT * 20, expected_funding_8h=0.0,
    )
    size_with_fund = compute_position_size(
        asset="ETHUSDT", entry_price=3000.0, sl_distance=30.0,
        capital=RISK_USDT * 20, expected_funding_8h=0.001, expected_holding_h=8.0,
    )
    assert size_with_fund < size_no_fund
    assert size_with_fund > 0


def test_funding_adjustment_formula():
    """Manuell: funding=0.001, holding=8h, risk_pct=500/50000=0.01 → adj≈0.9."""
    adj = _funding_adjustment(
        expected_funding_8h=0.001,
        expected_holding_h=8.0,
        risk_per_trade_pct=0.01,
    )
    # funding_drag = 0.001 * (8/8) = 0.001
    # adj = 1 - 1.0 * 0.001 / 0.01 = 1 - 0.1 = 0.9
    assert abs(adj - 0.9) < 0.01


def test_extreme_funding_floors_at_zero():
    """Sehr hohe Funding-Rate floort adj auf 0."""
    adj = _funding_adjustment(
        expected_funding_8h=0.005,
        expected_holding_h=24.0,
        risk_per_trade_pct=0.01,
    )
    # funding_drag = 0.005 * 3 = 0.015 > risk_pct → adj < 0 → 0
    assert adj == 0.0


def test_size_floors_at_zero_with_extreme_funding():
    """Bei extremem Funding: size = 0."""
    size = compute_position_size(
        asset="BTCUSDT", entry_price=50000.0, sl_distance=500.0,
        capital=RISK_USDT * 20,
        expected_funding_8h=0.005, expected_holding_h=24.0,
    )
    assert size == 0.0


def test_negative_funding_no_increase():
    """Negative Funding (Long bekommt Zahlung) → adj bleibt 1.0 (kein Bonus)."""
    adj = _funding_adjustment(
        expected_funding_8h=-0.001,
        expected_holding_h=8.0,
        risk_per_trade_pct=0.01,
    )
    assert adj == 1.0


def test_zero_sl_returns_zero():
    assert compute_position_size("BTCUSDT", 50000.0, 0.0, 1000.0) == 0.0


def test_zero_entry_price_returns_zero():
    assert compute_position_size("BTCUSDT", 0.0, 500.0, 1000.0) == 0.0
