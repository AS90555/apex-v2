"""Tests für LabSearchConfig (v7 Phase 3)."""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from research.lab_search_config import LabSearchConfig, LAB_SEARCH_CFG


def test_same_config_same_hash():
    """Zwei identische Instanzen → gleicher Hash."""
    cfg1 = LabSearchConfig()
    cfg2 = LabSearchConfig()
    assert cfg1.hash() == cfg2.hash()


def test_different_seed_different_hash():
    cfg_a = LabSearchConfig(tpe_seed=42)
    cfg_b = LabSearchConfig(tpe_seed=99)
    assert cfg_a.hash() != cfg_b.hash()


def test_different_pruner_different_hash():
    cfg_a = LabSearchConfig(pruner_type="MedianPruner")
    cfg_b = LabSearchConfig(pruner_type="HyperbandPruner")
    assert cfg_a.hash() != cfg_b.hash()


def test_different_version_different_hash():
    cfg_a = LabSearchConfig(version="v7.0")
    cfg_b = LabSearchConfig(version="v7.1")
    assert cfg_a.hash() != cfg_b.hash()


def test_hash_is_64_chars():
    """SHA256 → 64 Hex-Zeichen."""
    h = LabSearchConfig().hash()
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_short_hash_is_8_chars():
    assert len(LAB_SEARCH_CFG.short_hash()) == 8


def test_frozen_immutable():
    """LabSearchConfig ist frozen — Mutation muss fehlen."""
    cfg = LabSearchConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.tpe_seed = 999  # type: ignore[misc]


def test_build_sampler_returns_tpe():
    """build_sampler() erzeugt einen TPESampler."""
    try:
        import optuna
    except ImportError:
        pytest.skip("optuna nicht installiert")
    sampler = LAB_SEARCH_CFG.build_sampler()
    assert isinstance(sampler, optuna.samplers.TPESampler)


def test_build_pruner_returns_median():
    """build_pruner() erzeugt MedianPruner bei Standardkonfig."""
    try:
        import optuna
    except ImportError:
        pytest.skip("optuna nicht installiert")
    pruner = LAB_SEARCH_CFG.build_pruner()
    assert isinstance(pruner, optuna.pruners.MedianPruner)


def test_build_pruner_unknown_raises():
    cfg = LabSearchConfig(pruner_type="UnknownPruner")
    with pytest.raises(ValueError, match="Unbekannter Pruner-Typ"):
        cfg.build_pruner()


def test_lab_search_cfg_version_v7():
    assert LAB_SEARCH_CFG.version == "v7.0"
