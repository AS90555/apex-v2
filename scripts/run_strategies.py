#!/usr/bin/env python3
"""
Cron Entry-Point: Signal-Generierung aller Strategien.
Läuft nach run_intake.py + run_features.py.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations
from core.utils import log
from strategies.orb import ORBStrategy
from strategies.vaa import VAAStrategy
from strategies.kdt import KDTStrategy
from strategies.asian_fade import AsianFadeStrategy
from strategies.squeeze import SqueezeStrategy
from strategies.generic_deployed import load_deployed_strategies


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "strategies", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_strategies] Start")

    deployed = load_deployed_strategies()
    if deployed:
        log(f"[run_strategies] {len(deployed)} Deploy-Instanz(en) geladen: "
            + ", ".join(d.name for d in deployed))

    strategies = [SqueezeStrategy(), ORBStrategy(), VAAStrategy(), KDTStrategy(), AsianFadeStrategy()] + deployed
    total_signals = 0
    errors = []

    for strat in strategies:
        try:
            sigs = strat.run()
            total_signals += len(sigs)
            if not sigs and hasattr(strat, '_key'):
                log(f"[run_strategies] {strat.name}: kein Signal")
        except Exception as e:
            log(f"[run_strategies] FEHLER in {strat.name}: {e}")
            errors.append(f"{strat.name}: {str(e)[:100]}")

    latency = (time.monotonic() - t0) * 1000
    status  = "warn" if errors else "ok"
    message = f"signals={total_signals} errors={len(errors)}"
    if errors:
        message += " | " + "; ".join(errors)

    write_heartbeat(status, message, latency)
    log(f"[run_strategies] Fertig: {message} ({latency:.0f}ms)")


if __name__ == "__main__":
    main()
