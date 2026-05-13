"""
Tests für v7 Phase 1 Warm-up Guard.

Verifiziert dass:
1. run_walk_forward den purge_bars-Default aus compute_max_lookback() zieht.
2. Der purge_bars-Wert für bekannte Strategien korrekt gesetzt wird.
3. Ein expliziter purge_bars-Override weiterhin funktioniert.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect
import pytest


def test_walk_forward_purge_default_is_none():
    """purge_bars Default in run_walk_forward ist None (wird dynamisch gesetzt)."""
    from backtest.walk_forward import run_walk_forward
    sig = inspect.signature(run_walk_forward)
    assert sig.parameters["purge_bars"].default is None, \
        "purge_bars Default muss None sein (dynamisch via compute_max_lookback)"


def test_compute_max_lookback_imported_in_walk_forward():
    """walk_forward.py muss compute_max_lookback importieren."""
    import backtest.walk_forward as wf_mod
    assert hasattr(wf_mod, "compute_max_lookback"), \
        "compute_max_lookback muss in walk_forward.py importiert sein"


def test_squeeze_lookback_sufficient():
    """squeeze nutzt EMA-200 → purge_bars muss ≥250 sein."""
    from features.registry import compute_max_lookback
    assert compute_max_lookback("squeeze") >= 250


def test_registry_covers_all_signal_fns():
    """Alle Strategien in SIGNAL_FNS müssen einen gültigen Lookback haben."""
    from features.registry import compute_max_lookback
    # Strategien aus backtest/engine.py SIGNAL_FNS
    strategies = [
        "vaa", "kdt", "weekend_momo", "asian_fade", "squeeze",
        "mean_reversion", "vwap_bounce", "ema_pullback",
        "donchian_breakout", "inside_bar_breakout", "dual_donchian",
        "bb_kc_squeeze", "supertrend", "orb",
    ]
    for s in strategies:
        lb = compute_max_lookback(s)
        assert lb >= 50, f"{s}: lookback {lb} zu gering"
        assert lb <= 500, f"{s}: lookback {lb} unrealistisch hoch"


def test_walk_forward_accepts_explicit_purge_bars():
    """run_walk_forward muss einen expliziten purge_bars-Override akzeptieren."""
    from backtest.walk_forward import run_walk_forward
    sig = inspect.signature(run_walk_forward)
    # Kein TypeError wenn purge_bars=300 übergeben wird
    # (Kein echter Run — nur Signatur-Check)
    params = sig.parameters
    assert "purge_bars" in params
    # Annotation erlaubt int | None
    annotation = params["purge_bars"].annotation
    assert annotation is not inspect.Parameter.empty


def test_reeval_imports_walk_forward():
    """run_legacy_reeval.py muss run_walk_forward importieren."""
    import scripts.run_legacy_reeval as reeval
    assert hasattr(reeval, "run_walk_forward"), \
        "run_legacy_reeval.py muss run_walk_forward importieren"


def test_reeval_does_not_import_run_backtest_directly():
    """run_legacy_reeval.py darf run_backtest nicht mehr direkt importieren."""
    # Source-Text prüfen statt Import (run_backtest ist in engine, nicht reeval)
    reeval_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "run_legacy_reeval.py",
    )
    with open(reeval_path) as f:
        source = f.read()
    assert "from backtest.engine import run_backtest" not in source, \
        "run_legacy_reeval.py darf run_backtest nicht mehr direkt importieren"
