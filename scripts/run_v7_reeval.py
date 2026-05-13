"""
v7-Re-Evaluation aller 14 SIGNAL_FNS-Strategien (Phase 6).

Bewertet 14 Strategien × 7 LIVE_ASSETS = 98 Kombinationen unter dem v7-Framework:
  WalkForward (Phase 1) → MC-Bootstrap-DSR (Phase 2) → Stability (Phase 2)
  → Composite mit weights_hash (Phase 3)

Schreibt Ergebnisse als neue lab_discoveries-Zeilen mit framework_version='v7'.
Idempotent via params_hash (Unique-Index idx_lab_disc_idempotent).
Gate-Check: DSR ≥ DSR_MIN_DRY_RUN, PBO ≤ PBO_MAX, Stability ≥ STABILITY_MIN,
            MaxDD ≤ MAX_DD_GATE, n_oos ≥ 100, oos_folds_n ≥ OOS_FOLDS_MIN_V7.

Report: research/findings/v7_reeval/{date}/summary.md
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import SIGNAL_FNS
from backtest.walk_forward import run_walk_forward
from backtest.monte_carlo import bootstrap_dsr
from backtest.stability import run_stability
from backtest.composite_score import composite_score_with_hash, CompositeInput
from backtest.metrics import pbo
from config.settings import (
    LIVE_ASSETS,
    DSR_MIN_DRY_RUN, PBO_MAX, STABILITY_MIN, MAX_DD_GATE, OOS_FOLDS_MIN_V7,
)
from core.db import get_connection
from core.utils import log
from research.lab_search_config import LAB_SEARCH_CFG

# Zeitraum: 2 Jahre zurück ab jetzt
REEVAL_DAYS     = 730
REEVAL_IS_BARS  = 4320  # ~6 Monate 1h
REEVAL_OOS_BARS = 720   # ~1 Monat 1h
REEVAL_STEP     = 720


@dataclass
class ReevalResult:
    strategy:     str
    asset:        str
    dsr_oos:      float
    pbo_val:      float
    stability:    float
    max_dd:       float
    composite:    float
    weights_hash: str
    n_oos:        int
    oos_folds_n:  int
    passed:       bool
    fail_reasons: list[str]
    params_json:  str
    discovery_id: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_params_hash(strategy: str, asset: str) -> str:
    key = f"v7_reeval__{strategy}__{asset}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _default_params(strategy: str) -> dict:
    """Lädt beste bekannte params aus lab_discoveries oder gibt leeres Dict zurück."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT params_json FROM lab_discoveries
               WHERE strategy=? AND framework_version != 'v7'
               ORDER BY composite_score DESC LIMIT 1""",
            (strategy,),
        ).fetchone()
    except Exception:
        row = None
    conn.close()
    if row and row["params_json"]:
        try:
            return json.loads(row["params_json"])
        except Exception:
            pass
    return {}


def _start_ts() -> int:
    now_ms = int(time.time() * 1000)
    return now_ms - REEVAL_DAYS * 24 * 3600 * 1000


def run_one(strategy: str, asset: str) -> ReevalResult:
    start_ts = _start_ts()
    end_ts   = int(time.time() * 1000)
    params   = _default_params(strategy)

    try:
        wf = run_walk_forward(
            strategy=strategy,
            asset=asset,
            start_ts=start_ts,
            end_ts=end_ts,
            cfg=params,
            is_bars=REEVAL_IS_BARS,
            oos_bars=REEVAL_OOS_BARS,
            step_bars=REEVAL_STEP,
        )
    except Exception as e:
        log(f"[v7-ReEval] WalkForward fehler {strategy}/{asset}: {e}")
        return ReevalResult(
            strategy=strategy, asset=asset,
            dsr_oos=0.0, pbo_val=1.0, stability=0.0, max_dd=0.0,
            composite=0.0, weights_hash="", n_oos=0, oos_folds_n=0,
            passed=False, fail_reasons=[f"WalkForward-Fehler: {e}"],
            params_json=json.dumps(params),
        )

    oos_pnl = wf.all_oos_pnl_rs
    n_oos   = len(oos_pnl)

    # MC-Bootstrap DSR
    dsr_med, _ = bootstrap_dsr(oos_pnl, n_tested=len(SIGNAL_FNS))

    # PBO via CSCV (braucht OOS-Folds mit pnl_rs pro Fold)
    if wf.n_folds >= OOS_FOLDS_MIN_V7:
        fold_oos_rets = [getattr(f, "_oos_pnl_rs", []) for f in wf.folds]
        fold_is_rets  = [getattr(f, "_is_pnl_rs",  []) for f in wf.folds]
        # Nur Folds mit Trades verwenden
        valid = [(a, b) for a, b in zip(fold_is_rets, fold_oos_rets) if a and b]
        if len(valid) >= OOS_FOLDS_MIN_V7:
            pbo_val = pbo([v[0] for v in valid], [v[1] for v in valid])
        else:
            pbo_val = 0.5
    else:
        pbo_val = 0.5  # Fallback bei zu wenigen Folds

    # Stability
    try:
        stab_result = run_stability(strategy, asset, start_ts, end_ts, params)
        stab_score = stab_result.stability_score
    except Exception as e:
        log(f"[v7-ReEval] Stability-Fehler {strategy}/{asset}: {e}")
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

    # Gate-Check
    fail_reasons: list[str] = []
    if dsr_med < DSR_MIN_DRY_RUN:
        fail_reasons.append(f"DSR={dsr_med:.3f} < {DSR_MIN_DRY_RUN}")
    if pbo_val > PBO_MAX:
        fail_reasons.append(f"PBO={pbo_val:.3f} > {PBO_MAX}")
    if stab_score < STABILITY_MIN:
        fail_reasons.append(f"Stability={stab_score:.3f} < {STABILITY_MIN}")
    if max_dd > MAX_DD_GATE:
        fail_reasons.append(f"MaxDD={max_dd:.3f} > {MAX_DD_GATE}")
    if n_oos < 100:
        fail_reasons.append(f"n_oos={n_oos} < 100")
    if wf.n_folds < OOS_FOLDS_MIN_V7:
        fail_reasons.append(f"oos_folds_n={wf.n_folds} < {OOS_FOLDS_MIN_V7}")

    return ReevalResult(
        strategy=strategy,
        asset=asset,
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
        params_json=json.dumps(params),
    )


def _save_discovery(result: ReevalResult, conn) -> int:
    params_hash = _make_params_hash(result.strategy, result.asset)
    now         = _now_iso()

    # Basis-Spalten (immer vorhanden nach run_migrations)
    base_sql = """INSERT OR IGNORE INTO lab_discoveries
           (discovered_at, params_hash, strategy, asset, params_json,
            framework_version, lab_config_hash,
            composite_weights_hash, oos_folds_n, re_evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)"""
    base_vals = (
        now, params_hash, result.strategy, result.asset, result.params_json,
        "v7", LAB_SEARCH_CFG.hash(),
        result.weights_hash, result.oos_folds_n, now,
    )
    cur = conn.execute(base_sql, base_vals)
    row_id = cur.lastrowid or 0
    conn.commit()

    if row_id == 0:
        return 0  # INSERT OR IGNORE → bereits vorhanden

    # Optionale v6/v7-Spalten (können per ALTER TABLE hinzugefügt worden sein)
    optional_updates: list[tuple[str, object]] = [
        ("dsr_value", result.dsr_oos),
        ("pbo_value", result.pbo_val),
        ("stability_score", result.stability),
        ("composite_score", result.composite),
        ("max_drawdown", -result.max_dd),
        ("cost_model_applied", 1),
        ("backtest_funding_model", "static"),
        ("intrabar_model", "gbm"),
        ("sync_status", "pending"),
        ("deployment_status", "lab"),
    ]
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_discoveries)").fetchall()}
    for col, val in optional_updates:
        if col in existing_cols:
            conn.execute(
                f"UPDATE lab_discoveries SET {col}=? WHERE params_hash=?",
                (val, params_hash),
            )
    conn.commit()
    return row_id


def _write_report(results: list[ReevalResult], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    lines = [
        f"# v7 Re-Eval Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"Strategien: {len(set(r.strategy for r in results))} | "
        f"Assets: {len(set(r.asset for r in results))} | "
        f"Gesamt: {len(results)} | "
        f"Pass: {sum(1 for r in results if r.passed)} | "
        f"Fail: {sum(1 for r in results if not r.passed)}",
        "",
        "| Strategie | Asset | Composite | DSR | PBO | MaxDD | Stability | Folds | n_oos | Pass |",
        "|-----------|-------|-----------|-----|-----|-------|-----------|-------|-------|------|",
    ]
    for r in sorted(results, key=lambda x: (-x.composite, x.strategy)):
        status = "✅" if r.passed else "❌"
        lines.append(
            f"| {r.strategy} | {r.asset} | {r.composite:.3f} | {r.dsr_oos:.3f} | "
            f"{r.pbo_val:.3f} | {r.max_dd:.3f} | {r.stability:.3f} | "
            f"{r.oos_folds_n} | {r.n_oos} | {status} |"
        )
    out_path = os.path.join(out_dir, "summary.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"[v7-ReEval] Report: {out_path}")


def main(strategies: list[str] | None = None, assets: list[str] | None = None) -> list[ReevalResult]:
    strats = strategies or list(SIGNAL_FNS.keys())
    assts  = assets    or LIVE_ASSETS
    total  = len(strats) * len(assts)

    log(f"[v7-ReEval] Starte Re-Evaluation: {len(strats)} Strategien × {len(assts)} Assets = {total} Kombinationen")

    conn = get_connection()
    results: list[ReevalResult] = []
    done = 0

    for strategy in strats:
        for asset in assts:
            done += 1
            log(f"[v7-ReEval] [{done}/{total}] {strategy}/{asset}")
            result = run_one(strategy, asset)
            disc_id = _save_discovery(result, conn)
            result.discovery_id = disc_id

            status = "PASS" if result.passed else f"FAIL ({', '.join(result.fail_reasons[:2])})"
            log(
                f"[v7-ReEval] {strategy}/{asset}: composite={result.composite:.3f} "
                f"DSR={result.dsr_oos:.3f} PBO={result.pbo_val:.3f} → {status}"
            )
            results.append(result)

    conn.close()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "research", "findings", "v7_reeval", date_str,
    )
    _write_report(results, out_dir)

    passed = sum(1 for r in results if r.passed)
    log(f"[v7-ReEval] Abgeschlossen: {passed}/{len(results)} Pass")
    return results


if __name__ == "__main__":
    main()
