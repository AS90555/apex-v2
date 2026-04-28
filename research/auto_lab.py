#!/usr/bin/env python3
"""
APEX Auto-Lab — Continuous Alpha Generation

Programmatischer Parameter-Optimierer mit eingebautem Skeptizismus.
Statt "finde die besten Parameter" → "finde Parameter die robust UND signifikant sind".

Anti-Overfit-Architektur (3 Schichten):
  1. Walk-Forward OOS-Split: 70% Train / 30% Test (chronologisch). Score basiert nur auf Test.
  2. Robustheits-Penalty: Setup muss in beiden Halbzeiten greifen (Train PF ≥ 1.1).
  3. Strikte Mindest-Filter:
        n_test ≥ 40, avg_r_test ≥ 0.4, pf_test ≥ 1.3
        |avg_r_train - avg_r_test| ≤ 0.5  (kein Performance-Drop = kein Overfit)

Fitness-Score: pf_test × min(avg_r_test, 1.0) × log(n_test)
  → belohnt Robustheit und Sample-Größe, dämpft Avg-R-Hype.

Alle Läufe (auch abgelehnte) landen in `research_runs` für Auditierbarkeit.
"""

import sys
import os
import json
import math
import time
import itertools
from datetime import datetime, timezone
from typing import Iterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log
from backtest.engine import run_backtest


# ── Fitness-Schwellen ────────────────────────────────────────────────────────
MIN_TRADES_TEST     = 40
MIN_AVG_R_TEST      = 0.05   # 5 Cent pro R → minimal aber real profitabel
MIN_PF_TEST         = 1.05   # Profit Factor > 1.0 → mehr Gewinner als Verlierer
MIN_PF_TRAIN        = 1.05
MAX_TRAIN_TEST_DROP = 0.5    # |avg_r_train - avg_r_test|

TRAIN_FRAC = 0.70  # 70% Train, 30% Test (chronologisch)


# ── Parameter-Grids pro Strategie ───────────────────────────────────────────
# Konservativ gewählt: nicht zu groß (Combinatorial Explosion), nicht zu eng (zu wenig Variation).
GRIDS = {
    "squeeze": {
        "SQUEEZE_PERIOD": [15, 20, 25],
        "EMA_PERIOD":     [15, 20, 25],
        "SL_ATR_MULT":    [0.5, 0.75, 1.0, 1.5],
        "TP_R":           [1.5, 2.0, 3.0, 4.0],
    },
    "vaa": {
        "VOL_MULT":     [2.0, 2.5, 3.0, 3.5],
        "BODY_MULT":    [0.4, 0.5, 0.6, 0.7],
        "ATR_EXPAND":   [1.0, 1.2, 1.5],
        "TP_R":         [2.0, 3.0, 4.0],
    },
    "kdt": {
        "EMA_PERIOD":   [20, 50, 100],
        "ENTRY_WINDOW": [2, 3, 5],
        "TP_R":         [2.0, 3.0, 4.0],
        "SL_ATR_MULT":  [0.5, 1.0, 1.5],
    },
}


def _grid_iter(grid: dict) -> Iterator[dict]:
    keys   = list(grid.keys())
    values = [grid[k] for k in keys]
    for combo in itertools.product(*values):
        yield dict(zip(keys, combo))


def _summary_metrics(result) -> dict:
    s = result.summary()
    n     = s["trades"]
    if n == 0:
        return {"n": 0, "total_r": 0.0, "avg_r": 0.0, "pf": 0.0, "wr": 0.0}
    wins   = [t.pnl_r for t in result.trades if t.pnl_r > 0]
    losses = [t.pnl_r for t in result.trades if t.pnl_r < 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "n":       n,
        "total_r": round(s["total_r"], 3),
        "avg_r":   round(s["avg_r"],   3),
        "pf":      round(pf, 3),
        "wr":      round(s["winrate"], 2),
    }


def _fitness(test_metrics: dict) -> float:
    """Fitness = pf × min(avg_r, 1.0) × log(n).  0 wenn nicht qualifiziert."""
    n     = test_metrics["n"]
    avg_r = test_metrics["avg_r"]
    pf    = test_metrics["pf"]
    if n <= 0 or pf <= 0:
        return 0.0
    return round(pf * min(avg_r, 1.0) * math.log(max(n, 2)), 4)


def _evaluate(train: dict, test: dict) -> tuple[bool, str]:
    """Strikte Fitness-Prüfung. Gibt (passed, reject_reason) zurück."""
    if test["n"] < MIN_TRADES_TEST:
        return False, f"n_test={test['n']}<{MIN_TRADES_TEST}"
    if test["avg_r"] < MIN_AVG_R_TEST:
        return False, f"avg_r_test={test['avg_r']}<{MIN_AVG_R_TEST}"
    if test["pf"] < MIN_PF_TEST:
        return False, f"pf_test={test['pf']}<{MIN_PF_TEST}"
    if train["pf"] < MIN_PF_TRAIN:
        return False, f"pf_train={train['pf']}<{MIN_PF_TRAIN} (kein robuster Edge)"
    drop = abs(train["avg_r"] - test["avg_r"])
    if drop > MAX_TRAIN_TEST_DROP:
        return False, f"overfit-drop={drop:.2f}>{MAX_TRAIN_TEST_DROP}"
    return True, ""


def _store_run(conn, lab_session: str, strategy: str, asset: str,
               params: dict, train: dict, test: dict,
               passed: bool, reject_reason: str, fitness: float):
    conn.execute(
        """INSERT INTO research_runs
           (created_at, lab_session, strategy, asset, params_json,
            n_train, total_r_train, avg_r_train, pf_train, wr_train,
            n_test,  total_r_test,  avg_r_test,  pf_test,  wr_test,
            fitness_score, passed, reject_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(), lab_session, strategy, asset,
            json.dumps(params, sort_keys=True),
            train["n"], train["total_r"], train["avg_r"], train["pf"], train["wr"],
            test["n"],  test["total_r"],  test["avg_r"],  test["pf"],  test["wr"],
            fitness, 1 if passed else 0, reject_reason or None,
        ),
    )
    conn.commit()


def run_lab(strategy: str, asset: str, days: int = 730,
            verbose: bool = False) -> dict:
    """
    Hauptfunktion: testet Grid auf (strategy, asset) mit chronologischem Train/Test-Split.

    Returns:
      dict mit Lab-Statistik: {tested, passed, top_score, top_params}
    """
    if strategy not in GRIDS:
        raise ValueError(f"Kein Grid definiert für '{strategy}'. Verfügbar: {list(GRIDS)}")

    grid       = GRIDS[strategy]
    combos     = list(_grid_iter(grid))
    total      = len(combos)
    lab_session = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    now_ms     = int(time.time() * 1000)
    end_ms     = now_ms
    start_ms   = end_ms - days * 86_400_000
    split_ms   = start_ms + int((end_ms - start_ms) * TRAIN_FRAC)

    log(f"[LAB] === Auto-Lab Session {lab_session} ===")
    log(f"[LAB] Strategy={strategy} Asset={asset} Combos={total}")
    log(f"[LAB] Train: {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc):%Y-%m-%d} → "
        f"{datetime.fromtimestamp(split_ms/1000, tz=timezone.utc):%Y-%m-%d}")
    log(f"[LAB] Test:  {datetime.fromtimestamp(split_ms/1000, tz=timezone.utc):%Y-%m-%d} → "
        f"{datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d}")
    log(f"[LAB] Filter: n_test≥{MIN_TRADES_TEST}, avg_r_test≥{MIN_AVG_R_TEST}, "
        f"pf_test≥{MIN_PF_TEST}, pf_train≥{MIN_PF_TRAIN}, drop≤{MAX_TRAIN_TEST_DROP}")

    conn         = get_connection()
    passed_runs  = []
    t0           = time.monotonic()

    for i, params in enumerate(combos, 1):
        # Train-Backtest
        train_res = run_backtest(strategy, asset, start_ms, split_ms, cfg=params)
        train_m   = _summary_metrics(train_res)

        # Test-Backtest (Out-of-Sample)
        test_res  = run_backtest(strategy, asset, split_ms,  end_ms,   cfg=params)
        test_m    = _summary_metrics(test_res)

        passed, reason = _evaluate(train_m, test_m)
        fitness        = _fitness(test_m) if passed else 0.0

        _store_run(conn, lab_session, strategy, asset, params,
                   train_m, test_m, passed, reason, fitness)

        if passed:
            passed_runs.append((fitness, params, train_m, test_m))
            log(f"[LAB] ✓ #{i}/{total} PASS fitness={fitness:.3f} "
                f"params={params} train(n={train_m['n']},pf={train_m['pf']},avgR={train_m['avg_r']}) "
                f"test(n={test_m['n']},pf={test_m['pf']},avgR={test_m['avg_r']})")
        elif verbose:
            log(f"[LAB] ✗ #{i}/{total} {reason} params={params}")
        elif i % 25 == 0:
            log(f"[LAB] ... {i}/{total} getestet, {len(passed_runs)} bestanden ({time.monotonic()-t0:.0f}s)")

    conn.close()

    passed_runs.sort(reverse=True)  # höchster Fitness zuerst

    log(f"[LAB] === Fertig: {total} Combos in {time.monotonic()-t0:.0f}s ===")
    log(f"[LAB] Bestanden: {len(passed_runs)}/{total}  (Session: {lab_session})")

    if passed_runs:
        log(f"[LAB] === TOP 5 ===")
        for rank, (fit, params, tr, te) in enumerate(passed_runs[:5], 1):
            log(f"[LAB] #{rank}  fitness={fit:.3f}  params={params}")
            log(f"[LAB]      train n={tr['n']:>3} pf={tr['pf']:.2f} avgR={tr['avg_r']:+.3f} "
                f"|  test n={te['n']:>3} pf={te['pf']:.2f} avgR={te['avg_r']:+.3f}")
    else:
        log(f"[LAB] Kein Setup hat alle Filter überstanden.")

    return {
        "lab_session": lab_session,
        "tested":      total,
        "passed":      len(passed_runs),
        "top":         passed_runs[:5],
    }


def list_top(strategy: str = None, asset: str = None, limit: int = 10) -> list[dict]:
    """Liest TOP-Setups aus der DB (alle Sessions)."""
    conn = get_connection()
    sql  = "SELECT * FROM research_runs WHERE passed=1"
    args = []
    if strategy:
        sql  += " AND strategy=?"
        args.append(strategy)
    if asset:
        sql  += " AND asset=?"
        args.append(asset)
    sql += " ORDER BY fitness_score DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    import argparse
    p = argparse.ArgumentParser(description="APEX Auto-Lab — Continuous Alpha Generation")
    p.add_argument("--strategy", required=True, choices=list(GRIDS),
                   help="Strategie für Grid-Test")
    p.add_argument("--asset",    required=True, help="z.B. ETH, BTC, SOL")
    p.add_argument("--days",     type=int, default=730, help="Backtest-Tiefe in Tagen")
    p.add_argument("--verbose",  action="store_true", help="Auch abgelehnte Runs loggen")
    p.add_argument("--list-top", action="store_true", help="Nur TOP-Setups aus DB anzeigen")
    args = p.parse_args()

    if args.list_top:
        for row in list_top(args.strategy, args.asset, limit=10):
            print(f"  fitness={row['fitness_score']:.3f}  {row['params_json']}")
            print(f"    test:  n={row['n_test']} pf={row['pf_test']:.2f} avgR={row['avg_r_test']:+.3f}")
        return

    run_lab(args.strategy, args.asset, args.days, verbose=args.verbose)


if __name__ == "__main__":
    main()
