#!/usr/bin/env python3
"""
Einmaliges Backfill-Skript: lädt historische Kerzen in 200er-Chunks.
Läuft vor dem ersten Backtest; danach übernimmt run_intake.py die Pflege.

Ziel: 90+ Tage 1h-Daten für ETH, SOL (KDT/VAA-Backtesting).
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.db import get_connection, run_migrations
from core.utils import log
from execution.bitget_client import BitgetClient

BACKFILL_MATRIX = {
    "ETH":  ["1h", "4h"],
    "SOL":  ["1h"],
    "AVAX": ["1h", "4h"],
}

INTERVAL_MS = {
    "1h":  3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

CHUNK = 200   # Bitget-Limit pro Request


def backfill(client, asset: str, interval: str, days: int = 100):
    interval_ms  = INTERVAL_MS[interval]
    now_ms       = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start = now_ms - days * 86_400_000

    conn         = get_connection()
    fetched_at   = datetime.now(timezone.utc).isoformat()

    # Ältesten vorhandenen Timestamp ermitteln
    row = conn.execute(
        "SELECT MIN(ts) FROM candles WHERE asset=? AND interval=?",
        (asset, interval),
    ).fetchone()
    oldest_in_db = row[0] if row and row[0] else now_ms

    if oldest_in_db <= target_start:
        log(f"[BACKFILL] {asset}/{interval}: bereits vollständig (älteste: "
            f"{datetime.fromtimestamp(oldest_in_db/1000, tz=timezone.utc).date()})")
        conn.close()
        return

    # Rückwärts in 200er-Chunks bis target_start
    chunk_end = oldest_in_db
    total_inserted = 0

    while chunk_end > target_start:
        chunk_start = chunk_end - CHUNK * interval_ms
        if chunk_start < target_start:
            chunk_start = target_start

        candles = client.get_candles(
            coin=asset, interval=interval, limit=CHUNK,
            start_time=chunk_start - 1,   # Bitget: start_time ist exklusiv
            end_time=chunk_end,
        )

        if not candles:
            log(f"[BACKFILL] {asset}/{interval}: keine Daten für Chunk → Stop")
            break

        # Stop wenn API nicht weiter zurückgeht (ältester Timestamp unverändert)
        if candles[0]["time"] >= chunk_end:
            log(f"[BACKFILL] {asset}/{interval}: API-Limit erreicht → Stop")
            break

        inserted = 0
        for c in candles:
            cur = conn.execute(
                """INSERT OR IGNORE INTO candles
                   (asset, interval, ts, open, high, low, close, volume, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (asset, interval, c["time"], c["open"], c["high"],
                 c["low"], c["close"], c["volume"], fetched_at),
            )
            inserted += cur.rowcount
        conn.commit()
        total_inserted += inserted

        oldest_fetched = candles[0]["time"]
        log(f"[BACKFILL] {asset}/{interval}: chunk ab "
            f"{datetime.fromtimestamp(oldest_fetched/1000, tz=timezone.utc).date()} "
            f"— {inserted}/{len(candles)} neu gespeichert")

        chunk_end = oldest_fetched
        time.sleep(0.25)   # Rate-Limit

    conn.close()
    log(f"[BACKFILL] {asset}/{interval}: fertig — {total_inserted} Kerzen gesamt")


def main():
    run_migrations()
    client = BitgetClient(dry_run=False)

    if not client.is_ready:
        log("[BACKFILL] BitgetClient nicht bereit — Credentials fehlen?")
        sys.exit(1)

    log("[BACKFILL] Starte historisches Backfill (100 Tage)")
    for asset, intervals in BACKFILL_MATRIX.items():
        for interval in intervals:
            backfill(client, asset, interval, days=100)

    log("[BACKFILL] Abgeschlossen — starte Feature-Berechnung")

    from scripts.run_features import main as run_features
    run_features()
    log("[BACKFILL] Features berechnet — Backtest kann gestartet werden")


if __name__ == "__main__":
    main()
