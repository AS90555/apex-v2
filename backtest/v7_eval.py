"""
Gemeinsame v7/v7.1-Evaluierungsfunktion (Phase 2 v7.1).

Extrahiert die WalkForward→DSR→Stability→PBO→Composite-Pipeline aus
scripts/run_v7_reeval.py, damit v7-Default-Reeval und v7.1-Übernahme
identische Bewertungslogik verwenden.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backtest.walk_forward import run_walk_forward
from backtest.monte_carlo import bootstrap_dsr
from backtest.stability import run_stability
from backtest.composite_score import composite_score_with_hash, CompositeInput
from backtest.metrics import pbo
from backtest.engine import SIGNAL_FNS
from config.settings import (
    DSR_MIN_DRY_RUN, PBO_MAX, STABILITY_MIN, MAX_DD_GATE, OOS_FOLDS_MIN_V7,
)
from core.utils import log

REEVAL_IS_BARS  = 4320   # ~6 Monate 1h
REEVAL_OOS_BARS = 720    # ~1 Monat 1h
REEVAL_STEP     = 720


@dataclass
class V7EvalResult:
    strategy:     str
    asset:        str
    params_json:  str
    dsr_oos:      float
    pbo_val:      float
    stability:    float
    max_dd:       float
    composite:    float
    weights_hash: str
    n_oos:        int
    oos_folds_n:  int
    passed:       bool
    fail_reasons: list[str] = field(default_factory=list)


def evaluate_v7(
    strategy: str,
    asset: str,
    params: dict,
    start_ts: int,
    end_ts: int,
    is_bars: int = REEVAL_IS_BARS,
    oos_bars: int = REEVAL_OOS_BARS,
    step_bars: int = REEVAL_STEP,
    n_tested: int | None = None,
) -> V7EvalResult:
    """
    Führt die vollständige v7-Bewertungspipeline für eine Strategie/Asset-Kombination
    mit den übergebenen Parametern durch. Kein DB-Write.

    n_tested: Anzahl getesteter Konfigurationen für DSR Multiple-Testing-Korrektur.
      None  → len(SIGNAL_FNS) (v7-Default: 14 Strategien)
      int>0 → explizit übergeben (v7.1: Anzahl Optuna-Trials dieser strategy/asset)

    Reihenfolge: WalkForward → MC-Bootstrap-DSR → CSCV-PBO → Stability
                 → Composite + weights_hash → Gate-Check
    """
    import json

    params_json = json.dumps(params)

    try:
        wf = run_walk_forward(
            strategy=strategy,
            asset=asset,
            start_ts=start_ts,
            end_ts=end_ts,
            cfg=params,
            is_bars=is_bars,
            oos_bars=oos_bars,
            step_bars=step_bars,
        )
    except Exception as e:
        log(f"[v7_eval] WalkForward-Fehler {strategy}/{asset}: {e}")
        return V7EvalResult(
            strategy=strategy, asset=asset, params_json=params_json,
            dsr_oos=0.0, pbo_val=1.0, stability=0.0, max_dd=0.0,
            composite=0.0, weights_hash="", n_oos=0, oos_folds_n=0,
            passed=False, fail_reasons=[f"WalkForward-Fehler: {e}"],
        )

    oos_pnl = wf.all_oos_pnl_rs
    n_oos   = len(oos_pnl)

    _n_tested = n_tested if n_tested is not None else len(SIGNAL_FNS)
    dsr_med, _ = bootstrap_dsr(oos_pnl, n_tested=_n_tested)

    pbo_insufficient = False
    if wf.n_folds >= OOS_FOLDS_MIN_V7:
        fold_oos_rets = [getattr(f, "_oos_pnl_rs", []) for f in wf.folds]
        fold_is_rets  = [getattr(f, "_is_pnl_rs",  []) for f in wf.folds]
        valid = [(a, b) for a, b in zip(fold_is_rets, fold_oos_rets) if a and b]
        if len(valid) >= OOS_FOLDS_MIN_V7:
            pbo_val = pbo([v[0] for v in valid], [v[1] for v in valid])
        else:
            pbo_val = 0.5
            pbo_insufficient = True  # Folds vorhanden, aber ohne Trades → PBO nicht berechenbar
    else:
        pbo_val = 0.5
        pbo_insufficient = True

    try:
        stab_result = run_stability(strategy, asset, start_ts, end_ts, params)
        stab_score  = stab_result.stability_score
    except Exception as e:
        log(f"[v7_eval] Stability-Fehler {strategy}/{asset}: {e}")
        stab_score = 0.0

    max_dd = abs(wf.worst_max_dd)

    inp = CompositeInput(
        sharpe_oos=wf.mean_sharpe_oos,
        dsr=dsr_med,
        max_drawdown=wf.worst_max_dd,
        stability_score=stab_score,
        pbo=pbo_val,
        n_oos=n_oos,
    )
    comp_score, w_hash = composite_score_with_hash(inp)

    fail_reasons: list[str] = []
    if dsr_med < DSR_MIN_DRY_RUN:
        fail_reasons.append(f"DSR={dsr_med:.3f} < {DSR_MIN_DRY_RUN}")
    if pbo_insufficient:
        fail_reasons.append(
            f"pbo_insufficient_folds: n_folds={wf.n_folds} < {OOS_FOLDS_MIN_V7} "
            f"oder keine Trade-Daten in Folds — PBO nicht berechenbar"
        )
    elif pbo_val > PBO_MAX:
        fail_reasons.append(f"PBO={pbo_val:.3f} > {PBO_MAX}")
    if stab_score < STABILITY_MIN:
        fail_reasons.append(f"Stability={stab_score:.3f} < {STABILITY_MIN}")
    if max_dd > MAX_DD_GATE:
        fail_reasons.append(f"MaxDD={max_dd:.3f} > {MAX_DD_GATE}")
    if n_oos < 100:
        fail_reasons.append(f"n_oos={n_oos} < 100")
    if wf.n_folds < OOS_FOLDS_MIN_V7:
        fail_reasons.append(f"oos_folds_n={wf.n_folds} < {OOS_FOLDS_MIN_V7}")

    return V7EvalResult(
        strategy=strategy,
        asset=asset,
        params_json=params_json,
        dsr_oos=round(dsr_med, 4),
        pbo_val=round(pbo_val, 4),
        stability=round(stab_score, 4),
        max_dd=round(max_dd, 4),
        composite=round(comp_score, 4),
        weights_hash=w_hash,
        n_oos=n_oos,
        oos_folds_n=wf.n_folds,
        passed=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )
