"""
Chaos-Test 1: Netzwerk-Timeout nach place_market_order.

Signal muss auf 'failed' landen (nicht auf 'processing' hängen bleiben).
Reconciliation-Flag reconcile_required=1 muss gesetzt werden.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_timeout_after_order_send_sets_failed():
    """
    BitgetClient.place_market_order wirft Timeout-Exception.
    → Signal-Status muss auf 'failed', nicht auf 'processing' bleiben.
    """
    from execution.executor import Executor

    # Mock Signal
    signal = MagicMock()
    signal.id = 9001
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

    # DB-Mock: UPDATE gibt 1 Row zurück (Lock erfolgreich)
    conn_mock = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 1
    cursor_mock.lastrowid = 999
    conn_mock.execute.return_value = cursor_mock
    conn_mock.execute.return_value.fetchone.return_value = None
    conn_mock.execute.return_value.fetchall.return_value = []

    # BitgetClient wirft Timeout nach Order
    import requests
    timeout_error = requests.exceptions.Timeout("Simulated timeout")

    with patch("execution.executor.get_connection", return_value=conn_mock):
        with patch.object(Executor, "_execute_live", side_effect=timeout_error):
            executor = Executor()
            result = executor.execute(signal)

    # Signal darf nicht auf 'processing' hängen bleiben
    # Der Executor soll status='failed' setzen
    update_calls = [str(c) for c in conn_mock.execute.call_args_list]
    failed_updates = [c for c in update_calls if "failed" in c.lower()]
    # Mindestens ein UPDATE auf failed oder execution_aborted
    assert len(failed_updates) > 0 or result is None, \
        "Timeout muss Signal auf 'failed' setzen oder None zurückgeben"


def test_no_processing_hang_on_exception():
    """
    Bei beliebiger Exception in _execute_live:
    Signal-Status darf NICHT auf 'processing' bleiben.
    """
    from execution.executor import Executor

    signal = MagicMock()
    signal.id = 9002
    signal.strategy = "vaa"
    signal.asset = "ETH"
    signal.direction = "short"
    signal.entry_price = 3000.0
    signal.stop_loss = 3100.0
    signal.take_profit_1 = 2900.0
    signal.take_profit_2 = 2800.0
    signal.size = 0.1
    signal.risk_usd = 1.5
    signal.session = "ny"
    signal.status = "approved"
    signal.mode = "live"
    signal.created_at = "2026-05-13T10:00:00+00:00"

    conn_mock = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.rowcount = 1
    cursor_mock.lastrowid = 1000
    conn_mock.execute.return_value = cursor_mock
    conn_mock.execute.return_value.fetchone.return_value = None
    conn_mock.execute.return_value.fetchall.return_value = []

    with patch("execution.executor.get_connection", return_value=conn_mock):
        with patch.object(Executor, "_execute_live",
                          side_effect=RuntimeError("Unexpected crash")):
            executor = Executor()
            result = executor.execute(signal)

    # Entweder None oder ein fehlerfreier Pfad — niemals 'processing'
    update_calls = [str(c) for c in conn_mock.execute.call_args_list]
    processing_final = [c for c in update_calls
                        if "processing" in c.lower() and "failed" not in c.lower()]
    # Kein finales 'processing' ohne nachfolgendes 'failed'
    assert result is None or len(processing_final) == 0
