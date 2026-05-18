"""Phase-7-Tests: Market-Impact-Guard, Liquidity-Score, IOC-Toleranz."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    MARKET_IMPACT_THRESHOLD,
    IOC_SLIPPAGE_TOLERANCE_BASE,
    WORST_CASE_TOLERANCE,
    LIQUIDITY_DEGRADATION_THRESHOLD,
    LIQUIDITY_STRESS_MULTIPLIER,
)


# ── Liquidity-Score ────────────────────────────────────────────────────────────

def test_liquidity_score_perfect():
    """Kein Spread, maximale Tiefe → Score 1.0."""
    from scripts.run_liquidity_intake import _compute_liquidity_score
    score = _compute_liquidity_score(spread_bps=0.0, depth_l1=100_000.0, depth_l3=300_000.0)
    assert score == pytest.approx(1.0)


def test_liquidity_score_worst():
    """50bps Spread, keine Tiefe → Score 0.0."""
    from scripts.run_liquidity_intake import _compute_liquidity_score
    score = _compute_liquidity_score(spread_bps=50.0, depth_l1=0.0, depth_l3=0.0)
    assert score == pytest.approx(0.0)


def test_liquidity_score_midrange():
    """Mittlerer Spread, mittlere Tiefe → Score um 0.5."""
    from scripts.run_liquidity_intake import _compute_liquidity_score
    score = _compute_liquidity_score(spread_bps=25.0, depth_l1=25_000.0, depth_l3=75_000.0)
    assert 0.0 < score < 1.0


def test_liquidity_score_clamped():
    """Score immer in [0, 1]."""
    from scripts.run_liquidity_intake import _compute_liquidity_score
    assert _compute_liquidity_score(100.0, 0.0, 0.0) >= 0.0
    assert _compute_liquidity_score(0.0, 1_000_000.0, 0.0) <= 1.0


# ── Market-Impact-Guard: Guard deaktiviert ─────────────────────────────────────

def test_guard_disabled_always_market():
    """V6_MARKET_IMPACT_GUARD=False → immer Market Order."""
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", False):
        from execution.market_impact_guard import evaluate
        decision = evaluate("BTC", order_size_usd=100.0)
    assert decision.order_type == "market"
    assert decision.market_impact_check == "disabled"


# ── Market-Impact-Guard: kleine Order → Market ────────────────────────────────

def test_small_order_market():
    """Kleine Order relativ zu L1-Tiefe → Market Order."""
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        mock_liq = {
            "liquidity_score": 0.9,
            "avg_spread_bps": 2.0,
            "avg_depth_level1_usd": 100_000.0,
            "measured_at": _now,
        }
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=mock_liq):
            with patch("execution.market_impact_guard._get_regime", return_value="TREND"):
                from execution.market_impact_guard import evaluate
                # 100 USDT ≤ 10% × 100k = 10k → Market
                decision = evaluate("BTC", order_size_usd=100.0, client=None)

    assert decision.order_type == "market"
    assert decision.market_impact_check == "ok"


# ── Market-Impact-Guard: große Order → IOC ────────────────────────────────────

def test_large_order_ioc():
    """Order > 10% L1-Tiefe → IOC-Limit."""
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        mock_liq = {
            "liquidity_score": 0.8,
            "avg_spread_bps": 3.0,
            "avg_depth_level1_usd": 1_000.0,  # sehr gering
            "measured_at": "2026-05-13T12:00:00+00:00",
        }
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=mock_liq):
            with patch("execution.market_impact_guard._get_regime", return_value="TREND"):
                from execution.market_impact_guard import evaluate
                # 500 USDT > 10% × 1k = 100 → IOC
                decision = evaluate("ETH", order_size_usd=500.0, client=None)

    assert decision.order_type == "ioc_limit"


# ── Market-Impact-Guard: degradierte Liquidität ────────────────────────────────

def test_degraded_liquidity_stress_multiplier():
    """Score < LIQUIDITY_DEGRADATION_THRESHOLD → Toleranz verdoppelt."""
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        mock_liq = {
            "liquidity_score": LIQUIDITY_DEGRADATION_THRESHOLD - 0.1,  # unterhalb
            "avg_spread_bps": 20.0,
            "avg_depth_level1_usd": 500_000.0,  # riesig → Market
            "measured_at": _now,
        }
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=mock_liq):
            with patch("execution.market_impact_guard._get_regime", return_value="TREND"):
                from execution.market_impact_guard import evaluate
                decision = evaluate("SOL", order_size_usd=50.0, client=None)

    assert decision.market_impact_check == "degraded"
    expected_min = IOC_SLIPPAGE_TOLERANCE_BASE * LIQUIDITY_STRESS_MULTIPLIER
    assert decision.ioc_tolerance_bps >= expected_min


# ── Market-Impact-Guard: stale Daten ─────────────────────────────────────────

def test_stale_liquidity_data_worst_case():
    """Daten > 48h alt → WORST_CASE_TOLERANCE."""
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        mock_liq = {
            "liquidity_score": 0.9,
            "avg_spread_bps": 2.0,
            "avg_depth_level1_usd": 100_000.0,
            "measured_at": "2026-05-11T00:00:00+00:00",  # > 48h in der Vergangenheit
        }
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=mock_liq):
            with patch("execution.market_impact_guard._get_regime", return_value="TREND"):
                from execution.market_impact_guard import evaluate
                decision = evaluate("BTC", order_size_usd=100.0, client=None)

    assert decision.market_impact_check == "stale"
    assert decision.ioc_tolerance_bps == WORST_CASE_TOLERANCE
    assert decision.order_type == "ioc_limit"


# ── Market-Impact-Guard: keine Liquiditätsdaten ────────────────────────────────

def test_no_liquidity_data_fallback():
    """Keine Daten in DB → Fallback-Logik (kein Crash)."""
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=None):
            with patch("execution.market_impact_guard._get_regime", return_value="UNDEFINED"):
                from execution.market_impact_guard import evaluate
                decision = evaluate("XRP", order_size_usd=100.0, client=None)

    # Bei depth_l1=0: order_size > 0*threshold → ioc_limit
    assert decision is not None
    assert decision.ioc_tolerance_bps > 0


# ── IOC-Toleranz-Grenzen ──────────────────────────────────────────────────────

def test_ioc_tolerance_never_exceeds_worst_case():
    """IOC-Toleranz nie über WORST_CASE_TOLERANCE."""
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", True):
        mock_liq = {
            "liquidity_score": 0.1,   # sehr schlecht
            "avg_spread_bps": 40.0,
            "avg_depth_level1_usd": 100.0,
            "measured_at": "2026-05-13T12:00:00+00:00",
        }
        with patch("execution.market_impact_guard._get_liquidity_metrics", return_value=mock_liq):
            with patch("execution.market_impact_guard._get_regime", return_value="HIGH_VOL"):
                from execution.market_impact_guard import evaluate
                decision = evaluate("DOGE", order_size_usd=100.0, client=None)

    assert decision.ioc_tolerance_bps <= WORST_CASE_TOLERANCE


def test_ioc_tolerance_positive():
    from execution.market_impact_guard import evaluate
    with patch("execution.market_impact_guard.V6_MARKET_IMPACT_GUARD", False):
        decision = evaluate("BTC", order_size_usd=100.0)
    assert decision.ioc_tolerance_bps > 0


# ── Konstanten-Sanity ─────────────────────────────────────────────────────────

def test_constants_sane():
    assert MARKET_IMPACT_THRESHOLD > 0
    assert IOC_SLIPPAGE_TOLERANCE_BASE > 0
    assert WORST_CASE_TOLERANCE > IOC_SLIPPAGE_TOLERANCE_BASE
    assert 0 < LIQUIDITY_DEGRADATION_THRESHOLD < 1
    assert LIQUIDITY_STRESS_MULTIPLIER > 1
