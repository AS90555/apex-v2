"""
Gate-Immutabilitäts-Tests für v7.2 (Phase 7).

Sichert, dass die Gate-Schwellen in config/settings.py unverändert bleiben.
Falls einer dieser Tests rot wird, wurde eine Gate-Konstante geändert —
das ist NICHT Teil von v7.2 und benötigt einen eigenen Plan + User-Freigabe.
"""
from __future__ import annotations

from config.settings import (
    DSR_MIN_DRY_RUN,
    DSR_MIN_LIVE,
    MAX_DD_GATE,
    OOS_FOLDS_MIN_V7,
    PBO_MAX,
    STABILITY_MIN,
)


def test_max_dd_gate_unchanged():
    assert MAX_DD_GATE == 5.0, f"MAX_DD_GATE wurde verändert: {MAX_DD_GATE} != 5.0"


def test_dsr_min_dry_run_unchanged():
    assert DSR_MIN_DRY_RUN == 0.50, f"DSR_MIN_DRY_RUN wurde verändert: {DSR_MIN_DRY_RUN} != 0.50"


def test_dsr_min_live_unchanged():
    assert DSR_MIN_LIVE == 0.65, f"DSR_MIN_LIVE wurde verändert: {DSR_MIN_LIVE} != 0.65"


def test_pbo_max_unchanged():
    assert PBO_MAX == 0.30, f"PBO_MAX wurde verändert: {PBO_MAX} != 0.30"


def test_stability_min_unchanged():
    assert STABILITY_MIN == 0.50, f"STABILITY_MIN wurde verändert: {STABILITY_MIN} != 0.50"


def test_oos_folds_min_v7_unchanged():
    assert OOS_FOLDS_MIN_V7 == 3, f"OOS_FOLDS_MIN_V7 wurde verändert: {OOS_FOLDS_MIN_V7} != 3"
