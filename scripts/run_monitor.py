#!/usr/bin/env python3
"""
Cron Entry-Point: Position-Monitor + Heartbeat-Check.
Läuft nach run_execution.py.

Cron: */5 * * * * sleep 40 && python3 /root/apex-v2/scripts/run_monitor.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import run_migrations
from core.utils import log
from config.settings import HEARTBEAT_TTL_DAYS
from monitor.heartbeat import check_all_heartbeats, write_heartbeat, cleanup_old_heartbeats
from monitor.position_monitor import run_position_monitor


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_monitor] Start")

    # ── 1. Position-Monitor ───────────────────────────────────────────────────
    stats = run_position_monitor()
    log(f"[run_monitor] Positionen: checked={stats['checked']} open={stats['open']} "
        f"exits={stats['exits']} be_new={stats['be_applied_new']}")

    # ── 2. Heartbeat-Check ────────────────────────────────────────────────────
    alerts = check_all_heartbeats()
    if alerts:
        for a in alerts:
            log(f"[run_monitor] ALARM: {a['component']} — {a['reason']}")
        hb_status  = "warn"
        hb_message = f"alerts={len(alerts)}: " + ", ".join(a["component"] for a in alerts)
    else:
        hb_status  = "ok"
        hb_message = f"all_components_ok positions={stats['open']}"

    # ── 3. Heartbeat bereinigen ───────────────────────────────────────────────
    deleted = cleanup_old_heartbeats(HEARTBEAT_TTL_DAYS)
    if deleted:
        log(f"[run_monitor] Heartbeat-Cleanup: {deleted} alte Einträge gelöscht")

    latency = (time.monotonic() - t0) * 1000
    write_heartbeat("monitor", hb_status, hb_message, latency)
    log(f"[run_monitor] Fertig: {hb_message} ({latency:.0f}ms)")


if __name__ == "__main__":
    main()
