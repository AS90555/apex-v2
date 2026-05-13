"""
Tests für research/v72_objective.py (Phase 3).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import optuna
import pytest

optuna.logging.set_verbosity(optuna.logging.WARNING)

from research.v72_objective import compute_study_hash, objective_v72
from backtest.v7_eval import V7EvalResult
from config.settings import PBO_MAX


def _make_eval_result(**overrides) -> V7EvalResult:
    defaults = dict(
        strategy="donchian_breakout",
        asset="BTC",
        params_json="{}",
        dsr_oos=0.8,
        pbo_val=0.1,
        stability=0.7,
        max_dd=2.0,
        composite=0.75,
        weights_hash="abc123",
        n_oos=200,
        oos_folds_n=18,
        passed=True,
        fail_reasons=[],
    )
    defaults.update(overrides)
    return V7EvalResult(**defaults)


def test_compute_study_hash_deterministic():
    h1 = compute_study_hash("donchian_breakout", "BTC")
    h2 = compute_study_hash("donchian_breakout", "BTC")
    assert h1 == h2


def test_compute_study_hash_strategy_dependent():
    h1 = compute_study_hash("donchian_breakout", "BTC")
    h2 = compute_study_hash("squeeze", "BTC")
    assert h1 != h2


def test_compute_study_hash_asset_dependent():
    h1 = compute_study_hash("donchian_breakout", "BTC")
    h2 = compute_study_hash("donchian_breakout", "ETH")
    assert h1 != h2


def test_compute_study_hash_length():
    h = compute_study_hash("donchian_breakout", "BTC")
    assert len(h) == 32


def test_objective_v72_pbo_hard_filter():
    """PBO > PBO_MAX → Objective soll 0.0 zurückgeben und pruned_pbo setzen."""
    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    ev_overfit = _make_eval_result(pbo_val=PBO_MAX + 0.1, composite=0.8, passed=False,
                                   fail_reasons=[f"PBO={PBO_MAX + 0.1:.3f} > {PBO_MAX}"])

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    with patch("research.v72_objective.evaluate_v7", return_value=ev_overfit), \
         patch("research.v72_objective.suggest_v72", return_value={"DC_PERIOD": 20}):
        score = objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=50)

    assert score == 0.0
    assert trial.user_attrs.get("pruned_pbo") is True


def test_objective_v72_passes_composite():
    """PBO ≤ PBO_MAX → Composite-Score wird zurückgegeben."""
    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    ev_good = _make_eval_result(pbo_val=0.1, composite=0.75, passed=True)

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    with patch("research.v72_objective.evaluate_v7", return_value=ev_good), \
         patch("research.v72_objective.suggest_v72", return_value={"DC_PERIOD": 20}):
        score = objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=50)

    assert score == pytest.approx(0.75)
    assert "eval_result" in trial.user_attrs
    assert trial.user_attrs.get("pruned_pbo") is None


def test_objective_v72_eval_result_attrs():
    """trial.user_attrs['eval_result'] wird immer befüllt."""
    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    ev = _make_eval_result(dsr_oos=0.9, pbo_val=0.05, composite=0.6)

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    with patch("research.v72_objective.evaluate_v7", return_value=ev), \
         patch("research.v72_objective.suggest_v72", return_value={}):
        objective_v72(trial, "squeeze", "ETH", start_ts, end_ts, n_tested_hint=30)

    er = trial.user_attrs["eval_result"]
    assert er["dsr_oos"] == pytest.approx(0.9)
    assert er["pbo_val"] == pytest.approx(0.05)
    assert er["composite"] == pytest.approx(0.6)


def test_objective_v72_uses_n_tested_hint():
    """n_tested_hint wird korrekt an evaluate_v7 weitergereicht."""
    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    ev = _make_eval_result()
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    with patch("research.v72_objective.evaluate_v7", return_value=ev) as mock_eval, \
         patch("research.v72_objective.suggest_v72", return_value={}):
        objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=80)

    call_kwargs = mock_eval.call_args
    assert call_kwargs.kwargs.get("n_tested") == 80 or call_kwargs.args[-1] == 80
