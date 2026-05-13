"""
Parameter-Stabilitäts-Checks (Phase 4 / v7 Phase 2).

Variiert jeden Parameter ±10/20/50 % und misst Sharpe-Standardabweichung.
stability_score = 1 - (std(Sharpes) / mean(|Sharpes|)).

v7 Ergänzung:
  run_stability() — führt alle Variationen automatisch via run_backtest aus
  und liefert direkt einen StabilityResult ohne manuell Backtests aufzurufen.
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


_VARIATION_FACTORS = (0.50, 0.80, 0.90, 1.10, 1.20, 1.50)


def run_stability(
    strategy:      str,
    asset:         str,
    start_ts:      int,
    end_ts:        int,
    base_cfg:      dict,
    cooldown_bars: int  = 8,
    apply_costs:   bool = True,
    max_params:    int  = 6,
) -> StabilityResult:
    """
    Führt alle Variationen automatisch durch und gibt StabilityResult zurück.

    Variiert jeden numerischen Parameter aus base_cfg mit _VARIATION_FACTORS.
    Begrenzt auf max_params Parameter (die mit dem größten Absolutwert).
    Bei Exceptions einer Variation: überspringen (statt Absturz).
    """
    from backtest.engine import run_backtest

    # Numerische Parameter auswählen (Ganzzahl oder Float, > 0)
    numeric_params = [
        k for k, v in base_cfg.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    ]
    # Nach absolutem Wert sortieren → relevanteste zuerst
    numeric_params = sorted(numeric_params, key=lambda k: abs(base_cfg[k]), reverse=True)
    numeric_params = numeric_params[:max_params]

    base_bt = run_backtest(
        strategy=strategy, asset=asset, start_ts=start_ts, end_ts=end_ts,
        cfg=base_cfg, cooldown_bars=cooldown_bars, apply_costs=apply_costs,
    )
    base_pnl_rs = [t.pnl_r for t in base_bt.trades]

    variations: dict[str, list[list[float]]] = {}
    for param in numeric_params:
        param_rs_list: list[list[float]] = []
        for factor in _VARIATION_FACTORS:
            varied = vary_cfg(base_cfg, param, factor)
            try:
                bt = run_backtest(
                    strategy=strategy, asset=asset, start_ts=start_ts, end_ts=end_ts,
                    cfg=varied, cooldown_bars=cooldown_bars, apply_costs=apply_costs,
                )
                rs = [t.pnl_r for t in bt.trades]
                param_rs_list.append(rs if rs else [0.0])
            except Exception:
                param_rs_list.append([0.0])
        variations[param] = param_rs_list

    return compute_stability(base_pnl_rs, variations)
