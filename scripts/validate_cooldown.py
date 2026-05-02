#!/usr/bin/env python3
"""
Validiert die 7 aktiven Deployments mit Cooldown=8 Bars.
Vergleicht neuen Micro-Score mit dem gespeicherten (Legacy, ohne Cooldown).
Gibt Empfehlung: ✅ Behalten / ⚠️ Prüfen / ❌ Deaktivieren
"""

import sys
import os
import math
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection, run_migrations
from backtest.engine import run_backtest
from core.utils import log

COOLDOWN_BARS = 8
DAYS_TEST     = 180   # 180 Tage: bei ~1 Trade/5d → ~36 Trades → statistisch belastbar
DAYS_TRAIN    = 120

MIN_SCORE_KEEP   = 8.0   # unter diesem Wert → ❌
MIN_SCORE_WARN   = 15.0  # unter diesem Wert → ⚠️

RISK_PER_TRADE   = 1.50  # USDT


def _calc_micro_score(pf, avg_r, wr, n, max_dd_r):
    if n < 15 or pf <= 0 or avg_r <= 0 or max_dd_r <= 0:
        return 0.0
    dd_penalty    = 1.0 / (1.0 + max_dd_r / 3.0)
    calmar        = avg_r / max_dd_r
    calmar_factor = min(calmar / 0.05, 2.0)
    return round(
        math.sqrt(pf) * avg_r * (wr / 50.0) * math.log(max(n, 2))
        * dd_penalty * calmar_factor * 10, 2,
    )


def _max_drawdown_r(trades) -> float:
    peak = 0.0
    cumr = 0.0
    mdd  = 0.0
    for t in trades:
        cumr += t.pnl_r
        if cumr > peak:
            peak = cumr
        dd = peak - cumr
        if dd > mdd:
            mdd = dd
    return round(mdd, 3)


def main():
    run_migrations()
    conn = get_connection()
    now_ms = int(time.time() * 1000)

    rows = conn.execute("""
        SELECT ad.strategy_key, ad.base_strategy, ad.asset, ad.params_json, ad.mode,
               ld.micro_score as legacy_score, ld.n_test as legacy_n,
               ld.avg_r_test, ld.pf_test, ld.wr_test
        FROM active_deployments ad
        LEFT JOIN lab_discoveries ld ON ld.id = ad.discovery_id
        WHERE ad.active=1
        ORDER BY ld.micro_score DESC
    """).fetchall()
    conn.close()

    print("\n" + "═" * 78)
    print("  COOLDOWN-VALIDIERUNG — 7 aktive Deployments (cooldown=8 Bars)")
    print("  Legacy = ohne Cooldown  |  Neu = mit Cooldown=8")
    print("═" * 78)

    results = []
    for row in rows:
        key, strategy, asset, params_json, mode, legacy_score, legacy_n, legacy_avg_r, legacy_pf, legacy_wr = row
        params = json.loads(params_json)

        test_start = now_ms - DAYS_TEST  * 86_400_000
        test_end   = now_ms
        train_start = now_ms - (DAYS_TRAIN + DAYS_TEST) * 86_400_000
        train_end   = now_ms - DAYS_TEST * 86_400_000

        try:
            te = run_backtest(strategy, asset, test_start, test_end,
                              cfg=params, cooldown_bars=COOLDOWN_BARS)
        except Exception as e:
            print(f"  ❗ {key}: Backtest-Fehler — {e}")
            continue

        n      = te.total
        if n == 0:
            new_score = 0.0
            new_wr    = 0.0
            new_avg_r = 0.0
            new_pf    = 0.0
            mdd       = 0.0
        else:
            wins   = sum(1 for t in te.trades if t.pnl_r > 0)
            new_wr = round(wins / n * 100, 1)
            total_r = sum(t.pnl_r for t in te.trades)
            new_avg_r = round(total_r / n, 3)
            gross_w = sum(t.pnl_r for t in te.trades if t.pnl_r > 0)
            gross_l = abs(sum(t.pnl_r for t in te.trades if t.pnl_r < 0))
            new_pf  = round(gross_w / gross_l, 2) if gross_l > 0 else 0.0
            mdd     = _max_drawdown_r(te.trades)
            new_score = _calc_micro_score(new_pf, new_avg_r, new_wr, n, mdd) if mdd > 0 else 0.0

        delta = new_score - (legacy_score or 0)
        delta_pct = (delta / legacy_score * 100) if legacy_score else 0

        if new_score >= MIN_SCORE_WARN:
            icon = "✅"
        elif new_score >= MIN_SCORE_KEEP:
            icon = "⚠️ "
        else:
            icon = "❌"

        results.append({
            "key": key, "strategy": strategy, "asset": asset, "mode": mode,
            "legacy_score": legacy_score, "new_score": new_score,
            "n_new": n, "wr_new": new_wr, "avg_r_new": new_avg_r,
            "delta_pct": delta_pct, "icon": icon,
        })

        print(f"\n  {icon} {key} [{mode}]")
        print(f"     Legacy : score={legacy_score:5.2f}  n={legacy_n}  wr={legacy_wr:.0f}%  avg_r={legacy_avg_r:+.3f}  pf={legacy_pf:.2f}")
        print(f"     Neu    : score={new_score:5.2f}  n={n}   wr={new_wr:.0f}%  avg_r={new_avg_r:+.3f}  pf={new_pf:.2f}  mdd={mdd:.1f}R")
        print(f"     Delta  : {delta:+.2f} ({delta_pct:+.0f}%)")

    print("\n" + "─" * 78)
    keep  = sum(1 for r in results if r["icon"] == "✅")
    warn  = sum(1 for r in results if r["icon"] == "⚠️ ")
    drop  = sum(1 for r in results if r["icon"] == "❌")
    print(f"  Ergebnis: ✅ {keep} Behalten  ⚠️  {warn} Prüfen  ❌ {drop} Deaktivieren")
    print("═" * 78 + "\n")

    if drop > 0:
        print("  ❌ Deaktivierungsempfehlung:")
        for r in results:
            if r["icon"] == "❌":
                print(f"     → {r['key']} (score={r['new_score']:.2f})")
    if warn > 0:
        print("  ⚠️  Zur Beobachtung:")
        for r in results:
            if r["icon"] == "⚠️ ":
                print(f"     → {r['key']} (score={r['new_score']:.2f}, delta={r['delta_pct']:+.0f}%)")
    print()


if __name__ == "__main__":
    main()
