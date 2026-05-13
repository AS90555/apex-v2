"""
Tests für research/v72_search_space.py (Phase 2).
"""
from __future__ import annotations

import pytest
import optuna

optuna.logging.set_verbosity(optuna.logging.WARNING)

from backtest.engine import SIGNAL_FNS
from research.v72_search_space import (
    RANGES_V72,
    RANGES_V72_VERSION,
    ranges_v72_hash,
    suggest_v72,
)


def test_ranges_v72_hash_deterministic():
    assert ranges_v72_hash() == ranges_v72_hash()


def test_ranges_v72_hash_is_sha256():
    h = ranges_v72_hash()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_all_signal_fns_covered():
    missing = set(SIGNAL_FNS.keys()) - set(RANGES_V72.keys())
    assert not missing, f"Strategien ohne v7.2-Range: {missing}"


def test_suggest_v72_squeeze():
    study = optuna.create_study()
    trial = study.ask()
    params = suggest_v72(trial, "squeeze")
    assert set(params.keys()) == {"SQUEEZE_PERIOD", "EMA_PERIOD", "SL_ATR_MULT", "TP_R"}
    assert isinstance(params["SQUEEZE_PERIOD"], int)
    assert isinstance(params["TP_R"], float)


def test_suggest_v72_all_strategies():
    for strategy in RANGES_V72:
        study = optuna.create_study()
        trial = study.ask()
        params = suggest_v72(trial, strategy)
        assert isinstance(params, dict)
        assert len(params) > 0


def test_suggest_v72_unknown_strategy():
    study = optuna.create_study()
    trial = study.ask()
    with pytest.raises(ValueError, match="Unbekannte Strategie"):
        suggest_v72(trial, "nonexistent_strat")


def test_version_bump_changes_hash(monkeypatch):
    import research.v72_search_space as ss
    h1 = ranges_v72_hash()
    monkeypatch.setattr(ss, "RANGES_V72_VERSION", "9.9")
    h2 = ss.ranges_v72_hash()
    assert h1 != h2
