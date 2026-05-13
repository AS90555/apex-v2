"""
Legacy Re-Evaluation (Phase 4) — läuft manuell oder 1×/Woche via Cron.

Für alle lab_discoveries mit framework_version IS NULL oder 'v1':
  - Lädt Params, führt v6-Backtest (Phase-3-Engine) durch.
  - Berechnet DSR, MaxDD, Calmar.
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
from backtest.engine import run_backtest
from backtest.metrics import dsr as calc_dsr, max_drawdown, calmar, sharpe
from backtest.composite_score import composite_score, CompositeInput
from config.settings import DSR_MIN_DRY_RUN, PBO_MAX, STABILITY_MIN


_MS_PER_DAY = 86_400_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reeval_one(disc: dict, conn) -> dict:
    """
    Führt Re-Eval für eine Discovery durch.
    Gibt dict mit neuen Metrik-Werten zurück.
    """
    params = json.loads(disc["params_json"])
    strategy = disc["strategy"]
    asset    = disc["asset"]

    # Zeitraum: letzte 480 Tage (entspricht Phase-1-WF-Fenster)
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - 480 * _MS_PER_DAY

    try:
        bt = run_backtest(
            strategy=strategy,
            asset=asset,
            start_ts=start_ts,
            end_ts=end_ts,
            cfg=params,
            cooldown_bars=params.get("COOLDOWN_BARS", 8),
            apply_costs=True,
        )
    except Exception as e:
        log(f"[REEVAL] Fehler bei {strategy}/{asset} #{disc['id']}: {e}")
        return {}

    pnl_rs = [t.pnl_r for t in bt.trades]
    n      = len(pnl_rs)

    if n < 5:
        return {
            "re_evaluated_at":    _now_iso(),
            "framework_version":  "v6_reeval",
            "n_test":             n,
            "reject_note":        f"zu wenige Trades für Re-Eval: {n}",
        }

    dsr_val   = calc_dsr(pnl_rs, n_tested=1)
    mdd       = max_drawdown(pnl_rs)
    cal       = calmar(pnl_rs)
    sr        = sharpe(pnl_rs)

    comp = composite_score(CompositeInput(
        sharpe_oos=sr, dsr=dsr_val, max_drawdown=mdd,
        stability_score=0.5,  # ohne Variations-Backtest → neutral
        pbo=0.5,              # ohne CSCV → neutral
        n_oos=n,
    ))

    return {
        "re_evaluated_at":    _now_iso(),
        "framework_version":  "v6_reeval",
        "dsr_value":          dsr_val,
        "max_drawdown":       mdd,
        "calmar_ratio":       cal,
        "composite_score":    comp,
        "n_test":             n,
        "pf_test_netto":      disc.get("pf_test_netto"),   # unverändertes Feld
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
               max_drawdown=?, calmar_ratio=?, composite_score=?
               WHERE id=?""",
            (
                metrics.get("re_evaluated_at"),
                metrics.get("framework_version", "v6_reeval"),
                metrics.get("dsr_value"),
                metrics.get("max_drawdown"),
                metrics.get("calmar_ratio"),
                metrics.get("composite_score"),
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
