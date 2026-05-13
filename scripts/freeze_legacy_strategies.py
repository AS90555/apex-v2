"""
Phase-1-Oneshot: Legacy-Strategien einfrieren.

Setzt deployment_status='frozen' für alle lab_discoveries, die noch kein
framework_version='v6' haben (Spalte wird erst in Phase 2 angelegt).

Für active_deployments: mode wird auf 'shadow' gesetzt und 'note' um
'[FROZEN-v6-Phase1]' ergänzt, damit der DMS und der Governance-Gate
keinen Live-Traffic von Legacy-Strategien verarbeiten.

Ausführung:
    python scripts/freeze_legacy_strategies.py          # Dry-Run (zeigt Änderungen)
    python scripts/freeze_legacy_strategies.py --apply  # schreibt in DB
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log


def main(apply: bool) -> None:
    conn = get_connection()

    # lab_discoveries: alle die noch nicht 'frozen' sind
    ld_rows = conn.execute(
        """SELECT id, strategy, asset, deployment_status
           FROM lab_discoveries
           WHERE deployment_status NOT IN ('frozen')
             AND deployment_status IS NOT NULL"""
    ).fetchall()

    log(f"[FREEZE] {len(ld_rows)} lab_discoveries zum Einfrieren")
    for r in ld_rows:
        log(f"  LD #{r['id']} {r['strategy']}/{r['asset']} ({r['deployment_status']}) → frozen")

    # active_deployments: alle aktiven die NICHT schon shadow sind
    ad_rows = conn.execute(
        """SELECT id, strategy_key, asset, mode, note
           FROM active_deployments
           WHERE active=1 AND mode NOT IN ('shadow')"""
    ).fetchall()

    log(f"[FREEZE] {len(ad_rows)} active_deployments auf shadow setzen")
    for r in ad_rows:
        log(f"  AD #{r['id']} {r['strategy_key']}/{r['asset']} ({r['mode']}) → shadow")

    if not apply:
        log("[FREEZE] Dry-Run — keine Änderungen geschrieben (--apply fehlt)")
        conn.close()
        return

    if ld_rows:
        conn.execute(
            "UPDATE lab_discoveries SET deployment_status='frozen' "
            "WHERE deployment_status NOT IN ('frozen') AND deployment_status IS NOT NULL"
        )

    for r in ad_rows:
        old_note = r["note"] or ""
        new_note = (old_note + " [FROZEN-v6-Phase1]").strip()
        conn.execute(
            "UPDATE active_deployments SET mode='shadow', note=? WHERE id=?",
            (new_note, r["id"]),
        )

    conn.commit()
    conn.close()
    log(f"[FREEZE] Fertig — {len(ld_rows)} Discoveries + {len(ad_rows)} Deployments eingefroren")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    main(apply=apply)
