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

from core.db import run_migrations
from core.utils import log

# Lazy-Import der main()-Funktionen um Ladezeit zu minimieren
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
    # (Name,         Funktion,          abort_on_fail)
    ("intake",       _step_intake,       True),
    ("features",     _step_features,     True),
    ("strategies",   _step_strategies,   False),
    ("governance",   _step_governance,   False),
    ("executor",     _step_execution,    False),
    ("monitor",      _step_monitor,      False),
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
