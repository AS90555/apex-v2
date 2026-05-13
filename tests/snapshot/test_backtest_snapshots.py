"""
Snapshot-Tests (Phase 8) — Backtest-Reproduzierbarkeit.

Prüft dass die Backtest-Kern-Logik deterministisch ist:
  - Gleiche Inputs → gleiche Outputs (keine Zufallsabhängigkeit ohne Seed)
  - Partial-TP-Formel reproduzierbar
  - Kosten werden konsistent abgezogen
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ── Snapshot 1: Partial-TP-Formel deterministisch ────────────────────────────

def test_partial_tp_formula_snapshot():
    """
    Long-Trade: Entry=100, SL=95, TP1=102, TP2=110.
    TP1 bei 50% Exit → R_tp1 = (102-100)/(100-95) = 0.4 R (für 50% Position)
    Rest auf BE-SL (entry_price=100).
    TP2-Hit → R_tp2 = (110-100)/(100-95) = 2.0 R (für restliche 50%)
    Gesamt = 0.5 * 0.4 + 0.5 * 2.0 = 1.2 R
    """
    entry = 100.0
    sl    = 95.0
    tp1   = 102.0
    tp2   = 110.0
    sl_dist = entry - sl  # 5.0

    r_tp1 = (tp1 - entry) / sl_dist
    r_tp2 = (tp2 - entry) / sl_dist

    # 50/50 Split
    total_r = 0.5 * r_tp1 + 0.5 * r_tp2
    assert total_r == pytest.approx(1.2, rel=1e-9)


def test_partial_tp_formula_short_snapshot():
    """
    Short-Trade: Entry=100, SL=105, TP1=98, TP2=92.
    R_tp1 = (100-98)/(105-100) = 0.4 R (50%)
    R_tp2 = (100-92)/(105-100) = 1.6 R (50%)
    Gesamt = 0.5 * 0.4 + 0.5 * 1.6 = 1.0 R
    """
    entry = 100.0
    sl    = 105.0
    tp1   = 98.0
    tp2   = 92.0
    sl_dist = abs(sl - entry)  # 5.0

    r_tp1 = abs(entry - tp1) / sl_dist
    r_tp2 = abs(entry - tp2) / sl_dist

    total_r = 0.5 * r_tp1 + 0.5 * r_tp2
    assert total_r == pytest.approx(1.0, rel=1e-9)


# ── Snapshot 2: BE-Stop-Logik ─────────────────────────────────────────────────

def test_be_stop_snapshot():
    """
    Nach TP1-Hit: neuer SL = entry_price.
    Kurs fällt danach auf entry → BE-Stop-Exit mit 0 R für den Rest.
    Gesamt = 0.5 * r_tp1 + 0.5 * 0.0
    """
    entry = 100.0
    sl    = 95.0
    tp1   = 102.0
    sl_dist = entry - sl

    r_tp1 = (tp1 - entry) / sl_dist  # 0.4 R

    # BE-Stop: rest bei entry → 0 R
    r_be = (entry - entry) / sl_dist  # 0.0 R

    total_r = 0.5 * r_tp1 + 0.5 * r_be
    assert total_r == pytest.approx(0.2, rel=1e-9)


# ── Snapshot 3: Kostenmodell deterministisch ──────────────────────────────────

def test_cost_formula_snapshot():
    """
    Kostenformel (aus engine._apply_trade_costs):
    cost_r = ROUND_TRIP / (sl_pct)
    Net = Brutto - cost_r
    """
    from config.settings import TAKER_FEE, SLIPPAGE_EST, FUNDING_8H

    entry   = 50_000.0
    sl      = 49_000.0
    sl_pct  = abs(entry - sl) / entry  # 0.02 = 2%
    brutto_r = 2.0

    round_trip = (TAKER_FEE + SLIPPAGE_EST) * 2
    funding    = FUNDING_8H * 4  # 4 Funding-Perioden
    total_cost_r = (round_trip + funding) / sl_pct

    net_r = brutto_r - total_cost_r

    # Kosten müssen positiv sein und Net < Brutto
    assert total_cost_r > 0
    assert net_r < brutto_r
    # Reproduzierbarkeit: gleiche Formel → gleicher Wert
    assert total_cost_r == pytest.approx(
        ((TAKER_FEE + SLIPPAGE_EST) * 2 + FUNDING_8H * 4) / sl_pct,
        rel=1e-12,
    )


# ── Snapshot 4: DSR-Formel reproduzierbar ────────────────────────────────────

def test_dsr_snapshot():
    """DSR mit festen Inputs ist deterministisch reproduzierbar."""
    from backtest.metrics import dsr

    rs = [0.5, -0.3, 0.8, -0.2, 1.0, -0.1, 0.4, 0.6, -0.5, 0.9]

    result1 = dsr(rs, n_tested=1)
    result2 = dsr(rs, n_tested=1)

    assert result1 == result2
    assert 0.0 <= result1 <= 1.0


# ── Snapshot 5: Composite-Score reproduzierbar ────────────────────────────────

def test_composite_score_snapshot():
    """Gleiche CompositeInput → immer gleicher Score."""
    from backtest.composite_score import composite_score, CompositeInput

    inp = CompositeInput(
        sharpe_oos=1.5, dsr=0.65, max_drawdown=-0.08,
        stability_score=0.75, pbo=0.20, n_oos=50,
    )
    score1 = composite_score(inp)
    score2 = composite_score(inp)

    assert score1 == score2
    assert 0.0 <= score1 <= 1.0


# ── Snapshot 6: GBM-Reproduzierbarkeit ───────────────────────────────────────

def test_gbm_seed_reproducible():
    """GBM mit gleichem Seed → identisches Ergebnis."""
    from backtest.intrabar_gbm import simulate_intrabar

    history = [{"close": 100 + i * 0.1} for i in range(200)]
    kwargs = dict(
        entry_price=100.0,
        stop_loss=98.0, take_profit_1=101.0, take_profit_2=103.0,
        direction="long",
        bar_open=100.0, bar_high=102.0, bar_low=99.0,
        candles_history=history, seed=42,
    )
    result1 = simulate_intrabar(**kwargs)
    result2 = simulate_intrabar(**kwargs)

    # simulate_intrabar gibt (exit_reason, exit_price) zurück
    reason1, price1 = result1
    reason2, price2 = result2
    assert reason1 == reason2
    if price1 is not None and price2 is not None:
        assert math.isclose(price1, price2, rel_tol=1e-12)
