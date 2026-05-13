"""
Parameter-Stabilitäts-Checks (Phase 4).

Variiert jeden Parameter ±10/20/50 % und misst Sharpe-Standardabweichung.
stability_score = 1 - (std(Sharpes) / mean(|Sharpes|)).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backtest.metrics import sharpe as calc_sharpe, _std, _mean


@dataclass
class StabilityResult:
    stability_score: float   # [0, 1] — höher = stabiler
    param_sensitivities: dict[str, float]   # param → Sharpe-Std über Variationen


def compute_stability(
    base_pnl_rs:   list[float],
    variations:    dict[str, list[list[float]]],
    # Key: param_name, Value: Liste von pnl_rs pro Variation
) -> StabilityResult:
    """
    Berechnet Stabilitäts-Score aus vorberechneten Variation-Returns.

    variations: {param_name: [pnl_rs_minus50, pnl_rs_minus20, pnl_rs_minus10,
                               pnl_rs_base, pnl_rs_plus10, pnl_rs_plus20, pnl_rs_plus50]}
    """
    all_sharpes: list[float] = [calc_sharpe(base_pnl_rs)]
    sensitivities: dict[str, float] = {}

    for param, rs_list in variations.items():
        param_sharpes = [calc_sharpe(rs) for rs in rs_list]
        all_sharpes.extend(param_sharpes)
        sensitivities[param] = round(_std(param_sharpes, ddof=1), 4)

    mean_abs = _mean([abs(s) for s in all_sharpes])
    std_s    = _std(all_sharpes, ddof=1)

    score = max(0.0, 1.0 - std_s / mean_abs) if mean_abs > 0 else 0.0
    return StabilityResult(
        stability_score      = round(score, 4),
        param_sensitivities  = sensitivities,
    )


def vary_cfg(base_cfg: dict, param: str, factor: float) -> dict:
    """Gibt neues cfg zurück mit param × factor."""
    new_cfg = dict(base_cfg)
    if param in new_cfg and isinstance(new_cfg[param], (int, float)):
        new_cfg[param] = type(new_cfg[param])(new_cfg[param] * factor)
    return new_cfg
