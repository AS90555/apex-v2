"""
n_tested-Semantik-Tests für v7.2 (Phase 7).

Sichert, dass n_tested_hint in objective_v72 korrekt als Optuna-Trial-Anzahl
der laufenden Study (= args.n_trials) weitergereicht wird — NICHT als
len(SIGNAL_FNS), Promotion-Menge oder andere Zahl.

Schützt gegen: n_tested=14 hardcoded (v7-Default), n_tested=Promotion-Menge.
"""
from __future__ import annotations

import time
from unittest.mock import call, patch, MagicMock

import optuna
import pytest

optuna.logging.set_verbosity(optuna.logging.WARNING)

from backtest.v7_eval import V7EvalResult
from backtest.engine import SIGNAL_FNS
from research.v72_objective import objective_v72


def _make_eval(**kw) -> V7EvalResult:
    defaults = dict(
        strategy="donchian_breakout", asset="BTC", params_json="{}",
        dsr_oos=0.8, pbo_val=0.1, stability=0.7, max_dd=2.0,
        composite=0.65, weights_hash="abc", n_oos=200,
        oos_folds_n=18, passed=True, fail_reasons=[],
    )
    defaults.update(kw)
    return V7EvalResult(**defaults)


def test_n_tested_hint_passed_to_evaluate_v7():
    """objective_v72 reicht n_tested_hint=80 korrekt an evaluate_v7 weiter."""
    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    with patch("research.v72_objective.evaluate_v7", return_value=_make_eval()) as mock_eval, \
         patch("research.v72_objective.suggest_v72", return_value={}):
        objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=80)

    _, kwargs = mock_eval.call_args
    assert kwargs.get("n_tested") == 80, \
        f"n_tested sollte 80 sein (= args.n_trials), aber war: {kwargs.get('n_tested')}"


def test_n_tested_is_not_signal_fns_default():
    """n_tested_hint darf NICHT mit len(SIGNAL_FNS)=14 hardcoded sein."""
    signal_fns_count = len(SIGNAL_FNS)

    study = optuna.create_study(direction="maximize")
    trial = study.ask()

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    # Wir übergeben einen Wert der NICHT len(SIGNAL_FNS) ist
    custom_n = signal_fns_count + 100  # z.B. 114

    with patch("research.v72_objective.evaluate_v7", return_value=_make_eval()) as mock_eval, \
         patch("research.v72_objective.suggest_v72", return_value={}):
        objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=custom_n)

    _, kwargs = mock_eval.call_args
    actual_n = kwargs.get("n_tested")
    assert actual_n == custom_n, \
        f"n_tested sollte {custom_n} sein, aber war {actual_n}. " \
        f"Prüfe: wurde len(SIGNAL_FNS)={signal_fns_count} hardcoded?"


def test_n_tested_varies_with_hint():
    """Verschiedene n_tested_hint-Werte → verschiedene n_tested in evaluate_v7."""
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 730 * 24 * 3600 * 1000

    collected_n = []

    for hint in [10, 50, 200]:
        study = optuna.create_study(direction="maximize")
        trial = study.ask()
        with patch("research.v72_objective.evaluate_v7", return_value=_make_eval()) as mock_eval, \
             patch("research.v72_objective.suggest_v72", return_value={}):
            objective_v72(trial, "donchian_breakout", "BTC", start_ts, end_ts, n_tested_hint=hint)
        _, kwargs = mock_eval.call_args
        collected_n.append(kwargs.get("n_tested"))

    assert collected_n == [10, 50, 200], f"n_tested sollte exakt dem Hint folgen, war: {collected_n}"
