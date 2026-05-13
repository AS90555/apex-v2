"""
Reproduzierbarkeits-Tests für v7.2 (Phase 7).

Sichert, dass:
1. Gleiche Seed + Config → gleiche Param-Sequenz in Optuna
2. study_hash ändert sich bei RANGES_V72_VERSION- oder OBJECTIVE_V72_VERSION-Bump
"""
from __future__ import annotations

import optuna
import pytest

optuna.logging.set_verbosity(optuna.logging.WARNING)

from research.lab_search_config import LAB_SEARCH_CFG
from research.v72_objective import compute_study_hash
from research.v72_search_space import suggest_v72, RANGES_V72_VERSION, ranges_v72_hash
from config.settings import OBJECTIVE_V72_VERSION


def _run_study(strategy: str, n: int = 5) -> list[dict]:
    """Führt n Trials durch und gibt die Param-Sequenz zurück."""
    study = optuna.create_study(
        direction="maximize",
        sampler=LAB_SEARCH_CFG.build_sampler(),
    )
    params_seq = []
    for _ in range(n):
        trial = study.ask()
        params = suggest_v72(trial, strategy)
        params_seq.append(params)
        study.tell(trial, 0.5)
    return params_seq


def test_same_seed_same_params():
    """Zwei identische Study-Läufe → identische Param-Sequenz."""
    seq1 = _run_study("donchian_breakout", n=5)
    seq2 = _run_study("donchian_breakout", n=5)
    assert seq1 == seq2, "Param-Sequenz ist nicht reproduzierbar!"


def test_study_hash_deterministic():
    h1 = compute_study_hash("donchian_breakout", "BTC")
    h2 = compute_study_hash("donchian_breakout", "BTC")
    assert h1 == h2


def test_study_hash_changes_on_ranges_bump(monkeypatch):
    import research.v72_search_space as ss
    h1 = compute_study_hash("donchian_breakout", "BTC")
    monkeypatch.setattr(ss, "RANGES_V72_VERSION", "99.0")
    h2 = compute_study_hash("donchian_breakout", "BTC")
    assert h1 != h2


def test_study_hash_changes_on_objective_bump(monkeypatch):
    import research.v72_objective as obj
    h1 = compute_study_hash("donchian_breakout", "BTC")
    monkeypatch.setattr(obj, "OBJECTIVE_V72_VERSION", "v72.99")
    h2 = compute_study_hash("donchian_breakout", "BTC")
    assert h1 != h2
