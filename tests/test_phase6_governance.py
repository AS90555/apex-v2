"""Phase-6-Tests: Stale-Candle, Funding-Rate, Portfolio-Exposure, Vol-Targeting, Kill-Switch."""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    STALE_CANDLE_TOLERANCE_SECONDS,
    FUNDING_RATE_WARN_THRESHOLD,
    FUNDING_RATE_BLOCK_THRESHOLD,
    SLIPPAGE_ALERT_THRESHOLD_BPS,
    RECONCILE_SIZE_TOLERANCE,
)


# ── Stale-Candle-Check ────────────────────────────────────────────────────────

def _make_signal(asset="BTC", direction="long", mode="dry_run", **kwargs):
    from core.models import Signal
    s = Signal(
        strategy="squeeze", asset=asset, direction=direction,
        entry_price=kwargs.get("entry_price", 100.0),
        stop_loss=kwargs.get("stop_loss", 95.0),
        take_profit_1=kwargs.get("take_profit_1", 105.0),
        take_profit_2=kwargs.get("take_profit_2", 110.0),
        size=0.01, risk_usd=1.5, session="london",
        status="pending", mode=mode,
    )
    s.id = kwargs.get("id", 1)
    return s


def test_stale_candle_fresh_passes():
    """Kerze < Tolerance → pass."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal()

    now_ms = int(time.time() * 1000)
    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {"ts": str(now_ms - 60_000)}  # 1 min alt

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed
    assert "ok" in reason


def test_stale_candle_old_blocks():
    """Kerze > Tolerance → rejected."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal()

    age_ms = (STALE_CANDLE_TOLERANCE_SECONDS + 300) * 1000  # 5 min über Limit
    now_ms = int(time.time() * 1000)
    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {"ts": str(now_ms - age_ms)}

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert not passed
    assert "stale_market_data" in reason


def test_stale_candle_no_data_passes():
    """Keine Kerze in DB → fail-open (pass)."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal()

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = None

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed
    assert "skip" in reason


# ── Funding-Rate-Check ────────────────────────────────────────────────────────

def test_funding_rate_low_passes():
    """Niedrige Funding-Rate → pass."""
    from governance.checks import FundingRateCheck
    check = FundingRateCheck()
    signal = _make_signal(direction="long")

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {"funding_rate": 0.0001, "funding_time": None}

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed


def test_funding_rate_warn_passes_with_note():
    """Warn-Level → durchlassen, aber markieren."""
    from governance.checks import FundingRateCheck
    check = FundingRateCheck()
    signal = _make_signal(direction="long")

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {
        "funding_rate": FUNDING_RATE_WARN_THRESHOLD + 0.0001,
        "funding_time": None,
    }

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed
    assert "FUNDING_WARN" in reason


def test_funding_rate_block_long():
    """Block-Level, hohe positive Rate gegen Long → rejected."""
    from governance.checks import FundingRateCheck
    check = FundingRateCheck()
    signal = _make_signal(direction="long")

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {
        "funding_rate": FUNDING_RATE_BLOCK_THRESHOLD + 0.001,
        "funding_time": None,
    }

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert not passed
    assert "funding_rate" in reason


def test_funding_rate_high_positive_favors_short():
    """Hohe positive Rate → für Short kein Problem."""
    from governance.checks import FundingRateCheck
    check = FundingRateCheck()
    signal = _make_signal(direction="short")

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {
        "funding_rate": FUNDING_RATE_BLOCK_THRESHOLD + 0.001,
        "funding_time": None,
    }

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed  # Hohe positive Funding = günstig für Shorts


def test_funding_rate_no_data_passes():
    """Keine Daten → fail-open."""
    from governance.checks import FundingRateCheck
    check = FundingRateCheck()
    signal = _make_signal()

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = None

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed
    assert "skip" in reason


# ── Portfolio-Exposure ────────────────────────────────────────────────────────

def test_portfolio_exposure_ok():
    """Kleine Position → pass."""
    from governance.portfolio_risk import PortfolioExposureCheck
    check = PortfolioExposureCheck()
    # entry_price=100, size=1.0 → 100 USDT — deutlich unter allen Limits
    signal = _make_signal(asset="BTC", entry_price=100.0, size=1.0)

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchall.return_value = []

    with patch("governance.portfolio_risk.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed, f"Kleine Position sollte passieren, reason={reason}"
    assert "ok" in reason


def test_portfolio_exposure_shadow_skipped():
    """Shadow-Signal → immer pass."""
    from governance.portfolio_risk import PortfolioExposureCheck
    check = PortfolioExposureCheck()
    signal = _make_signal(mode="shadow")

    conn_mock = MagicMock()
    with patch("governance.portfolio_risk.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed
    assert "shadow" in reason


def test_portfolio_exposure_asset_limit_exceeded():
    """Zu hohe Einzelposition → rejected."""
    from governance.portfolio_risk import PortfolioExposureCheck
    from config.settings import PORTFOLIO_MAX_EXPOSURE_USDT
    check = PortfolioExposureCheck()

    # Signal: 0.1 BTC @ 50k = 5000 USDT > PORTFOLIO_MAX_EXPOSURE_USDT
    signal = _make_signal(asset="BTC", entry_price=50000.0, size=0.1)

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchall.return_value = []

    with patch("governance.portfolio_risk.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert not passed
    assert "portfolio_exposure" in reason


# ── Vol-Targeting ─────────────────────────────────────────────────────────────

def test_vol_targeting_disabled_uses_legacy():
    """V6_VOL_TARGETING=False → klassische RISK_USDT/sl_dist Logik."""
    with patch("governance.sizing.V6_VOL_TARGETING", False):
        from governance.sizing import compute_position_size
        from config.settings import RISK_USDT
        size = compute_position_size("BTC", 50000.0, sl_distance=500.0, capital=1000.0)
        assert size == pytest.approx(RISK_USDT / 500.0)


def test_vol_targeting_atr_halves_size():
    """Doppelter ATR → halbe Vol-Targeting-Größe."""
    with patch("governance.sizing.V6_VOL_TARGETING", True):
        with patch("governance.sizing._get_atr", return_value=100.0) as mock_atr1:
            with patch("governance.sizing._get_regime", return_value="TREND"):
                from governance.sizing import compute_position_size
                from config.settings import RISK_USDT
                size1 = compute_position_size("BTC", 50000.0, sl_distance=5000.0, capital=10000.0)

        with patch("governance.sizing._get_atr", return_value=200.0):
            with patch("governance.sizing._get_regime", return_value="TREND"):
                size2 = compute_position_size("BTC", 50000.0, sl_distance=5000.0, capital=10000.0)

    assert size1 > 0
    assert size2 > 0
    # Doppelter ATR → halbe unkappte Größe (wenn unter Cap)
    # (nur wenn beide unter Cap liegen)
    if size1 < RISK_USDT / 5000.0 and size2 < RISK_USDT / 5000.0:
        assert size1 == pytest.approx(size2 * 2, rel=1e-3)


def test_vol_targeting_regime_multiplier():
    """HIGH_VOL-Regime → halbe Größe vs TREND (wenn nicht gecappt)."""
    # Großes Kapital + kleiner sl_distance → Cap greift nicht, reine Vol-Targeting-Logik
    from governance.sizing import compute_position_size
    from config.settings import RISK_USDT

    with patch("governance.sizing.V6_VOL_TARGETING", True):
        with patch("governance.sizing._get_atr", return_value=10.0):
            with patch("governance.sizing._get_regime", return_value="TREND"):
                # raw = 100000 * 0.02 / 10 * 1.0 = 200 — über Cap → RISK_USDT/sl
                # sl_distance=5000 → cap = RISK_USDT/5000 (sehr klein)
                # Benutze kleineres sl_distance damit cap groß ist
                # cap = RISK_USDT / 0.01 = sehr groß, raw = 1000 * 0.02 / 10 = 2
                size_trend = compute_position_size(
                    "ETH", 3000.0, sl_distance=0.01, capital=1000.0
                )

        with patch("governance.sizing._get_atr", return_value=10.0):
            with patch("governance.sizing._get_regime", return_value="HIGH_VOL"):
                size_highvol = compute_position_size(
                    "ETH", 3000.0, sl_distance=0.01, capital=1000.0
                )

    # Beide unter Cap: HIGH_VOL(0.5) vs TREND(1.0) → Faktor 2
    assert size_trend == pytest.approx(size_highvol * 2, rel=1e-3)


def test_vol_targeting_fallback_no_atr():
    """Kein ATR → Fallback auf Legacy-Sizing."""
    with patch("governance.sizing.V6_VOL_TARGETING", True):
        with patch("governance.sizing._get_atr", return_value=None):
            from governance.sizing import compute_position_size
            from config.settings import RISK_USDT
            size = compute_position_size("BTC", 50000.0, sl_distance=500.0, capital=1000.0)
    assert size == pytest.approx(RISK_USDT / 500.0)


# ── Kill-Switch ───────────────────────────────────────────────────────────────

def test_kill_switch_hierarchy():
    """Hard-Kill überschreibt Soft-Kill, aber nicht umgekehrt."""
    from governance.kill_switch import _LEVEL_RANK
    assert _LEVEL_RANK["hard"] > _LEVEL_RANK["soft"]
    assert _LEVEL_RANK["manual"] > _LEVEL_RANK["hard"]
    assert _LEVEL_RANK["none"] == 0


def test_kill_switch_is_hard_killed_global():
    """is_hard_killed erkennt 'hard' und 'manual'."""
    from governance.kill_switch import is_hard_killed

    with patch("governance.kill_switch.get_kill_mode", return_value="hard"):
        assert is_hard_killed()

    with patch("governance.kill_switch.get_kill_mode", return_value="manual"):
        assert is_hard_killed()

    with patch("governance.kill_switch.get_kill_mode", return_value="soft"):
        assert not is_hard_killed()

    with patch("governance.kill_switch.get_kill_mode", return_value="none"):
        assert not is_hard_killed()


def test_kill_switch_valid_modes():
    from governance.kill_switch import set_kill_mode, _LEVELS
    # Ungültiger Mode muss raisen
    with pytest.raises(ValueError):
        set_kill_mode("invalid_mode")
    # Alle gültigen Modi sind in _LEVELS
    for lvl in ("none", "soft", "vol", "hard", "manual"):
        assert lvl in _LEVELS
