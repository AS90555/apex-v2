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


def _print_result(result: BtResult):
    s = result.summary()
    print(f"\n{'═'*55}")
    print(f"  {s['strategy'].upper()} / {s['asset']}")
    print(f"{'─'*55}")
    print(f"  Trades:    {s['trades']}")
    print(f"  Win-Rate:  {s['winrate']}%")
    print(f"  Total R:   {s['total_r']:+.2f}R")
    print(f"  Avg R:     {s['avg_r']:+.3f}R")

    if result.trades:
        by_reason = {}
        for t in result.trades:
            r = t.exit_reason or "unknown"
            by_reason[r] = by_reason.get(r, 0) + 1
        print(f"  Exits:     {dict(sorted(by_reason.items()))}")

        # Equity-Kurve (kompakt)
        equity = 0.0
        peak   = 0.0
        max_dd = 0.0
        for t in result.trades:
            equity += t.pnl_r
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        print(f"  Max DD:    -{max_dd:.2f}R")
    print(f"{'═'*55}\n")


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
        for asset in VAA_ASSETS:
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
