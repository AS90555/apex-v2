#!/usr/bin/env python3
"""
Cron Entry-Point: Governance-Gate für alle pending Signale.
Läuft nach run_strategies.py.

Cron: */5 * * * * sleep 25 && python3 /root/apex-v2/scripts/run_governance.py
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations
from core.models import Signal
from core.state import get_daily_pnl, set_daily_pnl
from core.utils import log
from governance.gate import GovernanceGate
from governance.checks import (
    SignalExpiryCheck,
    DrawdownKillCheck,
    DailyDrawdownCheck,
    RegimeCheck,
    SizingSanityCheck,
    PositionOpenCheck,
    SessionTradedCheck,
)


def _load_pending_signals(conn) -> list[Signal]:
    rows = conn.execute(
        """SELECT id, created_at, strategy, asset, direction, entry_price,
                  stop_loss, take_profit_1, take_profit_2, size, risk_usd,
                  session, status, mode
           FROM signals WHERE status='pending'
           ORDER BY created_at ASC""",
    ).fetchall()
    signals = []
    for r in rows:
        s = Signal(
            strategy=r[2], asset=r[3], direction=r[4],
            entry_price=r[5], stop_loss=r[6],
            take_profit_1=r[7], take_profit_2=r[8],
            size=r[9], risk_usd=r[10],
            session=r[11], status=r[12], mode=r[13],
            created_at=r[1],
        )
        s.id = r[0]
        signals.append(s)
    return signals


def _write_governance_log(conn, signal_id: int, decision: str, reason: str, checks: dict):
    conn.execute(
        """INSERT INTO governance_log (signal_id, ts, decision, reason, checks_json)
           VALUES (?,?,?,?,?)""",
        (signal_id, datetime.now(timezone.utc).isoformat(),
         decision, reason, json.dumps(checks)),
    )


def _update_signal_status(conn, signal_id: int, status: str, reason: str = None):
    conn.execute(
        """UPDATE signals SET status=?, reject_reason=?, governance_ts=?
           WHERE id=?""",
        (status, reason, datetime.now(timezone.utc).isoformat(), signal_id),
    )


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "governance", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_governance] Start")

    # Daily-PnL-Reset — unabhängig von Trade-Exits, läuft jeden Zyklus
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_pnl()
    if daily.get("date") != today:
        set_daily_pnl(today, 0.0, 0.0, 0)
        log(f"[run_governance] Daily-PnL Reset für {today}")

    gate = GovernanceGate([
        SignalExpiryCheck(),
        DrawdownKillCheck(),
        DailyDrawdownCheck(),
        RegimeCheck(),
        SizingSanityCheck(),
        PositionOpenCheck(),
        SessionTradedCheck(),
    ])

    conn = get_connection()
    signals = _load_pending_signals(conn)
    log(f"[run_governance] {len(signals)} pending Signal(e) gefunden")

    approved_count = 0
    rejected_count = 0
    expired_count  = 0

    for signal in signals:
        # Shadow-Signale: Gate durchlaufen zum Logging, aber nie approved setzen
        passed, reason, checks = gate.evaluate(signal)

        if not passed:
            # expired vs. rejected unterscheiden
            final_status = "expired" if reason.startswith("expired:") else "rejected"
            _update_signal_status(conn, signal.id, final_status, reason)
            _write_governance_log(conn, signal.id, final_status, reason, checks)
            log(f"[run_governance] Signal #{signal.id} {signal.strategy}/{signal.asset} → {final_status}: {reason}")
            if final_status == "expired":
                expired_count += 1
            else:
                rejected_count += 1
        else:
            if signal.mode == "shadow":
                # Shadow: technisch getrennter Status — Executor lädt NUR 'approved'
                _update_signal_status(conn, signal.id, "approved_shadow", None)
                _write_governance_log(conn, signal.id, "approved_shadow", reason, checks)
                log(f"[run_governance] Signal #{signal.id} {signal.strategy}/{signal.asset} → approved_shadow (Audit only, kein Trade)")
            else:
                _update_signal_status(conn, signal.id, "approved", None)
                _write_governance_log(conn, signal.id, "approved", reason, checks)
                log(f"[run_governance] Signal #{signal.id} {signal.strategy}/{signal.asset} → approved [{signal.mode}]")
            approved_count += 1

    conn.commit()
    conn.close()

    latency = (time.monotonic() - t0) * 1000
    message = (f"processed={len(signals)} approved={approved_count} "
               f"rejected={rejected_count} expired={expired_count}")
    write_heartbeat("ok", message, latency)
    log(f"[run_governance] Fertig: {message} ({latency:.0f}ms)")


if __name__ == "__main__":
    main()
