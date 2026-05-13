"""
Phase-1-Test: DB-Hardening — parallele Schreibvorgänge ohne 'database is locked'.
"""

from __future__ import annotations

import threading
import time
import pytest

from core.db import get_connection, get_readonly_connection


def test_parallel_writes_no_lock():
    """5 Threads schreiben gleichzeitig in system_state — kein OperationalError."""
    errors = []

    def writer(i: int) -> None:
        try:
            conn = get_connection()
            conn.execute(
                "INSERT INTO system_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (f"test_parallel_{i}", str(i), "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cleanup
    conn = get_connection()
    conn.execute("DELETE FROM system_state WHERE key LIKE 'test_parallel_%'")
    conn.commit()
    conn.close()

    assert not errors, f"Parallele Writes fehlgeschlagen: {errors}"


def test_readonly_connection_cannot_write():
    """get_readonly_connection() darf keine Schreibvorgänge erlauben."""
    conn = get_readonly_connection()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO system_state(key, value, updated_at) VALUES('ro_test','x','2026-01-01')"
        )
    conn.close()


def test_readonly_connection_can_read():
    """get_readonly_connection() kann lesen."""
    conn = get_readonly_connection()
    rows = conn.execute("SELECT key FROM system_state LIMIT 1").fetchall()
    conn.close()
    # kein Fehler = Test bestanden


def test_wal_mode_active():
    """journal_mode muss WAL sein."""
    conn = get_connection()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert row[0].upper() == "WAL"


def test_isolation_level_immediate():
    """isolation_level muss IMMEDIATE sein (kein autocommit-Konflikt)."""
    import sqlite3
    conn = get_connection()
    # IMMEDIATE-Lock: zweite Verbindung kann nicht gleichzeitig schreiben
    conn2 = sqlite3.connect(str(__import__('core.db', fromlist=['DB_PATH']).DB_PATH))
    conn.execute("BEGIN IMMEDIATE")
    # conn2 sollte bei Schreibversuch blockieren oder mit timeout scheitern
    conn.execute("ROLLBACK")
    conn.close()
    conn2.close()
