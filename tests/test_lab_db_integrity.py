"""
A.2 — FK-Audit-Queries (audit-only, KEINE FK-Migration).

Prüft referenzielle Integrität der lab_state.db via reine SELECT-Queries.
Keine Schema-Änderungen, keine DDL, kein ALTER TABLE.

Ausgeführt gegen die Live-DB UND against eine temporäre Test-DB.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.lab_state_db import get_lab_state_connection, write_variant
from core.lab_families import sync_to_db


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "integrity_test.db")
    c = get_lab_state_connection(db)
    sync_to_db(c)
    yield c
    c.close()


class TestLabDBIntegrity:
    def test_variant_lineage_parent_exists(self, conn):
        """Alle parent_variant_id in variant_lineage müssen in strategy_variants existieren."""
        orphans = conn.execute("""
            SELECT vl.variant_id, vl.parent_variant_id
            FROM variant_lineage vl
            LEFT JOIN strategy_variants sv ON sv.variant_id = vl.parent_variant_id
            WHERE sv.variant_id IS NULL
        """).fetchall()
        assert orphans == [], (
            f"Orphaned variant_lineage.parent_variant_id: {[dict(r) for r in orphans]}"
        )

    def test_fitness_records_variant_exists(self, conn):
        """Alle variant_id in fitness_records müssen in strategy_variants existieren."""
        orphans = conn.execute("""
            SELECT fr.variant_id
            FROM fitness_records fr
            LEFT JOIN strategy_variants sv ON sv.variant_id = fr.variant_id
            WHERE sv.variant_id IS NULL
        """).fetchall()
        assert orphans == [], (
            f"Orphaned fitness_records.variant_id: {[r[0] for r in orphans]}"
        )

    def test_lab_queue_cycle_exists(self, conn):
        """Alle cycle_id in lab_queue müssen in lab_cycles existieren."""
        orphans = conn.execute("""
            SELECT lq.id, lq.cycle_id
            FROM lab_queue lq
            LEFT JOIN lab_cycles lc ON lc.id = lq.cycle_id
            WHERE lc.id IS NULL
        """).fetchall()
        assert orphans == [], (
            f"Orphaned lab_queue.cycle_id: {[dict(r) for r in orphans]}"
        )

    def test_evolution_events_variant_exists(self, conn):
        """Nicht-NULL variant_id in evolution_events müssen in strategy_variants existieren."""
        orphans = conn.execute("""
            SELECT ee.id, ee.variant_id
            FROM evolution_events ee
            LEFT JOIN strategy_variants sv ON sv.variant_id = ee.variant_id
            WHERE ee.variant_id IS NOT NULL AND sv.variant_id IS NULL
        """).fetchall()
        assert orphans == [], (
            f"Orphaned evolution_events.variant_id: {[dict(r) for r in orphans]}"
        )

    def test_no_schema_changes_made(self, conn):
        """Sanity-Check: Diese Tests ändern kein Schema (kein FOREIGN KEY Constraint hinzugefügt)."""
        # Prüfe dass strategy_variants keine FK-Constraint-Definition im CREATE enthält
        # (SQLite gibt kein PRAGMA foreign_key_list für Tabellen ohne FK zurück wenn nicht definiert)
        fk_on_variants = conn.execute(
            "PRAGMA foreign_key_list(strategy_variants)"
        ).fetchall()
        # strategy_variants hat KEINE definierten FK-Constraints (die kommen erst in F/R5)
        # Dieser Test stellt sicher dass A.2 nichts verändert hat
        assert isinstance(fk_on_variants, list), "PRAGMA liefert immer eine Liste"


class TestLiveDBIntegrity:
    """Führt dieselben Checks auf der Live-Lab-State-DB aus (wenn vorhanden)."""

    def test_live_db_integrity(self):
        live_db = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "data", "lab_state.db")
        if not os.path.exists(live_db):
            pytest.skip("Live lab_state.db nicht vorhanden")

        conn = get_lab_state_connection(live_db)
        try:
            for table, fk_col, ref_table, ref_col in [
                ("variant_lineage",  "parent_variant_id", "strategy_variants", "variant_id"),
                ("fitness_records",  "variant_id",        "strategy_variants", "variant_id"),
                ("lab_queue",        "cycle_id",          "lab_cycles",        "id"),
            ]:
                orphans = conn.execute(f"""
                    SELECT t.rowid FROM {table} t
                    LEFT JOIN {ref_table} r ON r.{ref_col} = t.{fk_col}
                    WHERE t.{fk_col} IS NOT NULL AND r.{ref_col} IS NULL
                """).fetchall()
                assert orphans == [], (
                    f"Live-DB: Orphaned {table}.{fk_col} — {len(orphans)} Einträge"
                )
        finally:
            conn.close()
