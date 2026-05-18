"""
P2.1 — clOrdId-Recovery Tests.

Verifiziert das Verhalten von execution/executor.py und
execution/bitget_client.py bei API-Response-Wegfall:

A) Netzwerkfehler + Recovery findet Order → als gefüllt behandeln, kein Duplikat
B) Netzwerkfehler + Recovery findet nichts → Retry mit -R1-Suffix
C) Netzwerkfehler + Recovery-Query selbst schlägt fehl → Originalfehler, kein Blind-Retry
D) Fachliches Exchange-Reject → keine Recovery (kein Netzwerkfehler)
E) Happy-Path (kein Fehler) → Recovery-Code wird nie betreten
F) get_order_by_client_id: dry_run → sofort success=False, kein HTTP
G) get_order_by_client_id: Order gefunden → success=True mit order_id
H) get_order_by_client_id: not_found → success=False, error="not_found"
I) get_order_by_client_id: HTTP-Fehler → success=False, error startet mit "query_failed:"
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.executor import _is_network_error
from execution.bitget_client import OrderResult


# ══════════════════════════════════════════════════════════════════════════════
# Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════════════════

def _make_signal(signal_id: int = 1, asset: str = "BTC",
                 direction: str = "long") -> MagicMock:
    sig = MagicMock()
    sig.id        = signal_id
    sig.asset     = asset
    sig.direction = direction
    sig.entry_price   = 60000.0
    sig.stop_loss     = 58000.0
    sig.take_profit_1 = 62000.0
    sig.take_profit_2 = None
    sig.mode          = "dry_run"
    sig.strategy      = "donchian_breakout"
    return sig


def _ok_result(order_id: str = "ORD-123", price: float = 60100.0,
               size: float = 0.01) -> OrderResult:
    return OrderResult(success=True, order_id=order_id,
                       filled_size=size, avg_price=price)


def _fail_result(error: str) -> OrderResult:
    return OrderResult(success=False, error=error)


# ══════════════════════════════════════════════════════════════════════════════
# _is_network_error — Unit-Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIsNetworkError:
    @pytest.mark.parametrize("error", [
        "ConnectionError: connection refused",
        "Read timed out",
        "Connect timed out after 10s",
        "RemoteDisconnected('Remote end closed connection')",
        "HTTP 502: Bad Gateway",
        "HTTP 503 Service Unavailable",
        "HTTP 504 Gateway Timeout",
        "HTTP 524 A Timeout Occurred",
        "Bitget API Rate Limit nach 4 Versuchen nicht aufgelöst",
        "ChunkedEncodingError",
    ])
    def test_network_errors_detected(self, error):
        assert _is_network_error(error), f"Sollte als Netzwerkfehler erkannt werden: {error!r}"

    @pytest.mark.parametrize("error", [
        "Bitget [40786]: insufficient margin",
        "Bitget [43011]: invalid symbol",
        "Bitget [40001]: invalid parameter",
        "HTTP 400: Bad Request",
        "HTTP 401: Unauthorized",
        "size too small",
        "order rejected by exchange",
    ])
    def test_exchange_rejections_not_network(self, error):
        assert not _is_network_error(error), \
            f"Fachliches Reject sollte NICHT als Netzwerkfehler gelten: {error!r}"


# ══════════════════════════════════════════════════════════════════════════════
# get_order_by_client_id — Unit-Tests (BitgetClient)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetOrderByClientId:
    def _make_client(self, dry_run: bool = False) -> "BitgetClient":
        from execution.bitget_client import BitgetClient
        with patch.dict(os.environ, {"APEX_KEY": "k", "APEX_SECRET": "s", "APEX_PASS": "p"}):
            client = BitgetClient(dry_run=dry_run)
        return client

    def test_dry_run_returns_failure_no_http(self):
        """dry_run → sofort success=False ohne HTTP-Call."""
        client = self._make_client(dry_run=True)
        with patch.object(client, "_get") as mock_get:
            result = client.get_order_by_client_id("BTC", "APEX-V2-SIG-1-E1")
        assert result.success is False
        mock_get.assert_not_called()

    def test_order_found_returns_success(self):
        """Order bei Bitget gefunden → success=True mit order_id."""
        client = self._make_client()
        mock_data = {
            "orderId": "BITGET-456",
            "priceAvg": "60100.5",
            "baseVolume": "0.01",
        }
        with patch.object(client, "_get", return_value=mock_data):
            result = client.get_order_by_client_id("BTC", "APEX-V2-SIG-1-E1")
        assert result.success is True
        assert result.order_id == "BITGET-456"
        assert result.avg_price == pytest.approx(60100.5)
        assert result.filled_size == pytest.approx(0.01)

    def test_not_found_returns_failure(self):
        """_get gibt None/leeres Dict zurück → not_found."""
        client = self._make_client()
        with patch.object(client, "_get", return_value=None):
            result = client.get_order_by_client_id("BTC", "APEX-V2-SIG-1-E1")
        assert result.success is False
        assert result.error == "not_found"

    def test_http_error_returns_query_failed(self):
        """HTTP-Fehler in _get → success=False, error beginnt mit 'query_failed:'."""
        client = self._make_client()
        with patch.object(client, "_get", side_effect=Exception("HTTP 503")):
            result = client.get_order_by_client_id("BTC", "APEX-V2-SIG-1-E1")
        assert result.success is False
        assert result.error.startswith("query_failed:")


# ══════════════════════════════════════════════════════════════════════════════
# Recovery-Logik in _execute_live — Integration (gemockt)
# ══════════════════════════════════════════════════════════════════════════════

def _patch_executor_deps(signal: MagicMock):
    """Patcht alle executor-internen Abhängigkeiten außer BitgetClient."""
    patches = [
        patch("execution.executor.get_connection",
              return_value=MagicMock(execute=MagicMock(
                  return_value=MagicMock(fetchone=MagicMock(return_value=None))))),
        patch("execution.executor._write_audit_log"),
        patch("execution.executor._increment_circuit_breaker"),
        patch("execution.executor._is_circuit_broken", return_value=False),
        patch("execution.executor._calc_sizing",
              return_value={"size": 0.01, "leverage": 5, "notional": 600.0}),
        patch("execution.market_impact_guard.evaluate",
              return_value=MagicMock(
                  order_type="market", ioc_tolerance_bps=10.0,
                  market_impact_check="ok", spread_at_snapshot_bps=2.0,
                  liquidity_score=0.9)),
    ]
    return patches


class TestRecoveryInExecuteLive:
    def _run_execute_live(self, signal, client_mock):
        """Ruft _execute_live auf mit gepatchtem BitgetClient."""
        from execution.executor import Executor
        executor = Executor.__new__(Executor)

        import execution.executor as ex_mod
        patches = _patch_executor_deps(signal)

        ctx_managers = [p.__enter__() for p in patches]
        try:
            with patch("execution.executor.BitgetClient", return_value=client_mock):
                result = ex_mod._execute_live.__get__(executor)(signal, dry_run=False)
        finally:
            for p, cm in zip(patches, ctx_managers):
                p.__exit__(None, None, None)
        return result

    def _make_bitget_client_mock(self):
        client = MagicMock()
        client.is_ready = True
        client.dry_run  = False
        client.get_price.return_value = 60000.0
        client.set_leverage.return_value = True
        return client

    def test_happy_path_no_recovery_called(self):
        """Erfolgreicher place_market_order → get_order_by_client_id nie aufgerufen."""
        signal = _make_signal()
        client = self._make_bitget_client_mock()
        client.place_market_order.return_value = _ok_result()

        with patch("execution.bitget_client.BitgetClient", return_value=client), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker"), \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))):
            import execution.executor as ex_mod
            executor = ex_mod.Executor.__new__(ex_mod.Executor)
            ex_mod.Executor._execute_live(executor, signal, dry_run=False)

        client.get_order_by_client_id.assert_not_called()

    def test_network_error_recovery_finds_order_no_duplicate(self):
        """Netzwerkfehler → Recovery-Query findet Order → kein Duplikat, Trade gespeichert."""
        signal = _make_signal()
        client = self._make_bitget_client_mock()
        client.place_market_order.return_value = _fail_result("ConnectionError: timeout")
        client.get_order_by_client_id.return_value = _ok_result("BITGET-RECOVERED")

        with patch("execution.bitget_client.BitgetClient", return_value=client), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker") as mock_cb, \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))):
            import execution.executor as ex_mod
            executor = ex_mod.Executor.__new__(ex_mod.Executor)
            trade = ex_mod.Executor._execute_live(executor, signal, dry_run=False)

        assert trade is not None, "Recovery erfolgreich → Trade muss zurückgegeben werden"
        # place_market_order genau einmal — kein Duplikat
        assert client.place_market_order.call_count == 1
        client.get_order_by_client_id.assert_called_once()
        mock_cb.assert_not_called()

    def test_network_error_recovery_not_found_retries_r1(self):
        """Netzwerkfehler + Order nicht bei Bitget → Retry mit -R1-Suffix."""
        signal = _make_signal()
        client = self._make_bitget_client_mock()
        client.place_market_order.side_effect = [
            _fail_result("Read timed out"),   # erster Aufruf: Netzwerkfehler
            _ok_result("ORD-R1"),             # zweiter Aufruf (R1): Erfolg
        ]
        # Recovery-Query: Order nicht gefunden (kein query_failed-Prefix)
        client.get_order_by_client_id.return_value = _fail_result("not_found")

        with patch("execution.bitget_client.BitgetClient", return_value=client), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker"), \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))):
            import execution.executor as ex_mod
            executor = ex_mod.Executor.__new__(ex_mod.Executor)
            trade = ex_mod.Executor._execute_live(executor, signal, dry_run=False)

        assert trade is not None, "R1-Retry erfolgreich → Trade muss zurückgegeben werden"
        assert client.place_market_order.call_count == 2
        # R1-Suffix im zweiten Call
        second_call_kwargs = client.place_market_order.call_args_list[1]
        client_oid_used = second_call_kwargs[1].get("client_order_id", "") or \
                          (second_call_kwargs[0][4] if len(second_call_kwargs[0]) > 4 else "")
        assert "-R1" in str(client_oid_used), \
            f"Zweiter Aufruf sollte -R1-Suffix haben, kwargs: {second_call_kwargs}"

    def test_network_error_query_fails_no_blind_retry(self):
        """Netzwerkfehler + Recovery-Query selbst fehlgeschlagen → kein Retry, gibt None zurück."""
        signal = _make_signal()
        client = self._make_bitget_client_mock()
        client.place_market_order.return_value = _fail_result("HTTP 503 Service Unavailable")
        client.get_order_by_client_id.return_value = _fail_result("query_failed:HTTP 503")

        with patch("execution.bitget_client.BitgetClient", return_value=client), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker"), \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))):
            import execution.executor as ex_mod
            executor = ex_mod.Executor.__new__(ex_mod.Executor)
            trade = ex_mod.Executor._execute_live(executor, signal, dry_run=False)

        assert trade is None, "Recovery-Query fehlgeschlagen → kein Trade"
        # place_market_order genau einmal — kein Blind-Retry
        assert client.place_market_order.call_count == 1

    def test_exchange_rejection_no_recovery(self):
        """Fachliches Exchange-Reject → keine Recovery-Query."""
        signal = _make_signal()
        client = self._make_bitget_client_mock()
        client.place_market_order.return_value = _fail_result(
            "Bitget [40786]: insufficient margin"
        )

        with patch("execution.bitget_client.BitgetClient", return_value=client), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker"), \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))):
            import execution.executor as ex_mod
            executor = ex_mod.Executor.__new__(ex_mod.Executor)
            trade = ex_mod.Executor._execute_live(executor, signal, dry_run=False)

        assert trade is None
        client.get_order_by_client_id.assert_not_called()
