"""Phase-4-Tests: Walk-Forward, PBO, Monte-Carlo, Composite-Score."""

from __future__ import annotations

import math
import random
import pytest

from backtest.metrics import sharpe, sortino, max_drawdown, calmar, dsr, pbo
from backtest.monte_carlo import run_monte_carlo
from backtest.composite_score import composite_score, CompositeInput


# ── Metriken ──────────────────────────────────────────────────────────────────

def test_sharpe_positive_series():
    rng = random.Random(1)
    rs  = [0.5 + rng.gauss(0, 0.1) for _ in range(50)]   # positiver Mittelwert + Varianz
    assert sharpe(rs) > 0


def test_sharpe_zero_std():
    rs = [1.0] * 20
    assert sharpe(rs) == 0.0  # keine Varianz → 0


def test_max_drawdown_known():
    rs = [1.0, 1.0, -3.0, 1.0]
    # Kum: 1, 2, -1 (dd=-3), 0 → MaxDD = -3
    assert max_drawdown(rs) == pytest.approx(-3.0, abs=1e-9)


def test_max_drawdown_no_loss():
    rs = [0.5, 0.5, 0.5]
    assert max_drawdown(rs) == 0.0


def test_calmar_positive():
    rs = [0.2] * 100 + [-1.0] * 5
    cal = calmar(rs)
    assert cal > 0


def test_dsr_random_series_low():
    """Zufällige Returns → DSR nahe 0.5 (kein Signal)."""
    rng = random.Random(99)
    rs  = [rng.gauss(0, 1) for _ in range(200)]
    d   = dsr(rs, n_tested=1)
    assert 0.0 <= d <= 1.0


def test_dsr_strong_positive_series():
    """Starke positive Returns → DSR nahe 1."""
    rs = [1.0] * 30 + [0.1] * 20
    d  = dsr(rs, n_tested=1)
    assert d > 0.5


def test_dsr_too_short():
    assert dsr([0.1, 0.2], n_tested=1) == 0.0


# ── PBO ───────────────────────────────────────────────────────────────────────

def test_pbo_range():
    rng = random.Random(42)
    is_rs  = [[rng.gauss(0, 1) for _ in range(50)] for _ in range(4)]
    oos_rs = [[rng.gauss(0, 1) for _ in range(50)] for _ in range(4)]
    p = pbo(is_rs, oos_rs)
    assert 0.0 <= p <= 1.0


def test_pbo_too_few_folds():
    assert pbo([[1.0]], [[1.0]]) == 0.5


# ── Monte-Carlo ───────────────────────────────────────────────────────────────

def test_monte_carlo_reproducible():
    rs = [0.3, -0.5, 0.8, -0.2, 1.0] * 20
    r1 = run_monte_carlo(rs, n_paths=100, seed=42)
    r2 = run_monte_carlo(rs, n_paths=100, seed=42)
    assert r1.median_total_r == r2.median_total_r


def test_monte_carlo_ruin_zero_for_winners():
    rs = [0.5] * 50
    r  = run_monte_carlo(rs, n_paths=200, seed=7)
    assert r.ruin_probability == 0.0


def test_monte_carlo_ruin_high_for_losers():
    rs = [-0.5] * 50
    r  = run_monte_carlo(rs, n_paths=200, seed=7)
    assert r.ruin_probability == 1.0


def test_monte_carlo_percentile_order():
    rs = [0.1, -0.3, 0.5] * 30
    r  = run_monte_carlo(rs, n_paths=200, seed=1)
    assert r.p5_total_r <= r.median_total_r <= r.p95_total_r


# ── Composite-Score ───────────────────────────────────────────────────────────

def test_composite_score_max():
    inp = CompositeInput(sharpe_oos=3.0, dsr=1.0, max_drawdown=0.0,
                         stability_score=1.0, pbo=0.0, n_oos=50)
    score = composite_score(inp)
    assert score > 0.8


def test_composite_score_min():
    inp = CompositeInput(sharpe_oos=-3.0, dsr=0.0, max_drawdown=-10.0,
                         stability_score=0.0, pbo=1.0, n_oos=50)
    score = composite_score(inp)
    assert score < 0.3


def test_composite_score_low_n():
    inp = CompositeInput(sharpe_oos=2.0, dsr=0.8, max_drawdown=-1.0,
                         stability_score=0.9, pbo=0.1, n_oos=5)
    assert composite_score(inp) == 0.0   # n_oos < 10 → 0


# ── Promotion-Gates v2 ────────────────────────────────────────────────────────

def test_promotion_gates_v6_blocked_without_flag(monkeypatch):
    """Ohne V6_GATES_ENFORCED: alte Gates gelten, v6-Checks inaktiv."""
    import config.settings as s
    monkeypatch.setattr(s, "V6_GATES_ENFORCED", False)
    from scripts.run_auto_promotion import _check_gates
    from unittest.mock import MagicMock

    disc = {
        "strategy": "squeeze", "asset": "BTC",
        "cost_model_applied": 1, "pf_test_netto": 1.5, "n_test": 150,
        "dsr": 0.55, "deployment_status": "lab",
        "dsr_value": 0.3, "pbo_value": 0.5, "stability_score": 0.2,
        "oos_folds_n": 0, "framework_version": "v1",
    }
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    passed, failed = _check_gates(disc, conn)
    assert passed, f"Ohne v6 enforced sollte es passen. Fehlgeschlagen: {failed}"


def test_promotion_gates_v6_enforced_fails_pbo(monkeypatch):
    """Mit V6_GATES_ENFORCED: PBO > 0.30 blockiert Promotion."""
    import config.settings as s
    monkeypatch.setattr(s, "V6_GATES_ENFORCED", True)
    from scripts.run_auto_promotion import _check_gates
    from unittest.mock import MagicMock

    disc = {
        "strategy": "squeeze", "asset": "ETH",
        "cost_model_applied": 1, "pf_test_netto": 1.5, "n_test": 150,
        "dsr": 0.65, "deployment_status": "lab",
        "dsr_value": 0.65, "pbo_value": 0.45,   # > PBO_MAX=0.30
        "stability_score": 0.8, "oos_folds_n": 2,
        "framework_version": "v6",
    }
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    passed, failed = _check_gates(disc, conn)
    assert not passed
    assert any("pbo" in f.lower() for f in failed)
