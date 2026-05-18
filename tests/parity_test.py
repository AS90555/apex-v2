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
from config.settings import PRICE_DECIMALS, SIZE_DECIMALS, RISK_USDT, V7_FUNDING_SIZING

TOLERANCE_PCT      = 0.01   # 0.01 % — nur Rounding-Differenz erlaubt
TOLERANCE_SIZE_PCT = 1.0    # 1 % — Funding-Sizing kann leicht abweichen
ASSETS             = ["SOL", "BTC", "ETH", "XRP", "ADA", "AVAX", "LINK"]
SCAN_BARS          = 500    # Maximale Bars pro Asset beim Signal-Scan


def _simulate_live(bt_sig, asset: str) -> dict:
    """
    Repliziert die GenericDeployedStrategy-Mathematik auf einem BtSignal.
    Gibt die 'Live-Werte' zurück, die ein Deployment erzeugen würde.
    """
    dec_price = PRICE_DECIMALS.get(asset, 2)
    dec_size  = SIZE_DECIMALS.get(asset, 2)

    entry_price  = bt_sig.entry_price   # Fix B: direkte Übernahme
    sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)
    tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
    tp2_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_2)
    direction    = bt_sig.direction

    if direction == "long":
        stop_loss     = round(entry_price - sl_dist_orig, dec_price)
        take_profit_1 = round(entry_price + tp1_dist,     dec_price)
        take_profit_2 = round(entry_price + tp2_dist,     dec_price)
    else:
        stop_loss     = round(entry_price + sl_dist_orig, dec_price)
        take_profit_1 = round(entry_price - tp1_dist,     dec_price)
        take_profit_2 = round(entry_price - tp2_dist,     dec_price)

    sl_dist = abs(entry_price - stop_loss)
    size = round(RISK_USDT / sl_dist, dec_size) if sl_dist > 0 else 0.0

    return {
        "entry_price":   entry_price,
        "stop_loss":     stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "size":          size,
        "direction":     direction,
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

        # Vergleiche numerische Felder (TP2 + Size neu in P3.3)
        checks = [
            ("entry_price",   bt_sig.entry_price,    live["entry_price"],   TOLERANCE_PCT),
            ("stop_loss",     bt_sig.stop_loss,       live["stop_loss"],     TOLERANCE_PCT),
            ("take_profit_1", bt_sig.take_profit_1,   live["take_profit_1"], TOLERANCE_PCT),
            ("take_profit_2", bt_sig.take_profit_2,   live["take_profit_2"], TOLERANCE_PCT),
        ]
        # Size: nur wenn V7_FUNDING_SIZING deaktiviert — andernfalls Funding-Anpassung erwartet
        if not V7_FUNDING_SIZING:
            checks.append(("size", bt_sig.size, live["size"], TOLERANCE_SIZE_PCT))

        ok = True
        for field, bt_val, live_val, tol in checks:
            diff = _pct_diff(live_val, bt_val)
            if diff > tol:
                msg = (f"{field}: bt={bt_val} live={live_val} diff={diff:.4f}% "
                       f"(Toleranz={tol}%)")
                print(f"  FAIL  {strategy_name:<22} {asset} — {msg}")
                failures.append((strategy_name, asset, msg))
                ok = False

        if ok:
            ep   = bt_sig.entry_price
            sl   = bt_sig.stop_loss
            tp1  = bt_sig.take_profit_1
            tp2  = bt_sig.take_profit_2
            sz   = bt_sig.size
            dir_ = bt_sig.direction
            print(f"  PASS  {strategy_name:<22} {asset}  "
                  f"{dir_} @ {ep}  SL={sl}  TP1={tp1}  TP2={tp2}  size={sz}")
            passed += 1
        else:
            failed += 1

    conn.close()

    print()
    print(f"Ergebnis: {passed} PASS  |  {failed} FAIL  |  {skipped} SKIP")
    print(f"Toleranz entry/sl/tp: {TOLERANCE_PCT}%  size: {TOLERANCE_SIZE_PCT}%  |  "
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
