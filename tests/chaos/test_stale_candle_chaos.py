"""
Chaos-Test 4: Stale-Candle blockiert Signal mit korrektem reject_reason.

Simuliert einen Marktdaten-Ausfall: letzte Kerze ist 20 Minuten alt.
Signal muss abgelehnt werden mit reject_reason='stale_market_data'.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config.settings import STALE_CANDLE_TOLERANCE_SECONDS


def _make_signal(asset: str = "BTC"):
    from core.models import Signal
    s = Signal(
        strategy="squeeze", asset=asset, direction="long",
        entry_price=50000.0, stop_loss=49000.0,
        take_profit_1=51000.0, take_profit_2=52000.0,
        size=0.01, risk_usd=1.5, session="london",
        status="pending", mode="dry_run",
    )
    s.id = 1
    return s


def test_stale_candle_20min_blocked():
    """Kerze 20 Minuten alt → StaleCandleCheck blockiert."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal("BTC")

    age_ms = (STALE_CANDLE_TOLERANCE_SECONDS + 300) * 1000  # 5min über Limit
    now_ms = int(time.time() * 1000)
    stale_ts = now_ms - age_ms

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {"ts": str(stale_ts)}

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert not passed, "Stale-Candle muss blockieren"
    assert "stale_market_data" in reason


def test_stale_candle_5min_passes():
    """Kerze 5 Minuten alt → StaleCandleCheck lässt durch."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal("ETH")

    age_ms = 5 * 60 * 1000  # 5 Minuten = OK
    now_ms = int(time.time() * 1000)
    fresh_ts = now_ms - age_ms

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {"ts": str(fresh_ts)}

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert passed, f"Frische Kerze muss passieren, reason={reason}"


def test_stale_candle_reject_reason_format():
    """reject_reason enthält Asset und Alter."""
    from governance.checks import StaleCandleCheck
    check = StaleCandleCheck()
    signal = _make_signal("SOL")

    age_ms = (STALE_CANDLE_TOLERANCE_SECONDS + 600) * 1000
    now_ms = int(time.time() * 1000)

    conn_mock = MagicMock()
    conn_mock.execute.return_value.fetchone.return_value = {
        "ts": str(now_ms - age_ms)
    }

    with patch("governance.checks.get_connection", return_value=conn_mock):
        passed, reason = check.evaluate(signal)

    assert not passed
    assert "stale_market_data" in reason
    # Alter muss in der Reason stehen (Zahl vor 's' oder direkt)
    import re
    has_number = bool(re.search(r"\d+", reason))
    assert has_number, f"Reason sollte numerisches Alter enthalten: {reason}"


def test_stale_multi_asset_independence():
    """Stale-Check ist asset-spezifisch — ein stales Asset blockiert nicht andere."""
    from governance.checks import StaleCandleCheck
    check_btc = StaleCandleCheck()
    check_eth = StaleCandleCheck()

    now_ms   = int(time.time() * 1000)
    stale_ms = now_ms - (STALE_CANDLE_TOLERANCE_SECONDS + 300) * 1000
    fresh_ms = now_ms - 60_000  # 1 min

    def make_conn(ts_value):
        conn_mock = MagicMock()
        conn_mock.execute.return_value.fetchone.return_value = {"ts": str(ts_value)}
        return conn_mock

    btc_signal = _make_signal("BTC")
    eth_signal = _make_signal("ETH")

    with patch("governance.checks.get_connection", return_value=make_conn(stale_ms)):
        passed_btc, _ = check_btc.evaluate(btc_signal)

    with patch("governance.checks.get_connection", return_value=make_conn(fresh_ms)):
        passed_eth, _ = check_eth.evaluate(eth_signal)

    assert not passed_btc, "BTC stale → blockiert"
    assert passed_eth,     "ETH frisch → durchgelassen"
