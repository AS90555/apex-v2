#!/usr/bin/env python3
"""
Asian Fade Grid-Test: 3 Varianten über 730 Tage ETH/1h
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import run_backtest, BtResult

VARIANTS = [
    {
        "name":  "V1 Extreme OB (SHORT)",
        "cfg": {
            "PUMP_THRESHOLD": 0.030, "RSI_OB": 80, "DIRECTION": "short",
            "DUMP_MODE": False, "SL_ATR_MULT": 1.0, "TP_MULT": 1.5,
            "CAPITAL": 68.0, "MAX_RISK_PCT": 0.02,
        },
    },
    {
        "name":  "V2 Trend Following (LONG)",
        "cfg": {
            "PUMP_THRESHOLD": 0.015, "RSI_OB": 60, "DIRECTION": "long",
            "DUMP_MODE": False, "SL_ATR_MULT": 1.0, "TP_MULT": 1.5,
            "CAPITAL": 68.0, "MAX_RISK_PCT": 0.02,
        },
    },
    {
        "name":  "V3 Asian Dip-Buy (LONG)",
        "cfg": {
            "PUMP_THRESHOLD": 0.015, "RSI_OS": 30, "DIRECTION": "long",
            "DUMP_MODE": True, "SL_ATR_MULT": 1.0, "TP_MULT": 1.5,
            "CAPITAL": 68.0, "MAX_RISK_PCT": 0.02,
        },
    },
]

DAYS = 730

def fmt(result: BtResult, name: str):
    s = result.summary()
    n     = s["n_trades"]
    total = s["total_r"]
    wr    = s["win_rate"] * 100
    avg_r = s["avg_r"]
    pf    = s["profit_factor"]
    sig   = "✓ signifikant" if n >= 30 else f"⚠ n={n}<30"
    print(f"\n{'═'*56}")
    print(f"  {name}")
    print(f"{'═'*56}")
    print(f"  Trades : {n:>4}  ({sig})")
    print(f"  Total R: {total:>+.1f}R")
    print(f"  Win-Rate: {wr:.0f}%")
    print(f"  Avg R  : {avg_r:>+.3f}R")
    print(f"  PF     : {pf:.2f}")

def main():
    print(f"\n{'━'*56}")
    print(f"  ASIAN FADE GRID-TEST — ETH — {DAYS} Tage")
    print(f"{'━'*56}")
    results = []
    for v in VARIANTS:
        r = run_backtest(
            strategy="asian_fade", asset="ETH",
            days=DAYS, cfg_override=v["cfg"],
        )
        results.append((v["name"], r))
        fmt(r, v["name"])

    print(f"\n{'━'*56}")
    print("  VERGLEICH")
    print(f"{'━'*56}")
    print(f"  {'Variante':<30} {'n':>4} {'Total R':>8} {'PF':>6}")
    print(f"  {'-'*30} {'-'*4} {'-'*8} {'-'*6}")
    for name, r in results:
        s = r.summary()
        print(f"  {name:<30} {s['n_trades']:>4} {s['total_r']:>+8.1f} {s['profit_factor']:>6.2f}")
    print()

if __name__ == "__main__":
    main()
