#!/usr/bin/env python3
"""
Cron Entry-Point: Execution aller approved Signale.
Läuft nach run_governance.py.

Cron: */5 * * * * sleep 30 && python3 /root/apex-v2/scripts/run_execution.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations
from core.models import Signal
from core.utils import log
from execution.executor import Executor


def _load_approved_signals(conn) -> list[Signal]:
    """Lädt alle approved Signale, max. 1 pro Asset (FIFO)."""
    rows = conn.execute(
        """SELECT id, created_at, strategy, asset, direction, entry_price,
                  stop_loss, take_profit_1, take_profit_2, size, risk_usd,
                  session, status, mode
           FROM signals
           WHERE status='approved'
           ORDER BY created_at ASC""",
    ).fetchall()

    seen_assets: set[str] = set()
    signals: list[Signal] = []
    for r in rows:
        asset = r[3]
        if asset in seen_assets:
            continue   # max. 1 Trade pro Asset pro Zyklus
        seen_assets.add(asset)

        s = Signal(
            strategy=r[2], asset=asset, direction=r[4],
            entry_price=r[5], stop_loss=r[6],
            take_profit_1=r[7], take_profit_2=r[8],
            size=r[9], risk_usd=r[10],
            session=r[11], status=r[12], mode=r[13],
            created_at=r[1],
        )
        s.id = r[0]
        signals.append(s)
    return signals


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "executor", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_execution] Start")

    conn = get_connection()
    signals = _load_approved_signals(conn)
    conn.close()

    log(f"[run_execution] {len(signals)} approved Signal(e) — shadow-only werden simuliert")

    executor = Executor()
    executed = 0
    skipped  = 0
    errors   = []

    for signal in signals:
        try:
            trade = executor.execute(signal)
            if trade:
                executed += 1
                log(f"[run_execution] ✓ Trade #{trade.id}: {signal.asset} {signal.direction.upper()} "
                    f"mode={signal.mode} order_id={trade.order_id}")
            else:
                skipped += 1
        except Exception as e:
            log(f"[run_execution] FEHLER bei Signal #{signal.id}: {e}")
            errors.append(f"#{signal.id}: {str(e)[:80]}")

    latency = (time.monotonic() - t0) * 1000
    status  = "warn" if errors else "ok"
    message = (f"approved={len(signals)} executed={executed} skipped={skipped} errors={len(errors)}")
    if errors:
        message += " | " + "; ".join(errors)

    write_heartbeat(status, message, latency)
    log(f"[run_execution] Fertig: {message} ({latency:.0f}ms)")


if __name__ == "__main__":
    main()
