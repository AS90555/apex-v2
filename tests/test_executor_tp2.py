"""
P2.2 / H0 — TP2-Hard-Kill-Fallback Tests.

API-Befund (Bitget v2 USDT-FUTURES): place-order unterstützt nur einen Preset-TP-Slot
(presetStopSurplusPrice). Atomare TP2-Platzierung ist nicht möglich. TP2 wird daher
als separater place-tpsl-order platziert — scheitert er nach Retry, ist der Hard-Kill-
Fallback (Position schließen + set_kill_mode("hard")) verbindlich und hier test-bewiesen.

Prüft:
- TP2-Fehler nach Retry → Position wird geschlossen, Hard-Kill gesetzt, Trade=None
- TP2-Fehler beim ersten Versuch aber Erfolg beim Retry → Trade normal gespeichert
- TP2-Erfolg beim ersten Versuch → normaler Pfad (kein Close)
- take_profit_2=None → kein TPSL-Call, Execution läuft normal durch
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.bitget_client import OrderResult


def _make_signal(sig_id: int = 1, direction: str = "long") -> MagicMock:
    sig = MagicMock()
    sig.id = sig_id
    sig.strategy = "donchian_breakout"
    sig.asset = "BTC"
    sig.direction = direction
    sig.entry_price = 60000.0
    sig.stop_loss = 59000.0
    sig.take_profit_1 = 61000.0
    sig.take_profit_2 = 62000.0
    sig.size = 0.01
    sig.risk_usd = 1.5
    sig.session = "london"
    sig.status = "approved"
    sig.mode = "dry_run"
    sig.created_at = "2026-01-01T00:00:00+00:00"
    return sig


def _make_client_mock(tp2_results) -> MagicMock:
    """Erstellt einen Mock-Client mit vorgegebenem TP2-Verhalten."""
    client = MagicMock()
    client.is_ready = True
    client.get_price.return_value = 60000.0
    client.set_leverage.return_value = True
    ok_fill = OrderResult(success=True, order_id="ORD-1", filled_size=0.01, avg_price=60000.0)
    client.place_market_order.return_value = ok_fill
    if isinstance(tp2_results, list):
        client.place_take_profit.side_effect = tp2_results
    else:
        client.place_take_profit.return_value = tp2_results
    return client


class TestTP2FailureFallback:
    def test_tp2_fails_after_retry_closes_position_and_kills(self):
        """TP2 schlägt nach Retry fehl → Position close + Hard-Kill + None zurück."""
        from execution.executor import Executor

        sig = _make_signal(sig_id=42)
        tp2_fail = OrderResult(success=False, error="API timeout", avg_price=0.0)
        mock_client = _make_client_mock(tp2_results=tp2_fail)

        with (
            patch("execution.bitget_client.BitgetClient", return_value=mock_client),
            patch("execution.executor.get_connection", return_value=MagicMock()),
            patch("governance.kill_switch.set_kill_mode") as mock_kill,
            patch("execution.executor._is_circuit_broken", return_value=False),
            patch("execution.executor._calc_sizing", return_value={"size": 0.01, "leverage": 1, "notional": 600.0, "hold_side": "long"}),
            patch("execution.market_impact_guard.evaluate", return_value=type("MIG", (), {"order_type": "market", "ioc_tolerance_bps": 5.0, "market_impact_check": "disabled", "spread_at_snapshot_bps": 1.0, "liquidity_score": 1.0})()),
            patch("time.sleep"),
        ):
            executor = Executor()
            result = executor._execute_live(sig, dry_run=False)

        assert result is None, "Bei TP2-Fehler muss None zurückgegeben werden"
        # Zwei TP2-Versuche (original + Retry)
        assert mock_client.place_take_profit.call_count == 2
        # Position wurde geschlossen (reduce_only=True)
        close_calls = [
            c for c in mock_client.place_market_order.call_args_list
            if c.kwargs.get("reduce_only") is True
        ]
        assert len(close_calls) >= 1, "Position muss bei TP2-Fehler geschlossen werden"
        # Hard-Kill gesetzt
        mock_kill.assert_called_once()
        assert mock_kill.call_args.args[0] == "hard"

    def test_tp2_fails_first_succeeds_retry_normal_trade(self):
        """TP2 schlägt beim ersten Versuch fehl, Retry erfolgreich → Trade normal gespeichert."""
        from execution.executor import Executor

        sig = _make_signal(sig_id=43)
        tp2_fail = OrderResult(success=False, error="momentaner Fehler", avg_price=0.0)
        tp2_ok = OrderResult(success=True, avg_price=62000.0)
        mock_client = _make_client_mock(tp2_results=[tp2_fail, tp2_ok])

        conn_mock = MagicMock()
        cur = MagicMock()
        cur.rowcount = 1
        cur.lastrowid = 999
        conn_mock.execute.return_value = cur
        conn_mock.execute.return_value.fetchone.return_value = None

        with (
            patch("execution.bitget_client.BitgetClient", return_value=mock_client),
            patch("execution.executor.get_connection", return_value=conn_mock),
            patch("governance.kill_switch.set_kill_mode") as mock_kill,
            patch("execution.executor._is_circuit_broken", return_value=False),
            patch("execution.executor._calc_sizing", return_value={"size": 0.01, "leverage": 1, "notional": 600.0, "hold_side": "long"}),
            patch("execution.market_impact_guard.evaluate", return_value=type("MIG", (), {"order_type": "market", "ioc_tolerance_bps": 5.0, "market_impact_check": "disabled", "spread_at_snapshot_bps": 1.0, "liquidity_score": 1.0})()),
            patch("time.sleep"),
        ):
            executor = Executor()
            result = executor._execute_live(sig, dry_run=False)

        assert result is not None, "Bei erfolgreichem Retry muss Trade zurückgegeben werden"
        mock_kill.assert_not_called()
        assert mock_client.place_take_profit.call_count == 2

    def test_tp2_success_first_try_no_close(self):
        """TP2 beim ersten Versuch erfolgreich → kein Close, kein Kill."""
        from execution.executor import Executor

        sig = _make_signal(sig_id=44)
        tp2_ok = OrderResult(success=True, avg_price=62000.0)
        mock_client = _make_client_mock(tp2_results=tp2_ok)

        with (
            patch("execution.bitget_client.BitgetClient", return_value=mock_client),
            patch("execution.executor.get_connection", return_value=MagicMock()),
            patch("governance.kill_switch.set_kill_mode") as mock_kill,
            patch("execution.executor._is_circuit_broken", return_value=False),
            patch("execution.executor._calc_sizing", return_value={"size": 0.01, "leverage": 1, "notional": 600.0, "hold_side": "long"}),
            patch("execution.market_impact_guard.evaluate", return_value=type("MIG", (), {"order_type": "market", "ioc_tolerance_bps": 5.0, "market_impact_check": "disabled", "spread_at_snapshot_bps": 1.0, "liquidity_score": 1.0})()),
        ):
            executor = Executor()
            result = executor._execute_live(sig, dry_run=False)

        assert result is not None
        mock_kill.assert_not_called()
        close_calls = [
            c for c in mock_client.place_market_order.call_args_list
            if c.kwargs.get("reduce_only") is True
        ]
        assert len(close_calls) == 0

    def test_tp2_none_no_tpsl_call(self):
        """take_profit_2=None → place_take_profit nie aufgerufen, Trade wird normal gespeichert."""
        from execution.executor import Executor

        sig = _make_signal(sig_id=45)
        sig.take_profit_2 = None  # kein TP2 in diesem Signal
        tp2_ok = OrderResult(success=True, avg_price=62000.0)
        mock_client = _make_client_mock(tp2_results=tp2_ok)

        with (
            patch("execution.bitget_client.BitgetClient", return_value=mock_client),
            patch("execution.executor.get_connection", return_value=MagicMock()),
            patch("governance.kill_switch.set_kill_mode") as mock_kill,
            patch("execution.executor._is_circuit_broken", return_value=False),
            patch("execution.executor._calc_sizing", return_value={"size": 0.01, "leverage": 1, "notional": 600.0, "hold_side": "long"}),
            patch("execution.market_impact_guard.evaluate", return_value=type("MIG", (), {"order_type": "market", "ioc_tolerance_bps": 5.0, "market_impact_check": "disabled", "spread_at_snapshot_bps": 1.0, "liquidity_score": 1.0})()),
        ):
            executor = Executor()
            result = executor._execute_live(sig, dry_run=False)

        assert result is not None, "Kein TP2 → normaler Trade erwartet"
        mock_client.place_take_profit.assert_not_called()
        mock_kill.assert_not_called()
