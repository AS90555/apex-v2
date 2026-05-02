"""
Migration: Rescored micro_score für alle lab_discoveries-Einträge.

Neue Formel: 5-dimensionaler Composite-Score
  √PF × AvgR × (WR/50) × ln(n) × (1 / (1 + MaxDD/3)) × 10

Führt außerdem ein UPDATE auf lab_highscores.discovery_id durch,
damit es auf das Setup mit dem höchsten neuen Score zeigt.

WICHTIG: AutoLab-Daemon muss gestoppt sein, bevor dieses Script läuft.
"""

import math
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import get_connection


def new_score(pf, avg_r, wr, n, max_dd_r) -> float:
    if not n or n < 20 or not pf or pf <= 0 or not avg_r or avg_r <= 0 or not max_dd_r or max_dd_r <= 0:
        return 0.0
    dd_penalty = 1.0 / (1.0 + max_dd_r / 3.0)
    return round(math.sqrt(pf) * avg_r * (wr / 50.0) * math.log(max(n, 2)) * dd_penalty * 10, 2)


def main():
    conn = get_connection()

    rows = conn.execute(
        "SELECT id, pf_test, avg_r_test, wr_test, n_test, max_dd_r FROM lab_discoveries"
    ).fetchall()

    print(f"Starte Migration: {len(rows)} Discoveries")
    updated = 0
    zeroed  = 0

    for r in rows:
        disc_id, pf, avg_r, wr, n, max_dd = r
        score = new_score(pf or 0, avg_r or 0, wr or 0, n or 0, max_dd or 0)
        conn.execute("UPDATE lab_discoveries SET micro_score=? WHERE id=?", (score, disc_id))
        if score == 0.0:
            zeroed += 1
        else:
            updated += 1

    conn.commit()
    print(f"  Scores gesetzt: {updated} mit Wert > 0, {zeroed} auf 0.0 gesetzt")

    # lab_highscores.discovery_id korrigieren
    buckets = conn.execute(
        "SELECT strategy, asset, market_regime FROM lab_highscores"
    ).fetchall()

    fixed = 0
    for strategy, asset, regime in buckets:
        best = conn.execute(
            """SELECT id, pf_test, micro_score FROM lab_discoveries
               WHERE strategy=? AND asset=? AND market_regime=?
               ORDER BY micro_score DESC LIMIT 1""",
            (strategy, asset, regime),
        ).fetchone()
        if best:
            conn.execute(
                """UPDATE lab_highscores
                   SET discovery_id=?, best_pf=?, updated_at=datetime('now')
                   WHERE strategy=? AND asset=? AND market_regime=?""",
                (best[0], best[1], strategy, asset, regime),
            )
            fixed += 1

    conn.commit()
    conn.close()
    print(f"  lab_highscores: {fixed} Einträge korrigiert")
    print("Migration abgeschlossen.")


if __name__ == "__main__":
    main()
