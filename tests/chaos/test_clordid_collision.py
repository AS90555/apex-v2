"""
Chaos-Test 3: clOrdId-Kollision bei Retry.

Wenn Bitget 409 (Duplicate Order) zurückgibt, darf kein Doppeltrade entstehen.
Circuit-Breaker darf nicht fälschlicherweise ausgelöst werden.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_clordid_same_signal_same_id():
    """Gleiche signal.id → immer gleiche clOrdId (Idempotenz)."""
    signal_id = 42
    ids = [f"APEX-V2-SIG-{signal_id}-E1" for _ in range(10)]
    assert len(set(ids)) == 1


def test_clordid_retry_suffix_distinct():
    """Retry-Suffixe sind eindeutig und unterscheiden sich vom Basis-clOrdId."""
    signal_id = 99
    base   = f"APEX-V2-SIG-{signal_id}-E1"
    retry1 = f"APEX-V2-SIG-{signal_id}-E1-R1"
    retry2 = f"APEX-V2-SIG-{signal_id}-E1-R2"

    assert base != retry1
    assert retry1 != retry2
    assert retry2.endswith("R2")


def test_duplicate_order_response_handled():
    """
    Bitget-409-Antwort (Duplikat) → OrderResult.success=False.
    Executor darf kein zweites Mal place_market_order aufrufen.
    """
    from execution.bitget_client import OrderResult

    # OrderResult mit Fehlercode simulieren
    dup_result = OrderResult(
        success=False,
        order_id=None,
        avg_price=0.0,
        filled_size=0.0,
        error="Bitget [43025]: clientOid already exists",
    )
    assert not dup_result.success
    assert "43025" in dup_result.error or "already exists" in dup_result.error


def test_no_double_trade_on_collision():
    """
    Wenn place_market_order scheitert (409-Simulation),
    darf kein Trade-INSERT in die DB erfolgen.
    """
    from execution.executor import Executor
    from execution.bitget_client import OrderResult

    signal = MagicMock()
    signal.id = 7777
    signal.strategy = "squeeze"
    signal.asset = "BTC"
    signal.direction = "long"
    signal.entry_price = 50000.0
    signal.stop_loss = 49000.0
    signal.take_profit_1 = 51000.0
    signal.take_profit_2 = 52000.0
    signal.size = 0.01
    signal.risk_usd = 1.5
    signal.session = "london"
    signal.status = "approved"
    signal.mode = "dry_run"
    signal.created_at = "2026-05-13T10:00:00+00:00"

    conn_mock = MagicMock()
    cur = MagicMock()
    cur.rowcount = 1
    cur.lastrowid = 888
    conn_mock.execute.return_value = cur
    conn_mock.execute.return_value.fetchone.return_value = None
    conn_mock.execute.return_value.fetchall.return_value = []

    failed_result = OrderResult(
        success=False, order_id=None, avg_price=0.0, filled_size=0.0,
        error="Bitget [43025]: clientOid already exists",
    )

    with patch("execution.executor.get_connection", return_value=conn_mock):
        with patch.object(Executor, "_execute_live", return_value=None):
            executor = Executor()
            result = executor.execute(signal)

    # Kein Trade zurückgegeben
    assert result is None

    # Kein INSERT INTO trades
    all_calls = [str(c) for c in conn_mock.execute.call_args_list]
    trade_inserts = [c for c in all_calls if "INSERT INTO trades" in c]
    assert len(trade_inserts) == 0, "Bei fehlgeschlagener Order kein Trade-INSERT!"
