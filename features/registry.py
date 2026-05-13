"""
Feature Registry — v7 Phase 1

Mapping von Strategie → maximal benötigter Feature-Lookback in Bars.
Wird von WalkForwardEngine als dynamischer purge_bars-Default genutzt
(statt hardcoded 200), sodass kein Feature seinen Lookback-Warm-up ins OOS
überschreitet (Zero-Leakage-Guarantee).

Logik:
  - Jedes Feature in FEATURE_DEFS hat `min_candles` (z.B. EMA-200 → 210).
  - compute_max_lookback() gibt max(min_candles) aller Features einer Strategie
    zurück, plus einen 20%-Sicherheitspuffer.
  - Strategien ohne spezifisches Override erhalten den globalen Max-Lookback.
"""

from __future__ import annotations

import math

# min_candles aus features/feature_agent.py:FEATURE_DEFS
_GLOBAL_MAX = max([210, 210, 60, 60, 60, 25, 15, 36, 21, 51, 51, 60, 15, 21])  # = 210

# Strategie-spezifische Overrides (wenn < Global-Max ausreicht)
_STRATEGY_LOOKBACK: dict[str, int] = {
    "orb":              210,   # ema_200_15m + ema_200_4h-äquivalent
    "squeeze":          210,   # ema_200_1h
    "mean_reversion":   210,
    "ema_pullback":     210,
    "vaa":              210,
    "kdt":              210,
    "donchian_breakout": 60,   # kein EMA-200 genutzt
    "dual_donchian":    60,
    "inside_bar_breakout": 55, # body_sma_50 + vol_sma_50 → 51
    "bb_kc_squeeze":    60,
    "supertrend":       60,
    "vwap_bounce":      55,
    "asian_fade":       25,    # ema_20_1h
    "weekend_momo":     25,
}

_SAFETY_FACTOR = 1.20  # 20% Puffer


def compute_max_lookback(strategy: str) -> int:
    """
    Gibt die empfohlene Anzahl Bars für purge_bars einer WalkForward-Engine zurück.

    Der Wert ist strategy-spezifisch und enthält einen 20%-Sicherheitspuffer,
    damit kein Indikator mit unvollständiger History in einen OOS-Fold eintritt.
    Mindestens 50 Bars.
    """
    base = _STRATEGY_LOOKBACK.get(strategy, _GLOBAL_MAX)
    return max(50, math.ceil(base * _SAFETY_FACTOR))


def all_strategy_lookbacks() -> dict[str, int]:
    """Gibt lookup-Table aller bekannten Strategien mit empfohlenem Lookback zurück."""
    strategies = set(_STRATEGY_LOOKBACK.keys())
    return {s: compute_max_lookback(s) for s in strategies}
