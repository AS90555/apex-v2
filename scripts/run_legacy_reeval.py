"""
Legacy Re-Evaluation (Phase 4) — läuft manuell oder 1×/Woche via Cron.

DEPRECATED (v7): Für neue Re-Evaluierungen scripts/run_v7_reeval.py nutzen.
Diese Datei bleibt für Rollback-Szenarien erhalten.

Für alle lab_discoveries mit framework_version IS NULL oder 'v1':
  - Lädt Params, führt Walk-Forward (v7 Phase 1) mit embargo_mode durch.
  - Berechnet DSR, MaxDD, Calmar via WalkForwardEngine.
  - Schreibt Werte mit re_evaluated_at=now() und framework_version='v6_reeval'.
  - Setzt deployment_status auf 'frozen' wenn Gates nicht bestanden.

Aufruf:
    python scripts/run_legacy_reeval.py                  # alle Legacy
    python scripts/run_legacy_reeval.py --strategy=squeeze --asset=BTC
    python scripts/run_legacy_reeval.py --limit=10       # max 10 Discoveries
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log
from backtest.walk_forward import run_walk_forward
from backtest.metrics import max_drawdown, calmar, sharpe
from backtest.composite_score import composite_score, CompositeInput
from config.settings import DSR_MIN_DRY_RUN, PBO_MAX, STABILITY_MIN


_MS_PER_DAY = 86_400_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reeval_one(disc: dict, conn) -> dict:
    """
    Führt Re-Eval für eine Discovery durch (v7: WalkForwardEngine mit Embargo).
    Gibt dict mit neuen Metrik-Werten zurück.
    """
    params   = json.loads(disc["params_json"])
    strategy = disc["strategy"]
    asset    = disc["asset"]

    # 480 Tage für IS+OOS-Folds (WF splittiert intern)
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - 480 * _MS_PER_DAY

    try:
        wf = run_walk_forward(
            strategy=strategy,
            asset=asset,
            start_ts=start_ts,
            end_ts=end_ts,
            cfg=params,
            cooldown_bars=params.get("COOLDOWN_BARS", 8),
            apply_costs=True,
            # purge_bars=None → compute_max_lookback(strategy) automatisch
        )
    except Exception as e:
        log(f"[REEVAL] Fehler bei {strategy}/{asset} #{disc['id']}: {e}")
        return {}

    pnl_rs = wf.all_oos_pnl_rs
    n      = len(pnl_rs)
    n_folds = wf.n_folds

    if n < 5 or n_folds < 1:
        return {
            "re_evaluated_at":   _now_iso(),
            "framework_version": "v6_reeval",
            "n_test":            n,
            "reject_note":       f"zu wenige OOS-Trades: {n} ({n_folds} Folds)",
        }

    mdd  = max_drawdown(pnl_rs)
    cal  = calmar(pnl_rs)
    sr   = sharpe(pnl_rs)
    # DSR aus Walk-Forward-Mittel (echte OOS-Returns, nicht IS-Daten)
    dsr_val = wf.mean_dsr_oos

    comp = composite_score(CompositeInput(
        sharpe_oos=sr, dsr=dsr_val, max_drawdown=mdd,
        stability_score=0.5,  # Placeholder bis v7 Phase 2 (stability.py)
        pbo=0.5,              # Placeholder bis v7 Phase 2 (CSCV)
        n_oos=n,
    ))

    return {
        "re_evaluated_at":   _now_iso(),
        "framework_version": "v6_reeval",
        "dsr_value":         dsr_val,
        "max_drawdown":      mdd,
        "calmar_ratio":      cal,
        "composite_score":   comp,
        "n_test":            n,
        "oos_folds_n":       n_folds,
        "pf_test_netto":     disc.get("pf_test_netto"),
    }


def _passes_gates(metrics: dict) -> bool:
    dsr_val = metrics.get("dsr_value", 0.0) or 0.0
    mdd     = abs(metrics.get("max_drawdown", -999) or -999)
    n       = metrics.get("n_test", 0) or 0
    return dsr_val >= DSR_MIN_DRY_RUN and mdd <= 30.0 and n >= 10


def main(strategy: str = None, asset: str = None, limit: int = 9999) -> None:
    conn  = get_connection()

    where_parts = ["(framework_version IS NULL OR framework_version IN ('v1', 'v6_reeval'))"]
    params_q    = []
    if strategy:
        where_parts.append("strategy=?")
        params_q.append(strategy)
    if asset:
        where_parts.append("asset=?")
        params_q.append(asset)

    candidates = conn.execute(
        f"""SELECT id, strategy, asset, params_json, pf_test_netto,
                   n_test, dsr, deployment_status, framework_version
            FROM lab_discoveries
            WHERE {' AND '.join(where_parts)}
            ORDER BY micro_score DESC NULLS LAST
            LIMIT {limit}""",
        params_q,
    ).fetchall()

    log(f"[REEVAL] {len(candidates)} Discoveries zum Re-Evaluieren")

    promoted = failed = skipped = 0
    for disc in candidates:
        log(f"[REEVAL] Re-Eval #{disc['id']} {disc['strategy']}/{disc['asset']} ...")
        metrics = _reeval_one(dict(disc), conn)

        if not metrics:
            skipped += 1
            continue

        # Schreibe Metriken zurück
        conn.execute(
            """UPDATE lab_discoveries SET
               re_evaluated_at=?, framework_version=?, dsr_value=?,
               max_drawdown=?, calmar_ratio=?, composite_score=?, oos_folds_n=?
               WHERE id=?""",
            (
                metrics.get("re_evaluated_at"),
                metrics.get("framework_version", "v6_reeval"),
                metrics.get("dsr_value"),
                metrics.get("max_drawdown"),
                metrics.get("calmar_ratio"),
                metrics.get("composite_score"),
                metrics.get("oos_folds_n"),
                disc["id"],
            ),
        )

        if not _passes_gates(metrics):
            conn.execute(
                "UPDATE lab_discoveries SET deployment_status='frozen' WHERE id=?",
                (disc["id"],),
            )
            log(f"[REEVAL] #{disc['id']} → FROZEN (Gates nicht bestanden: "
                f"DSR={metrics.get('dsr_value', 0):.3f}, "
                f"MaxDD={metrics.get('max_drawdown', 0):.2f}, "
                f"n={metrics.get('n_test', 0)})")
            failed += 1
        else:
            log(f"[REEVAL] #{disc['id']} → OK (DSR={metrics.get('dsr_value', 0):.3f}, "
                f"MaxDD={metrics.get('max_drawdown', 0):.2f})")
            promoted += 1

        conn.commit()

    conn.close()
    log(f"[REEVAL] Fertig — OK={promoted}, Frozen={failed}, Skipped={skipped}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--asset",    default=None)
    parser.add_argument("--limit",    type=int, default=9999)
    args = parser.parse_args()
    main(strategy=args.strategy, asset=args.asset, limit=args.limit)
