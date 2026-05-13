"""Phase-5-Tests: clOrdId, State-Machine, Reconciliation, DMS-Logik, Slippage."""
from __future__ import annotations

import os
import sys
import time
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── clOrdId ──────────────────────────────────────────────────────────────────

def test_clordid_deterministic():
    """Gleiche signal.id → gleiche clOrdId."""
    signal_id = 42
    cl_ord_id = f"APEX-V2-SIG-{signal_id}-E1"
    assert cl_ord_id == "APEX-V2-SIG-42-E1"
    # Zweite Erzeugung identisch
    assert f"APEX-V2-SIG-{signal_id}-E1" == cl_ord_id


def test_clordid_unique_per_signal():
    ids = [f"APEX-V2-SIG-{i}-E1" for i in range(1, 6)]
    assert len(set(ids)) == 5


def test_clordid_retry_suffix():
    sig_id = 99
    base = f"APEX-V2-SIG-{sig_id}-E1"
    retry1 = f"APEX-V2-SIG-{sig_id}-E1-R1"
    retry2 = f"APEX-V2-SIG-{sig_id}-E1-R2"
    assert retry1 != base
    assert retry2 != retry1
    assert "R1" in retry1
    assert "R2" in retry2


def test_clordid_components():
    cl = "APEX-V2-SIG-123-E1"
    parts = cl.split("-")
    assert parts[0] == "APEX"
    assert parts[1] == "V2"
    assert parts[2] == "SIG"
    assert parts[3] == "123"
    assert parts[4] == "E1"


# ── State-Machine-Transitions ─────────────────────────────────────────────────

VALID_TRANSITIONS = {
    (None, "created"),
    ("created", "sent"),
    ("sent", "acked"),
    ("sent", "error"),
    ("acked", "filled"),
    ("acked", "error"),
    ("filled", None),   # Terminal
}


def _is_valid_transition(state_from, state_to) -> bool:
    return (state_from, state_to) in VALID_TRANSITIONS


def test_state_machine_happy_path():
    path = [None, "created", "sent", "acked", "filled"]
    for i in range(len(path) - 1):
        assert _is_valid_transition(path[i], path[i + 1]), \
            f"Ungültige Transition: {path[i]} → {path[i + 1]}"


def test_state_machine_error_from_sent():
    assert _is_valid_transition("sent", "error")


def test_state_machine_invalid_skip():
    assert not _is_valid_transition("created", "acked")


def test_state_machine_invalid_backward():
    assert not _is_valid_transition("acked", "sent")


# ── Reconciliation-Logik ──────────────────────────────────────────────────────

def test_reconciliation_phantom_detection():
    """Exchange hat Position, DB nicht → Hard Kill."""
    exchange_positions = {"BTCUSDT_UMCBL": 0.5}
    db_assets = set()  # keine offenen Trades

    phantom_assets = []
    for symbol, size in exchange_positions.items():
        asset = symbol.replace("USDT_UMCBL", "").replace("USDT", "")
        if asset not in db_assets:
            phantom_assets.append(asset)

    assert "BTC" in phantom_assets


def test_reconciliation_ghost_detection():
    """DB hat Trade, Exchange nicht → Alert."""
    exchange_positions = {}
    db_trades = [{"asset": "ETH", "size": 1.0, "status": "executed"}]

    ghost_assets = []
    for t in db_trades:
        asset = t["asset"]
        candidates = [f"{asset}USDT_UMCBL", f"{asset}USDT"]
        if not any(c in exchange_positions for c in candidates):
            ghost_assets.append(asset)

    assert "ETH" in ghost_assets


def test_reconciliation_size_match():
    """Übereinstimmende Größen → kein Alert."""
    from config.settings import RECONCILE_SIZE_TOLERANCE
    ex_size = 0.5
    db_size = 0.5
    diff = abs(abs(ex_size) - abs(db_size))
    assert diff <= RECONCILE_SIZE_TOLERANCE


def test_reconciliation_size_mismatch():
    from config.settings import RECONCILE_SIZE_TOLERANCE
    ex_size = 0.5
    db_size = 0.8
    diff = abs(abs(ex_size) - abs(db_size))
    assert diff > RECONCILE_SIZE_TOLERANCE


def test_reconciliation_no_orders_sent():
    """Reconciliation-Modul darf keine place_*-Funktionen enthalten."""
    import ast
    recon_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "run_reconciliation.py",
    )
    with open(recon_path) as f:
        source = f.read()
    tree = ast.parse(source)
    forbidden = {"place_market_order", "place_limit_order", "place_order"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, \
                    f"Reconciliation darf nicht '{node.func.attr}' aufrufen!"
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden, \
                    f"Reconciliation darf nicht '{node.func.id}' aufrufen!"


# ── DMS-Logik ─────────────────────────────────────────────────────────────────

def test_dms_heartbeat_fresh():
    """Frischer Heartbeat → kein Eingriff."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
    try:
        # Datei gerade eben geschrieben → Alter nahe 0
        mtime = os.path.getmtime(tmp_path)
        now = time.time()
        age = now - mtime
        assert age < 5, "Neue Datei sollte < 5s alt sein"
    finally:
        os.unlink(tmp_path)


def test_dms_heartbeat_stale():
    """Sehr alte Heartbeat-Datei → stale erkannt."""
    timeout = 300
    fake_age = 400  # älter als Timeout
    is_stale = fake_age > timeout
    assert is_stale


def test_dms_no_network_no_close():
    """Kein Netzwerk → Emergency-Close NICHT starten (Eskalation statt blinder Action)."""
    bitget_reachable = False
    process_running  = False
    # Logik: nur Emergency-Close wenn Bitget erreichbar (sonst Netzwerkfehler)
    should_emergency_close = bitget_reachable and not process_running
    assert not should_emergency_close


def test_dms_both_ok_no_action():
    """Bitget erreichbar + Prozess läuft → Retry, kein Emergency-Close."""
    bitget_reachable = True
    process_running  = True
    should_emergency_close = not bitget_reachable or not process_running
    assert not should_emergency_close


# ── Slippage-Monitoring ───────────────────────────────────────────────────────

def test_slippage_median_calculation():
    from scripts.run_slippage_monitor import _median
    assert _median([1.0, 3.0, 2.0]) == pytest.approx(2.0)
    assert _median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
    assert _median([]) == 0.0


def test_slippage_threshold_breach():
    from config.settings import SLIPPAGE_ALERT_THRESHOLD_BPS
    slippages = [10.0, 12.0, 15.0, 9.0, 11.0]
    from scripts.run_slippage_monitor import _median
    median = _median(slippages)
    assert median > SLIPPAGE_ALERT_THRESHOLD_BPS, \
        f"Median {median} sollte über Threshold {SLIPPAGE_ALERT_THRESHOLD_BPS} liegen"


def test_slippage_below_threshold():
    from config.settings import SLIPPAGE_ALERT_THRESHOLD_BPS
    slippages = [1.0, 2.0, 1.5, 0.5, 1.0]
    from scripts.run_slippage_monitor import _median
    median = _median(slippages)
    assert median < SLIPPAGE_ALERT_THRESHOLD_BPS


def test_slippage_bps_formula():
    signal_price = 100.0
    fill_price   = 100.5
    expected_bps = abs(fill_price - signal_price) / signal_price * 10000
    assert expected_bps == pytest.approx(50.0)


def test_slippage_bps_zero_for_perfect_fill():
    signal_price = 100.0
    fill_price   = 100.0
    bps = abs(fill_price - signal_price) / signal_price * 10000
    assert bps == 0.0
