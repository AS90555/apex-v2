"""
v7.2 Search-Space — versionierter Clone der Optuna-Ranges aus auto_lab_daemon.

RANGES_V72 ist eine 1:1-Kopie von OPTUNA_SPACES aus auto_lab_daemon.py (Stand 2026-05-13).
Eigenständige Datei ermöglicht unabhängige Versionierung ohne auto_lab_daemon zu berühren.

Bump RANGES_V72_VERSION bei jeder Range-Änderung → study_hash ändert sich → keine Hash-Kollision
mit alten Trials.
"""
from __future__ import annotations

import hashlib
import json

import optuna

RANGES_V72_VERSION = "1.0"

# Format: {strategy: {param: (low, high, is_int)}}
# Quelle: auto_lab_daemon.py OPTUNA_SPACES (Stand 2026-05-13)
RANGES_V72: dict[str, dict[str, tuple]] = {
    "kdt": {
        "SL_ATR_MULT": (0.5, 2.0, False),
        "TP_R":        (1.5, 5.0, False),
    },
    "weekend_momo": {
        "MOMENTUM_THRESHOLD": (0.01, 0.06, False),
        "ATR_SL_MULT":        (0.5,  2.5, False),
        "ATR_TP_MULT":        (1.5,  5.0, False),
    },
    "asian_fade": {
        "PUMP_THRESHOLD": (0.008, 0.03,  False),
        "RSI_OB":         (60,    80,    True),
        "RSI_OS":         (20,    40,    True),
        "SL_ATR_MULT":    (0.5,   2.0,  False),
        "TP_MULT":        (1.0,   3.0,  False),
    },
    "squeeze": {
        "SQUEEZE_PERIOD": (10,  30, True),
        "EMA_PERIOD":     (10,  40, True),
        "SL_ATR_MULT":    (0.5, 2.0, False),
        "TP_R":           (1.5, 5.0, False),
    },
    "mean_reversion": {
        "BB_PERIOD":  (10,  30, True),
        "BB_MULT":    (1.5, 3.0, False),
        "RSI_PERIOD": (7,   21, True),
        "RSI_OS":     (25,  45, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 4.0, False),
    },
    "vwap_bounce": {
        "VWAP_PERIOD": (12, 48, True),
        "VWAP_BAND":   (0.1, 0.5, False),
        "EMA_PERIOD":  (20,  80, True),
        "RSI_MIN":     (40,  60, False),
        "SL_ATR_MULT": (0.5, 2.0, False),
        "TP_R":        (1.5, 4.0, False),
    },
    "ema_pullback": {
        "EMA_SLOW":    (100, 200, True),
        "EMA_FAST":    (20,   75, True),
        "BODY_FACTOR": (0.1,  0.6, False),
        "SL_ATR_MULT": (0.5,  2.0, False),
        "TP_R":        (1.5,  5.0, False),
    },
    "donchian_breakout": {
        "DC_PERIOD":    (10,  50, True),
        "VOL_FACTOR":   (1.2, 3.0, False),
        "ATR_MIN_MULT": (0.8, 2.0, False),
        "SL_ATR_MULT":  (0.5, 1.5, False),
        "TP_R":         (1.2, 3.0, False),
    },
    "inside_bar_breakout": {
        "EMA_PERIOD":     (20, 100, True),
        "MOTHER_ATR_MIN": (0.3, 1.5, False),
        "SL_ATR_MULT":    (0.5, 2.0, False),
        "TP_R":           (1.5, 4.0, False),
    },
    "dual_donchian": {
        "ENTRY_PERIOD": (15,  60, True),
        "EXIT_PERIOD":  (5,   20, True),
        "VOL_FACTOR":   (1.2, 3.0, False),
        "ATR_MIN_MULT": (0.8, 2.0, False),
        "SL_ATR_MULT":  (0.5, 1.5, False),
        "TP_R":         (1.2, 3.0, False),
    },
    "bb_kc_squeeze": {
        "BB_PERIOD":  (10,  30, True),
        "BB_MULT":    (1.5, 3.0, False),
        "KC_MULT":    (1.0, 2.5, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 5.0, False),
    },
    "supertrend": {
        "ST1_PERIOD": (7,   14, True),
        "ST1_MULT":   (0.5, 2.0, False),
        "ST2_PERIOD": (10,  20, True),
        "ST2_MULT":   (1.5, 3.5, False),
        "ST3_PERIOD": (12,  25, True),
        "ST3_MULT":   (2.5, 5.0, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 5.0, False),
    },
    "orb": {
        "breakout_threshold_pct":  (0.0005, 0.005,  False),
        "min_box_range_pct":       (0.002,  0.015,  False),
        "max_box_age_bars":        (2,      12,     True),
        "volume_ratio_min":        (1.0,    3.0,    False),
        "max_breakout_dist_ratio": (1.0,    3.0,    False),
        "sl_buffer_pct":           (0.0005, 0.003,  False),
    },
    "vaa": {
        "VOL_MULT":   (1.5, 5.0, False),
        "BODY_MULT":  (0.3, 0.8, False),
        "ATR_EXPAND": (0.8, 2.5, False),
        "TP_R":       (1.5, 6.0, False),
    },
}


def suggest_v72(trial: optuna.Trial, strategy: str) -> dict:
    """Sampled Params aus RANGES_V72 für gegebene Strategie."""
    if strategy not in RANGES_V72:
        raise ValueError(f"Unbekannte Strategie für v7.2: {strategy!r}. Verfügbar: {sorted(RANGES_V72)}")
    params: dict = {}
    for key, (lo, hi, is_int) in RANGES_V72[strategy].items():
        if is_int:
            params[key] = trial.suggest_int(key, int(lo), int(hi))
        else:
            params[key] = round(trial.suggest_float(key, lo, hi), 4)
    return params


def ranges_v72_hash() -> str:
    """SHA256 über RANGES_V72 + RANGES_V72_VERSION (sorted JSON) — deterministisch."""
    payload = json.dumps(
        {"version": RANGES_V72_VERSION, "ranges": RANGES_V72},
        sort_keys=True,
        default=list,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
