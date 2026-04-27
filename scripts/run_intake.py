#!/usr/bin/env python3
"""
Cron Entry-Point: Daten-Intake
Aufruf: */5 * * * * sleep 10 && python3 /root/apex-v2/scripts/run_intake.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations
from core.utils import log
from config.settings import INTAKE_MATRIX, CANDLE_TTL_DAYS
from execution.bitget_client import BitgetClient
from intake.market_data import run_intake, cleanup_old_candles


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "intake", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()  # idempotent, stellt sicher DB existiert
    t0 = time.monotonic()

    log("[run_intake] Start")
    client = BitgetClient(dry_run=True)  # Intake braucht keine Orders

    try:
        results = run_intake(client, INTAKE_MATRIX)

        errors  = [r for r in results if "error" in r]
        fetched = sum(r.get("fetched", 0) for r in results)
        inserted = sum(r.get("inserted", 0) for r in results)

        latency_ms = (time.monotonic() - t0) * 1000
        status  = "warn" if errors else "ok"
        message = (f"fetched={fetched} inserted={inserted} errors={len(errors)}"
                   + (f" | {errors[0].get('error','')[:100]}" if errors else ""))

        write_heartbeat(status, message, latency_ms)
        log(f"[run_intake] Fertig: {message} ({latency_ms:.0f}ms)")

    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        write_heartbeat("error", str(e)[:200], latency_ms)
        log(f"[run_intake] KRITISCHER FEHLER: {e}")
        sys.exit(1)

    # Täglicher Cleanup (läuft idempotent, löscht nichts wenn noch frisch)
    cleanup_old_candles(CANDLE_TTL_DAYS)


if __name__ == "__main__":
    main()
