"""
v7.2 OOS-Objective für Optuna (Phase 3).

Ruft evaluate_v7() pro Trial auf und maximiert den Composite-Score.
PBO > PBO_MAX → Hard-Filter: return 0.0 (Overfit-Kandidaten verbrennen keine weiteren Zyklen).

n_tested_hint: Anzahl Optuna-Trials der laufenden Study (= args.n_trials aus run_v72_research.py).
  NICHT die spätere Promotion-/Takeover-Menge, NICHT len(SIGNAL_FNS).
  Dieser Wert steuert die DSR-Multiple-Testing-Korrektur in bootstrap_dsr().
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import optuna

from backtest.v7_eval import evaluate_v7, V7EvalResult
from config.settings import PBO_MAX, OBJECTIVE_V72_VERSION
from research.lab_search_config import LAB_SEARCH_CFG
from research.v72_search_space import RANGES_V72_VERSION, ranges_v72_hash, suggest_v72


@dataclass
class V72TrialResult:
    params: dict
    eval_result: V7EvalResult
    study_hash: str
    objective_version: str
    pruned: bool  # True wenn PBO-Hard-Filter ausgelöst hat


def compute_study_hash(strategy: str, asset: str) -> str:
    """
    SHA256 über (LAB_SEARCH_CFG.hash, RANGES_V72_VERSION, ranges_v72_hash,
                 OBJECTIVE_V72_VERSION, strategy, asset)[:32].
    Eindeutige ID der Optuna-Studie. Ändert sich bei:
    - Änderung der Sampler/Pruner-Config (LAB_SEARCH_CFG)
    - Änderung der Search-Space-Ranges (RANGES_V72_VERSION / ranges_v72_hash)
    - Änderung der Objective-Funktion (OBJECTIVE_V72_VERSION)
    - Anderen strategy/asset
    """
    payload = json.dumps(
        {
            "lab_config_hash":     LAB_SEARCH_CFG.hash(),
            "ranges_v72_version":  RANGES_V72_VERSION,
            "ranges_v72_hash":     ranges_v72_hash(),
            "objective_version":   OBJECTIVE_V72_VERSION,
            "strategy":            strategy,
            "asset":               asset,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def objective_v72(
    trial: optuna.Trial,
    strategy: str,
    asset: str,
    start_ts: int,
    end_ts: int,
    n_tested_hint: int,
) -> float:
    """
    Optuna-Objective für v7.2.

    Maximiert evaluate_v7().composite — außer PBO > PBO_MAX → return 0.0.
    n_tested_hint = Anzahl Optuna-Trials dieser Study (args.n_trials aus run_v72_research.py),
    NICHT die spätere Promotion-Menge oder len(SIGNAL_FNS).

    Speichert V7EvalResult in trial.user_attrs["eval_result"] für Reporting.
    Setzt trial.user_attrs["pruned_pbo"] = True wenn PBO-Hard-Filter greift.
    """
    params = suggest_v72(trial, strategy)
    ev = evaluate_v7(strategy, asset, params, start_ts, end_ts, n_tested=n_tested_hint)
    trial.set_user_attr("eval_result", _eval_to_dict(ev))

    if ev.pbo_val > PBO_MAX:
        trial.set_user_attr("pruned_pbo", True)
        return 0.0

    return ev.composite


def _eval_to_dict(ev: V7EvalResult) -> dict:
    """Konvertiert V7EvalResult zu serialisierbarem Dict für trial.user_attrs."""
    return {
        "strategy":     ev.strategy,
        "asset":        ev.asset,
        "dsr_oos":      ev.dsr_oos,
        "pbo_val":      ev.pbo_val,
        "stability":    ev.stability,
        "max_dd":       ev.max_dd,
        "composite":    ev.composite,
        "weights_hash": ev.weights_hash,
        "n_oos":        ev.n_oos,
        "oos_folds_n":  ev.oos_folds_n,
        "passed":       ev.passed,
        "fail_reasons": ev.fail_reasons,
    }
