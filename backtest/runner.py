#!/usr/bin/env python3
"""
Backtest-Runner — CLI für backtest/engine.py.

Beispiele:
  python3 backtest/runner.py --strategy vaa --asset ETH --days 90
  python3 backtest/runner.py --strategy kdt --asset ETH --days 180
  python3 backtest/runner.py --strategy weekend_momo --asset AVAX --days 365
  python3 backtest/runner.py --all --days 90
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import run_migrations
from core.utils import log
from backtest.engine import run_backtest, BtResult, SIGNAL_FNS, STRATEGY_INTERVAL
from config.settings import VAA_ASSETS, KDT_ASSET, WEEKEND_ASSET


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _r_histogram(trades, width: int = 40) -> str:
    """ASCII-Histogramm der R-Verteilung."""
    if not trades:
        return "  (keine Trades)"
    pnls = [t.pnl_r for t in trades]
    lo, hi = min(pnls), max(pnls)
    if lo == hi:
        return f"  alle Trades: {lo:+.2f}R"

    BINS = 12
    bin_w = (hi - lo) / BINS
    counts = [0] * BINS
    for p in pnls:
        b = min(int((p - lo) / bin_w), BINS - 1)
        counts[b] += 1

    max_c  = max(counts) or 1
    lines  = []
    for i, c in enumerate(counts):
        label = f"{lo + i*bin_w:+5.2f}R"
        bar   = "█" * int(c / max_c * width)
        lines.append(f"  {label} │{bar:<{width}} {c}")
    return "\n".join(lines)


def _print_result(result: BtResult):
    s = result.summary()
    n = s['trades']
    print(f"\n{'═'*60}")
    print(f"  {s['strategy'].upper()} / {s['asset']}")
    print(f"{'─'*60}")

    if n == 0:
        print("  Keine Trades im Zeitraum.")
        print(f"{'═'*60}\n")
        return

    # Basis-Stats
    pnls = [t.pnl_r for t in result.trades]
    profit_factor = (sum(p for p in pnls if p > 0) /
                     abs(sum(p for p in pnls if p < 0)) if any(p < 0 for p in pnls) else float("inf"))

    equity = 0.0; peak = 0.0; max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)

    # Signifikanz-Hinweis
    sig = "✓ signifikant" if n >= 30 else f"⚠ n={n} < 30 (nicht signifikant)"

    print(f"  Trades:         {n}  {sig}")
    print(f"  Win-Rate:       {s['winrate']}%")
    print(f"  Total R:        {s['total_r']:+.2f}R")
    print(f"  Avg R/Trade:    {s['avg_r']:+.3f}R")
    print(f"  Profit Factor:  {profit_factor:.2f}")
    print(f"  Max Drawdown:   -{max_dd:.2f}R")

    by_reason = {}
    for t in result.trades:
        r = t.exit_reason or "?"
        by_reason[r] = by_reason.get(r, 0) + 1
    print(f"  Exit-Gründe:    {dict(sorted(by_reason.items()))}")

    print(f"\n  R-Verteilung:")
    print(_r_histogram(result.trades))
    print(f"{'═'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="APEX V2 Backtest-Runner")
    parser.add_argument("--strategy", choices=list(SIGNAL_FNS), help="Strategie")
    parser.add_argument("--asset",    type=str, help="Asset (z.B. ETH, SOL)")
    parser.add_argument("--days",     type=int, default=90, help="Rückblick in Tagen")
    parser.add_argument("--all",      action="store_true", help="Alle Strategien/Assets testen")
    parser.add_argument("--verbose",  action="store_true", help="Bar-by-Bar-Logging")
    parser.add_argument("--json",     action="store_true", help="Ausgabe als JSON")
    args = parser.parse_args()

    run_migrations()

    end_ts   = _ts_ms(datetime.now(timezone.utc))
    start_ts = _ts_ms(datetime.now(timezone.utc) - timedelta(days=args.days))

    # Welche (strategy, asset)-Kombinationen laufen?
    runs: list[tuple[str, str]] = []

    if args.all:
        # VAA: Live-Assets + BTC als Deep-Backtest-Erweiterung
        bt_vaa_assets = list(dict.fromkeys(VAA_ASSETS + ["BTC", "ETH", "SOL"]))
        for asset in bt_vaa_assets:
            runs.append(("vaa", asset))
        runs.append(("kdt", KDT_ASSET))
        runs.append(("weekend_momo", WEEKEND_ASSET))
    elif args.strategy and args.asset:
        runs.append((args.strategy, args.asset.upper()))
    else:
        parser.error("--strategy + --asset angeben ODER --all verwenden")

    results = []
    for strategy, asset in runs:
        log(f"[RUNNER] Starte: {strategy}/{asset} ({args.days} Tage)")
        result = run_backtest(
            strategy=strategy, asset=asset,
            start_ts=start_ts, end_ts=end_ts,
            verbose=args.verbose,
        )
        results.append(result)
        if not args.json:
            _print_result(result)

    if args.json:
        output = [r.summary() for r in results]
        print(json.dumps(output, indent=2))

    # Gesamtbilanz bei --all
    if args.all and results:
        total_trades = sum(r.total for r in results)
        total_r      = sum(r.total_r for r in results)
        wins         = sum(r.wins for r in results)
        print(f"\n{'═'*55}")
        print(f"  GESAMT: {total_trades} Trades | "
              f"WR {wins/total_trades*100:.1f}% | "
              f"Total {total_r:+.2f}R")
        print(f"{'═'*55}")


if __name__ == "__main__":
    main()
