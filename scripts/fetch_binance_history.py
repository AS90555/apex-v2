#!/usr/bin/env python3
"""
Einmaliges Skript: lädt 2 Jahre 1h-Kerzen von Binance Futures (USDT-Perps)
via ccxt und injiziert sie in die candles-Tabelle.

Binance-Futures = hochkorrelierter Proxy für Bitget-Perps.
Kein API-Key nötig (öffentliche Endpoints).
Speichert unter asset='ETH'/'BTC'/'SOL' — kompatibel mit Backtest-Engine.
INSERT OR IGNORE: Bitget-Live-Daten werden nicht überschrieben.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt
from datetime import datetime, timezone, timedelta
from core.db import get_connection, run_migrations
from core.utils import log

ASSETS = {
    "ETH": "ETH/USDT:USDT",
    "BTC": "BTC/USDT:USDT",
    "SOL": "SOL/USDT:USDT",
}
INTERVAL  = "1h"
DAYS_BACK = 730   # 2 Jahre
CHUNK     = 1000  # Binance-Limit pro Request


def fetch_and_store(ex: ccxt.Exchange, asset: str, symbol: str, days: int):
    conn       = get_connection()
    fetched_at = datetime.now(timezone.utc).isoformat()
    now_ms     = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms   = now_ms - days * 86_400_000

    # Vorhandene Timestamps ermitteln → nur Lücken laden
    existing = set(
        r[0] for r in conn.execute(
            "SELECT ts FROM candles WHERE asset=? AND interval=?",
            (asset, INTERVAL),
        ).fetchall()
    )
    log(f"[BINANCE] {asset}: {len(existing)} Candles bereits in DB")

    since    = start_ms
    total    = 0
    requests = 0

    while since < now_ms:
        try:
            ohlcv = ex.fetch_ohlcv(symbol, INTERVAL, since=since, limit=CHUNK)
        except Exception as e:
            log(f"[BINANCE] {asset}: API-Fehler — {e}")
            time.sleep(5)
            continue

        if not ohlcv:
            break

        inserted = 0
        for bar in ohlcv:
            ts = bar[0]
            if ts in existing:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO candles
                   (asset, interval, ts, open, high, low, close, volume, fetched_at, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (asset, INTERVAL, ts,
                 bar[1], bar[2], bar[3], bar[4], bar[5],
                 fetched_at, "binance"),
            )
            if cur.rowcount:
                existing.add(ts)
                inserted += 1

        conn.commit()
        total    += inserted
        requests += 1
        since     = ohlcv[-1][0] + 3_600_000  # nächster Chunk

        dt_from = datetime.fromtimestamp(ohlcv[0][0]  / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        dt_to   = datetime.fromtimestamp(ohlcv[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        log(f"[BINANCE] {asset}: {dt_from} → {dt_to}  +{inserted} neu ({total} gesamt)")
        time.sleep(0.3)  # Rate-Limit

    conn.close()
    log(f"[BINANCE] {asset}: fertig — {total} neue Candles in {requests} Requests")
    return total


def main():
    run_migrations()
    ex = ccxt.binance({"options": {"defaultType": "future"}})

    log(f"[BINANCE] Starte Fetch: {list(ASSETS)} | {INTERVAL} | {DAYS_BACK} Tage")
    t0 = time.monotonic()

    for asset, symbol in ASSETS.items():
        fetch_and_store(ex, asset, symbol, DAYS_BACK)

    log(f"[BINANCE] Alle Assets geladen ({(time.monotonic()-t0):.0f}s) — starte Feature-Berechnung")

    from features.feature_agent import run_all_features
    matrix = {asset: [INTERVAL] for asset in ASSETS}
    run_all_features(matrix)

    log("[BINANCE] Features berechnet — Deep Backtest kann gestartet werden")
    log("[BINANCE] Befehl: python3 backtest/runner.py --all --days 730")


if __name__ == "__main__":
    main()
