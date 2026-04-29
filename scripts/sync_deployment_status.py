#!/usr/bin/env python3
"""
Einmaliges Migrations-Script: synchronisiert deployment_status in lab_discoveries
anhand der aktiven Einträge in active_deployments.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection

conn = get_connection()

rows = conn.execute(
    "SELECT id, strategy_key, asset, mode, discovery_id, active FROM active_deployments"
).fetchall()
print(f"active_deployments: {len(rows)} Einträge")
for r in rows:
    print(f"  ID={r[0]} {r[1]}/{r[2]} mode={r[3]} active={r[5]} discovery_id={r[4]}")

print()
updated = 0
skipped = 0
for row in rows:
    disc_id = row[4]  # discovery_id
    mode    = row[3]
    active  = row[5]
    if not disc_id:
        skipped += 1
        continue
    # Nur aktive Deployments setzen — inaktive bleiben 'lab'
    if active:
        status = "live" if mode == "live" else "dry"
    else:
        status = "lab"
    conn.execute(
        "UPDATE lab_discoveries SET deployment_status=? WHERE id=?",
        (status, disc_id),
    )
    updated += 1

conn.commit()

# Ergebnis anzeigen
result = conn.execute(
    """SELECT id, strategy, asset, deployment_status
       FROM lab_discoveries WHERE deployment_status != 'lab'
       ORDER BY id"""
).fetchall()
conn.close()

print(f"Sync abgeschlossen: {updated} lab_discoveries geprüft, {skipped} ohne discovery_id übersprungen")
print(f"\nlab_discoveries mit deployment_status != 'lab': {len(result)}")
for r in result:
    print(f"  ID={r[0]} {r[1]}/{r[2]} → {r[3]}")
