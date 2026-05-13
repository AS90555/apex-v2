"""
Isolierter Migrations-Test (v7 Phase 5).

Kopiert die Live-DB nach /tmp, führt run_migrations() aus, prüft alle
erwarteten Spalten/Indices/Tabellen. Berührt data/apex_v2.db NICHT.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LIVE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "apex_v2.db")

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "lab_discoveries": ["lab_config_hash", "composite_weights_hash", "framework_version"],
    "trades": ["signal_to_fill_ms", "slippage_bps"],
    "signals": ["signal_key", "mode"],
}

EXPECTED_TABLES = [
    "lab_discoveries", "trades", "signals", "candles", "heartbeats",
    "asset_execution_calibration",
]

EXPECTED_INDICES = [
    "idx_lab_disc_idempotent",
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _indices(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


def run(src: str = LIVE_DB) -> None:
    from core.db import run_migrations

    if not os.path.exists(src):
        print(f"[MigTest] SKIP — Live-DB nicht gefunden: {src}")
        return

    tmp = f"/tmp/apex_v2_migtest_{uuid4().hex}.db"
    try:
        shutil.copy2(src, tmp)
        for ext in ("-wal", "-shm"):
            if os.path.exists(src + ext):
                shutil.copy2(src + ext, tmp + ext)

        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        tables = _tables(conn)
        for table in EXPECTED_TABLES:
            assert table in tables, f"Tabelle '{table}' fehlt nach Migration"

        for table, cols in EXPECTED_COLUMNS.items():
            if table not in tables:
                continue
            existing = _columns(conn, table)
            for col in cols:
                assert col in existing, f"Spalte '{col}' fehlt in {table}"

        indices = _indices(conn)
        for idx in EXPECTED_INDICES:
            assert idx in indices, f"Index '{idx}' fehlt"

        # Smoke: 100 zufällige Rows aus zentralen Tabellen
        for table in ["lab_discoveries", "trades", "signals", "candles"]:
            if table in tables:
                conn.execute(f"SELECT * FROM {table} LIMIT 100").fetchall()

        conn.close()
        print(f"[MigTest] OK — alle Checks bestanden ({tmp})")

    finally:
        for path in [tmp, tmp + "-wal", tmp + "-shm"]:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    run()
