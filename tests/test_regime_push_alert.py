"""
E.1 — Tests für Regime-Wechsel Push-Alert.

Prüft:
- Regime-Wechsel (change_detected=True) + send_telegram=True → dispatch() einmal aufgerufen
- Kein Wechsel (change_detected=False) → kein dispatch()
- send_telegram=False → kein dispatch() (default)
- Dispatcher-Fehler crasht run_daily_check() nicht
- Alert-Nachricht enthält Asset und beide Regime-Namen
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.lab_regime_daily_check import run_daily_check, _send_drift_telegram


def _make_snapshot_entry(change_detected: bool, regime: str = "HIGH_VOL",
                          prev_regime: str = "MIXED") -> MagicMock:
    entry = MagicMock()
    entry.change_detected = change_detected
    entry.regime = regime
    entry.prev_regime = prev_regime
    entry.hurst_exponent = 0.5
    return entry


class TestRegimePushAlert:
    def test_change_detected_sends_dispatch(self):
        """Regime-Wechsel + send_telegram=True → dispatch() genau einmal."""
        entry = _make_snapshot_entry(change_detected=True, regime="HIGH_VOL", prev_regime="MIXED")
        with patch("scripts.lab_regime_daily_check.daily_snapshot", return_value=entry):
            with patch("scripts.lab_regime_daily_check._load_prices", return_value=[1.0, 2.0]):
                with patch("scripts.lab_regime_daily_check.get_lab_state_connection"):
                    with patch("scripts.lab_regime_daily_check.dispatch") as mock_dispatch:
                        run_daily_check(["BTC"], send_telegram=True)
        mock_dispatch.assert_called_once()

    def test_no_change_no_dispatch(self):
        """Kein Regime-Wechsel → kein dispatch()."""
        entry = _make_snapshot_entry(change_detected=False)
        with patch("scripts.lab_regime_daily_check.daily_snapshot", return_value=entry):
            with patch("scripts.lab_regime_daily_check._load_prices", return_value=[1.0, 2.0]):
                with patch("scripts.lab_regime_daily_check.get_lab_state_connection"):
                    with patch("scripts.lab_regime_daily_check.dispatch") as mock_dispatch:
                        run_daily_check(["BTC"], send_telegram=True)
        mock_dispatch.assert_not_called()

    def test_send_telegram_false_no_dispatch(self):
        """Default send_telegram=False → kein dispatch(), auch bei Wechsel."""
        entry = _make_snapshot_entry(change_detected=True)
        with patch("scripts.lab_regime_daily_check.daily_snapshot", return_value=entry):
            with patch("scripts.lab_regime_daily_check._load_prices", return_value=[1.0, 2.0]):
                with patch("scripts.lab_regime_daily_check.get_lab_state_connection"):
                    with patch("scripts.lab_regime_daily_check.dispatch") as mock_dispatch:
                        run_daily_check(["BTC"], send_telegram=False)
        mock_dispatch.assert_not_called()

    def test_alert_message_contains_asset_and_regimes(self):
        """Nachricht enthält Asset-Name und beide Regime-Bezeichnungen."""
        _send_drift_telegram("BTC", "MIXED", "HIGH_VOL")  # wird durch mock abgefangen
        # Direkt testen:
        with patch("scripts.lab_regime_daily_check.dispatch") as mock_dispatch:
            _send_drift_telegram("ETH", "LOW_VOL", "TREND")
        msg = mock_dispatch.call_args[0][0]
        assert "ETH" in msg
        assert "LOW_VOL" in msg
        assert "TREND" in msg

    def test_dispatcher_error_does_not_crash(self):
        """Fehler im Dispatcher crasht run_daily_check() nicht."""
        entry = _make_snapshot_entry(change_detected=True)
        with patch("scripts.lab_regime_daily_check.daily_snapshot", return_value=entry):
            with patch("scripts.lab_regime_daily_check._load_prices", return_value=[1.0, 2.0]):
                with patch("scripts.lab_regime_daily_check.get_lab_state_connection"):
                    with patch("scripts.lab_regime_daily_check.dispatch",
                               side_effect=RuntimeError("Netzwerk weg")):
                        results = run_daily_check(["BTC"], send_telegram=True)
        assert "BTC" in results  # Funktion läuft durch

    def test_multiple_assets_only_changed_gets_alert(self):
        """Mehrere Assets: nur das mit change_detected=True erhält Alert."""
        entry_btc = _make_snapshot_entry(change_detected=True, regime="HIGH_VOL")
        entry_eth = _make_snapshot_entry(change_detected=False, regime="MIXED")

        def _snapshot(asset, conn, prices):
            return entry_btc if asset == "BTC" else entry_eth

        with patch("scripts.lab_regime_daily_check.daily_snapshot", side_effect=_snapshot):
            with patch("scripts.lab_regime_daily_check._load_prices", return_value=[1.0, 2.0]):
                with patch("scripts.lab_regime_daily_check.get_lab_state_connection"):
                    with patch("scripts.lab_regime_daily_check.dispatch") as mock_dispatch:
                        run_daily_check(["BTC", "ETH"], send_telegram=True)
        mock_dispatch.assert_called_once()
