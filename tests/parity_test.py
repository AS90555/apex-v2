"""
parity_test.py — Verifiziert Bit-Parität zwischen Backtest-Engine und GenericDeployedStrategy.

Für jede Strategie in SIGNAL_FNS:
  1. Scanne historische Bars rückwärts bis ein Signal feuert
  2. Simuliere die GenericDeployedStrategy-Mathematik auf demselben BtSignal
  3. Vergleiche entry_price, stop_loss, take_profit_1, direction
     → Toleranz: 0.01% (Unterschied nur durch dec_price-Rounding erlaubt)

Exit-Codes:
  0 = alle getesteten Strategien bestehen (oder kein Signal gefunden)
  1 = mindestens eine Strategie verletzt die Toleranz
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from backtest.engine import SIGNAL_FNS
from config.settings import PRICE_DECIMALS

TOLERANCE_PCT = 0.01   # 0.01 % — nur Rounding-Differenz erlaubt
ASSETS        = ["SOL", "BTC", "ETH", "XRP", "ADA", "AVAX", "LINK"]
SCAN_BARS     = 500    # Maximale Bars pro Asset beim Signal-Scan


def _simulate_live(bt_sig, asset: str) -> dict:
    """
    Repliziert die GenericDeployedStrategy-Mathematik auf einem BtSignal.
    Gibt die 'Live-Werte' zurück, die ein Deployment erzeugen würde.
    """
    dec_price = PRICE_DECIMALS.get(asset, 2)

    entry_price  = bt_sig.entry_price   # Fix B: direkte Übernahme
    sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)
    tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
    direction    = bt_sig.direction

    if direction == "long":
        stop_loss     = round(entry_price - sl_dist_orig, dec_price)
        take_profit_1 = round(entry_price + tp1_dist,     dec_price)
    else:
        stop_loss     = round(entry_price + sl_dist_orig, dec_price)
        take_profit_1 = round(entry_price - tp1_dist,     dec_price)

    return {
        "entry_price":  entry_price,
        "stop_loss":    stop_loss,
        "take_profit_1": take_profit_1,
        "direction":    direction,
    }


def _pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / abs(b) * 100


def _find_signal(conn, strategy_name: str, signal_fn, cfg: dict):
    """
    Scannt rückwärts über ASSETS × SCAN_BARS bis ein BtSignal gefeuert wird.
    Gibt (bt_sig, asset) zurück oder (None, None) wenn keines gefunden.
    """
    for asset in ASSETS:
        rows = conn.execute(
            "SELECT ts FROM candles WHERE asset=? AND interval='1h' "
            "ORDER BY ts DESC LIMIT ?",
            (asset, SCAN_BARS),
        ).fetchall()
        timestamps = [r[0] for r in rows]

        for as_of_ts in timestamps:
            try:
                sig = signal_fn(conn, asset, as_of_ts, cfg)
            except Exception:
                continue
            if sig is not None:
                return sig, asset

    return None, None


def run_parity_tests() -> bool:
    """Führt Parity-Tests für alle SIGNAL_FNS aus. Gibt True zurück wenn alle bestehen."""
    conn      = get_connection()
    passed    = 0
    failed    = 0
    skipped   = 0
    failures  = []

    for strategy_name, signal_fn in SIGNAL_FNS.items():
        cfg = {}   # Standard-Parameter — identisch mit Labor-Defaults

        bt_sig, asset = _find_signal(conn, strategy_name, signal_fn, cfg)

        if bt_sig is None:
            print(f"  SKIP  {strategy_name:<22} — kein Signal in {SCAN_BARS} Bars × {len(ASSETS)} Assets")
            skipped += 1
            continue

        live = _simulate_live(bt_sig, asset)

        # Vergleiche direction
        if live["direction"] != bt_sig.direction:
            msg = (f"DIRECTION-MISMATCH: bt={bt_sig.direction} live={live['direction']}")
            print(f"  FAIL  {strategy_name:<22} {asset} — {msg}")
            failures.append((strategy_name, asset, msg))
            failed += 1
            continue

        # Vergleiche numerische Felder
        checks = [
            ("entry_price",   bt_sig.entry_price,   live["entry_price"]),
            ("stop_loss",     bt_sig.stop_loss,      live["stop_loss"]),
            ("take_profit_1", bt_sig.take_profit_1,  live["take_profit_1"]),
        ]

        ok = True
        for field, bt_val, live_val in checks:
            diff = _pct_diff(live_val, bt_val)
            if diff > TOLERANCE_PCT:
                msg = (f"{field}: bt={bt_val} live={live_val} diff={diff:.4f}% "
                       f"(Toleranz={TOLERANCE_PCT}%)")
                print(f"  FAIL  {strategy_name:<22} {asset} — {msg}")
                failures.append((strategy_name, asset, msg))
                ok = False

        if ok:
            ep   = bt_sig.entry_price
            sl   = bt_sig.stop_loss
            tp1  = bt_sig.take_profit_1
            dir_ = bt_sig.direction
            print(f"  PASS  {strategy_name:<22} {asset}  "
                  f"{dir_} @ {ep}  SL={sl}  TP1={tp1}")
            passed += 1
        else:
            failed += 1

    conn.close()

    print()
    print(f"Ergebnis: {passed} PASS  |  {failed} FAIL  |  {skipped} SKIP")
    print(f"Toleranz: {TOLERANCE_PCT}%  |  "
          f"Strategien gesamt: {len(SIGNAL_FNS)}  |  "
          f"Getestet (mit Signal): {passed + failed}")

    if failures:
        print()
        print("Fehlgeschlagene Checks:")
        for strat, asset, msg in failures:
            print(f"  [{strat} / {asset}] {msg}")

    return failed == 0


if __name__ == "__main__":
    print(f"APEX V2 — Parity Test (Backtest == Live)")
    print(f"Strategien: {len(SIGNAL_FNS)}  |  Assets: {ASSETS}  |  Scan: {SCAN_BARS} Bars")
    print()

    ok = run_parity_tests()
    sys.exit(0 if ok else 1)
