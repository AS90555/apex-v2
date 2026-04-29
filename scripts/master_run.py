#!/usr/bin/env python3
"""
Master-Orchestrator: ersetzt 6 einzelne Cron-Jobs mit sleep-Hacks.

Ein einziger Cron-Job alle 5 Minuten. Module werden sequenziell als
Funktionsaufrufe ausgeführt — keine Timing-Annahmen, keine Race-Conditions.

Fehler-Strategie:
  - intake/features: abort_on_fail=True (keine frischen Daten → Rest sinnlos)
  - strategies/governance/executor: abort_on_fail=False (Teilausfall tolerierbar)
  - monitor: läuft immer als letztes

Cron (ersetzt alle 6 sleep-Jobs):
  */5 * * * *  cd /root/apex-v2 && python3 scripts/master_run.py >> logs/master.log 2>&1
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import run_migrations, get_connection
from core.utils import log


def _step_processing_recovery():
    """Bereinigt Signale die zu lange in 'processing' hängen (Stale-Threshold: 5 Minuten).
    Unterscheidet order-sent vs. no-order für unterschiedliche reject_reason."""
    conn = get_connection()
    stale = conn.execute(
        """SELECT id, order_id FROM signals WHERE status='processing'
           AND created_at < datetime('now', '-5 minutes')"""
    ).fetchall()
    for row in stale:
        sig_id, order_id = row[0], row[1]
        reason = "stuck_processing_order_sent" if order_id else "stuck_processing_no_order"
        conn.execute(
            "UPDATE signals SET status='failed', reject_reason=? WHERE id=?",
            (reason, sig_id),
        )
        if order_id:
            log(f"[master_run] ⚠️ ACHTUNG Signal #{sig_id}: order_id={order_id} bereits gesendet — Position-Monitor muss prüfen")
        else:
            log(f"[master_run] Signal #{sig_id}: stuck processing → failed (kein Order)")
    if stale:
        log(f"[master_run] Processing-Recovery: {len(stale)} Signal(e) bereinigt")
        conn.commit()
    conn.close()


# Lazy-Import der main()-Funktionen um Ladezeit zu minimieren
def _step_cleanup():
    """Bereinigt alte Daten — läuft einmal täglich via system_state['last_cleanup_date']."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection()
    last = conn.execute(
        "SELECT value FROM system_state WHERE key='last_cleanup_date'"
    ).fetchone()
    if last and last[0] == today:
        conn.close()
        return
    log(f"[master_run] Cleanup für {today}")
    conn.execute("DELETE FROM candles    WHERE ts < (strftime('%s','now') - 30*86400) * 1000")
    conn.execute("DELETE FROM features   WHERE ts < (strftime('%s','now') - 30*86400) * 1000")
    conn.execute("DELETE FROM heartbeats WHERE ts < datetime('now', '-7 days')")
    conn.execute(
        """DELETE FROM signals WHERE status IN ('rejected','expired','failed')
           AND created_at < datetime('now', '-7 days')"""
    )
    conn.execute(
        """INSERT INTO system_state(key, value, updated_at) VALUES('last_cleanup_date', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (today, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    log("[master_run] Cleanup abgeschlossen")


def _step_intake():
    from scripts.run_intake import main; main()

def _step_features():
    from scripts.run_features import main; main()

def _step_strategies():
    from scripts.run_strategies import main; main()

def _step_governance():
    from scripts.run_governance import main; main()

def _step_execution():
    from scripts.run_execution import main; main()

def _step_monitor():
    from scripts.run_monitor import main; main()


PIPELINE = [
    # (Name,                  Funktion,                    abort_on_fail)
    ("processing_recovery",   _step_processing_recovery,   False),
    ("intake",                _step_intake,                True),
    ("features",     _step_features,     True),
    ("strategies",   _step_strategies,   False),
    ("governance",   _step_governance,   False),
    ("executor",     _step_execution,    False),
    ("monitor",      _step_monitor,      False),
    ("cleanup",      _step_cleanup,      False),
]


def main():
    run_migrations()
    t_total = time.monotonic()
    log("[master_run] ─── Pipeline Start ───────────────────────────────")

    aborted_at = None
    for name, fn, abort_on_fail in PIPELINE:
        # Monitor immer ausführen, auch nach Abbruch
        if aborted_at and name != "monitor":
            log(f"[master_run] {name}: übersprungen (Abbruch nach '{aborted_at}')")
            continue

        t_step = time.monotonic()
        try:
            fn()
            elapsed = (time.monotonic() - t_step) * 1000
            log(f"[master_run] {name}: OK ({elapsed:.0f}ms)")
        except Exception as e:
            elapsed = (time.monotonic() - t_step) * 1000
            log(f"[master_run] {name}: FEHLER — {e} ({elapsed:.0f}ms)")
            if abort_on_fail and not aborted_at:
                aborted_at = name
                log(f"[master_run] Pipeline abgebrochen nach '{name}' (abort_on_fail)")

    total = (time.monotonic() - t_total) * 1000
    status = f"aborted_at={aborted_at}" if aborted_at else "ok"
    log(f"[master_run] ─── Pipeline Fertig: {status} ({total:.0f}ms) ───")


if __name__ == "__main__":
    main()
