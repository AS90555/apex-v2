#!/usr/bin/env python3
"""
Canary-Tracker — Squeeze Dry-Run vs. Lab-Erwartung

Liest abgeschlossene dry_run-Trades der squeeze-Strategie aus SQLite
und prüft ob die Live-Performance mit dem Lab-Backtest übereinstimmt.

Abbruchkriterium: Wenn Avg-R-Diskrepanz > 30% nach ≥ 30 Trades
→ Strategie pausieren und neu evaluieren.

Nutzung:
  python3 scripts/canary_check.py            # alle Assets
  python3 scripts/canary_check.py --asset ETH
  python3 scripts/canary_check.py --ci       # nur Zahlen, kein ASCII (CI/Cron)
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection

# ── Lab-Referenzwerte (Auto-Lab 2026-04-27, OOS 219 Tage) ───────────────────
LAB_REFERENCE = {
    "ETH": {"avg_r": 0.095, "pf": 1.14, "n_oos": 1924},
    "BTC": {"avg_r": 0.068, "pf": 1.10, "n_oos":  418},
    "SOL": {"avg_r": 0.053, "pf": 1.07, "n_oos": 1527},
}

TARGET_TRADES   = 100   # Mindest-Sample für Go-Live-Entscheidung
WARN_THRESHOLD  = 0.30  # ±30% Diskrepanz → Warnung
KILL_THRESHOLD  = 0.50  # ±50% Diskrepanz → Stop-Empfehlung


def _load_trades(conn, asset: str | None) -> list[dict]:
    sql  = """
        SELECT asset, direction, entry_price, exit_price, exit_reason, pnl_r
        FROM trades
        WHERE strategy='squeeze' AND mode='dry_run' AND exit_ts IS NOT NULL
    """
    args = []
    if asset:
        sql  += " AND asset=?"
        args.append(asset)
    sql += " ORDER BY exit_ts ASC"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _metrics(trades: list[dict]) -> dict:
    n      = len(trades)
    if n == 0:
        return {"n": 0, "avg_r": None, "pf": None, "wr": None, "total_r": 0.0}
    total_r = sum(t["pnl_r"] for t in trades)
    wins    = [t["pnl_r"] for t in trades if t["pnl_r"] > 0]
    losses  = [t["pnl_r"] for t in trades if t["pnl_r"] < 0]
    gw      = sum(wins)
    gl      = abs(sum(losses))
    return {
        "n":       n,
        "avg_r":   round(total_r / n, 4),
        "pf":      round(gw / gl, 3) if gl > 0 else 999.0,
        "wr":      round(len(wins) / n * 100, 1),
        "total_r": round(total_r, 2),
    }


def _bar(value: float, max_val: float, width: int = 20, fill: str = "█") -> str:
    filled = int(round(abs(value) / max_val * width)) if max_val else 0
    filled = min(filled, width)
    char   = fill if value >= 0 else "░"
    return char * filled + "·" * (width - filled)


def _discrepancy_label(live_avg_r: float, lab_avg_r: float) -> tuple[str, str]:
    if lab_avg_r == 0:
        return "n/a", "?"
    disc = (live_avg_r - lab_avg_r) / abs(lab_avg_r)
    pct  = f"{disc:+.0%}"
    if abs(disc) <= WARN_THRESHOLD:
        label = "✅ OK"
    elif abs(disc) <= KILL_THRESHOLD:
        label = "⚠️  WARN"
    else:
        label = "🛑 STOP"
    return pct, label


def print_report(all_trades: list[dict], filter_asset: str | None, ci: bool):
    assets = [filter_asset] if filter_asset else list(LAB_REFERENCE.keys())

    if not ci:
        print()
        print("═" * 62)
        print("  SQUEEZE CANARY-TRACKER — Dry-Run vs. Lab")
        print("═" * 62)

    overall_trades = []

    for asset in assets:
        trades  = [t for t in all_trades if t["asset"] == asset]
        m       = _metrics(trades)
        lab     = LAB_REFERENCE.get(asset, {})
        lab_r   = lab.get("avg_r", 0)
        lab_pf  = lab.get("pf", 0)
        lab_n   = lab.get("n_oos", 0)
        n       = m["n"]
        progress = min(n / TARGET_TRADES * 100, 100)

        overall_trades.extend(trades)

        if ci:
            disc_pct, disc_label = _discrepancy_label(m["avg_r"] or 0, lab_r)
            print(f"{asset}  n={n}/{TARGET_TRADES}  avg_r={m['avg_r'] or 'n/a'}  lab={lab_r}  disc={disc_pct}  {disc_label}")
            continue

        # ASCII-Report
        print(f"\n  ── {asset}  [{n}/{TARGET_TRADES} Trades | {progress:.0f}% zum Ziel]")
        print(f"  {'Fortschritt':12} [{_bar(n, TARGET_TRADES, 20, '▓')}] {progress:.0f}%")

        if n == 0:
            print(f"  Noch keine Trades abgeschlossen.")
            print(f"  Lab-Referenz: AvgR={lab_r:+.3f}  PF={lab_pf:.2f}  (n_oos={lab_n})")
            continue

        disc_pct, disc_label = _discrepancy_label(m["avg_r"], lab_r)

        print(f"  {'Avg R':12} Live: {m['avg_r']:>+7.3f}R   Lab: {lab_r:>+7.3f}R   Diff: {disc_pct}  {disc_label}")
        print(f"  {'PF':12} Live: {m['pf']:>7.3f}    Lab: {lab_pf:>7.3f}")
        print(f"  {'Win-Rate':12} {m['wr']:>6.1f}%")
        print(f"  {'Total R':12} {m['total_r']:>+7.2f}R  über {n} Trades")

        # Warn wenn genug Daten und Diskrepanz zu hoch
        if n >= 30 and disc_label != "✅ OK":
            print()
            print(f"  !! {disc_label}: Live-Avg-R weicht {disc_pct} vom Lab ab.")
            if disc_label == "🛑 STOP":
                print(f"  !! Empfehlung: SQUEEZE/{asset} pausieren + neu evaluieren.")
            else:
                print(f"  !! Beobachten — evtl. Marktregime-Shift.")

    # Gesamt-Zusammenfassung
    if not filter_asset and not ci:
        om   = _metrics(overall_trades)
        print()
        print("─" * 62)
        print(f"  GESAMT  [{om['n']} Trades | {om['total_r']:+.2f}R | WR {om['wr'] or 0:.1f}% | Avg {om['avg_r'] or 0:+.3f}R]")

        # Go-Live-Empfehlung
        print()
        print("  GO-LIVE CHECKLISTE:")
        for asset in assets:
            t       = [x for x in all_trades if x["asset"] == asset]
            m       = _metrics(t)
            n       = m["n"]
            lab_r   = LAB_REFERENCE.get(asset, {}).get("avg_r", 0)
            if n < 30:
                tick = "⏳"
                note = f"n={n} — warte auf ≥30 Trades"
            elif m["avg_r"] is not None and abs(m["avg_r"] - lab_r) / abs(lab_r) <= WARN_THRESHOLD:
                tick = "✅"
                note = f"n={n} — Diskrepanz OK → live-ready"
            else:
                tick = "⚠️ "
                note = f"n={n} — Diskrepanz zu hoch"
            print(f"  {tick} {asset:<6} {note}")

        print()
        if om["n"] >= TARGET_TRADES:
            print("  🎯 100 Trades erreicht → Go/No-Go Entscheidung fällig!")
        else:
            remaining = TARGET_TRADES - om["n"]
            print(f"  ⏳ Noch {remaining} Trades bis zur Go-Live-Entscheidung.")
        print()


def main():
    p = argparse.ArgumentParser(description="Squeeze Canary-Tracker")
    p.add_argument("--asset", choices=list(LAB_REFERENCE), help="Nur ein Asset anzeigen")
    p.add_argument("--ci",    action="store_true", help="Maschinelles Output (Cron/CI)")
    args = p.parse_args()

    conn   = get_connection()
    trades = _load_trades(conn, args.asset)
    conn.close()

    print_report(trades, args.asset, args.ci)


if __name__ == "__main__":
    main()
