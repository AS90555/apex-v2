"""Tests für Composite-Score-Versionierung (v7 Phase 3)."""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from backtest.composite_score import (
    composite_score, composite_score_with_hash, weights_hash, CompositeInput,
)
from config.settings import COMPOSITE_WEIGHTS, COMPOSITE_WEIGHTS_VERSION


def _good_input(n: int = 50) -> CompositeInput:
    return CompositeInput(
        sharpe_oos=1.5, dsr=0.7, max_drawdown=-2.0,
        stability_score=0.8, pbo=0.2, n_oos=n,
    )


def test_composite_score_unchanged():
    """composite_score() funktioniert wie bisher (keine Regression)."""
    score = composite_score(_good_input())
    assert 0.0 < score <= 1.0


def test_composite_score_with_hash_returns_tuple():
    result = composite_score_with_hash(_good_input())
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_composite_score_with_hash_score_matches():
    """Score in Tuple muss mit composite_score() übereinstimmen."""
    inp = _good_input()
    score_direct = composite_score(inp)
    score_hashed, _ = composite_score_with_hash(inp)
    assert score_direct == score_hashed


def test_weights_hash_deterministic():
    """Gleiche Gewichte + Version → gleicher Hash."""
    h1 = weights_hash(COMPOSITE_WEIGHTS, COMPOSITE_WEIGHTS_VERSION)
    h2 = weights_hash(COMPOSITE_WEIGHTS, COMPOSITE_WEIGHTS_VERSION)
    assert h1 == h2


def test_weights_hash_changes_on_version():
    h_v7 = weights_hash(COMPOSITE_WEIGHTS, "v7.0")
    h_v8 = weights_hash(COMPOSITE_WEIGHTS, "v8.0")
    assert h_v7 != h_v8


def test_weights_hash_changes_on_weight_change():
    w1 = dict(COMPOSITE_WEIGHTS)
    w2 = dict(COMPOSITE_WEIGHTS)
    w2["sharpe"] = 0.99
    h1 = weights_hash(w1, COMPOSITE_WEIGHTS_VERSION)
    h2 = weights_hash(w2, COMPOSITE_WEIGHTS_VERSION)
    assert h1 != h2


def test_composite_weights_version_is_v7():
    assert COMPOSITE_WEIGHTS_VERSION == "v7.0"


def test_hash_length():
    h = weights_hash(COMPOSITE_WEIGHTS, COMPOSITE_WEIGHTS_VERSION)
    assert len(h) == 16  # gekürzt auf 16 Zeichen


def test_composite_score_low_n_returns_zero():
    inp = CompositeInput(sharpe_oos=2.0, dsr=0.9, max_drawdown=-1.0,
                         stability_score=1.0, pbo=0.0, n_oos=5)
    assert composite_score(inp) == 0.0
    score, _ = composite_score_with_hash(inp)
    assert score == 0.0
