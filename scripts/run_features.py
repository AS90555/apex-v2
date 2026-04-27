#!/usr/bin/env python3
"""
Cron Entry-Point: Feature-Berechnung
Läuft direkt nach run_intake.py.
Aufruf: */5 * * * * sleep 20 && python3 /root/apex-v2/scripts/run_features.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations
from core.utils import log
from config.settings import INTAKE_MATRIX
from features.feature_agent import run_all_features


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "features", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_features] Start")

    try:
        summary  = run_all_features(INTAKE_MATRIX)
        computed = sum(v for v in summary.values() if v > 0)
        latency  = (time.monotonic() - t0) * 1000
        message  = f"features_computed={computed} keys={len(summary)}"
        write_heartbeat("ok", message, latency)
        log(f"[run_features] Fertig: {message} ({latency:.0f}ms)")

    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        write_heartbeat("error", str(e)[:200], latency)
        log(f"[run_features] KRITISCHER FEHLER: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
