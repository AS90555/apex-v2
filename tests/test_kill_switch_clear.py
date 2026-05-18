"""
C.3 — Tests für den Kill-Switch Clear-Pfad und Audit-Log.

Prüft:
- set_kill_mode() schreibt Event in kill_switch_events
- clear_kill_mode() erfordert reason + cleared_by (Pflicht)
- Clear schreibt Event mit action='clear' in kill_switch_events
- Doppeltes Clear verhält sich sauber
- Leere reason/cleared_by werden abgelehnt
- is_hard_killed() reagiert korrekt nach Set und Clear
- get_kill_switch_events() liefert Audit-Trail
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


# ── Shared-Memory-DB (erlaubt mehrere Verbindungen zur selben In-Memory-DB) ──

_DB_URI = "file:ks_test?mode=memory&cache=shared"
_DDL = """
    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS kill_switch_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        action     TEXT NOT NULL,
        mode_from  TEXT,
        mode_to    TEXT NOT NULL,
        reason     TEXT NOT NULL,
        cleared_by TEXT,
        asset      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ks_ts ON kill_switch_events(ts DESC);
"""


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_URI, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db():
    """
    Hält eine Keeper-Verbindung offen (sonst verwirft SQLite die In-Memory-DB).
    _get_conn() gibt jeweils eine frische Verbindung zur selben shared DB zurück —
    jede kann normal geschlossen werden ohne Daten zu verlieren.
    """
    keeper = _open_conn()
    keeper.executescript(_DDL)
    keeper.commit()

    with patch("governance.kill_switch._get_conn", side_effect=_open_conn):
        yield keeper

    keeper.execute("DELETE FROM kill_switch_events")
    keeper.execute("DELETE FROM system_state")
    keeper.commit()
    keeper.close()


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _events(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM kill_switch_events ORDER BY id"
    ).fetchall()]


def _kill_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM system_state WHERE key='kill_mode'").fetchone()
    return row["value"] if row else "none"


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSetKillMode:
    def test_set_writes_audit_event(self, db):
        from governance.kill_switch import set_kill_mode
        set_kill_mode("hard", reason="TP2 fehlgeschlagen", asset="BTC")
        events = _events(db)
        assert len(events) == 1
        e = events[0]
        assert e["action"] == "set"
        assert e["mode_to"] == "hard"
        assert e["reason"] == "TP2 fehlgeschlagen"
        assert e["asset"] == "BTC"

    def test_set_lower_mode_ignored(self, db):
        from governance.kill_switch import set_kill_mode
        set_kill_mode("hard", reason="initial")
        set_kill_mode("soft", reason="attempt downgrade")
        assert _kill_mode(db) == "hard"
        assert len(_events(db)) == 1

    def test_set_higher_mode_succeeds(self, db):
        from governance.kill_switch import set_kill_mode
        set_kill_mode("soft", reason="soft start")
        set_kill_mode("manual", reason="telegram panic")
        assert _kill_mode(db) == "manual"
        events = _events(db)
        assert len(events) == 2
        assert events[1]["mode_from"] == "soft"
        assert events[1]["mode_to"] == "manual"

    def test_set_records_mode_from(self, db):
        from governance.kill_switch import set_kill_mode
        set_kill_mode("soft", reason="first")
        set_kill_mode("hard", reason="second")
        events = _events(db)
        assert events[0]["mode_from"] == "none"
        assert events[1]["mode_from"] == "soft"


class TestClearKillMode:
    def test_clear_requires_reason(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode
        set_kill_mode("hard", reason="test")
        with pytest.raises(ValueError, match="reason"):
            clear_kill_mode(reason="", cleared_by="admin")

    def test_clear_requires_cleared_by(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode
        set_kill_mode("hard", reason="test")
        with pytest.raises(ValueError, match="cleared_by"):
            clear_kill_mode(reason="alles OK", cleared_by="")

    def test_clear_sets_mode_to_none(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode
        set_kill_mode("hard", reason="bug")
        clear_kill_mode(reason="manuelle Prüfung OK", cleared_by="telegram:/panic_clear user=42")
        assert _kill_mode(db) == "none"

    def test_clear_writes_audit_event(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode
        set_kill_mode("manual", reason="panic")
        clear_kill_mode(reason="Lage normalisiert", cleared_by="operator@telegram")
        clear_ev = next(e for e in _events(db) if e["action"] == "clear")
        assert clear_ev["mode_from"] == "manual"
        assert clear_ev["mode_to"] == "none"
        assert clear_ev["cleared_by"] == "operator@telegram"
        assert clear_ev["reason"] == "Lage normalisiert"

    def test_double_clear_is_safe(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode
        set_kill_mode("hard", reason="test")
        clear_kill_mode(reason="erste Freigabe", cleared_by="admin")
        clear_kill_mode(reason="doppelter Clear", cleared_by="admin")
        assert _kill_mode(db) == "none"
        assert len([e for e in _events(db) if e["action"] == "clear"]) == 2

    def test_clear_when_already_none_logs_event(self, db):
        """Clear bei bereits none → kein Fehler, Event wird trotzdem geschrieben."""
        from governance.kill_switch import clear_kill_mode
        clear_kill_mode(reason="precautionary clear", cleared_by="admin")
        events = _events(db)
        assert len(events) == 1
        assert events[0]["mode_from"] == "none"
        assert events[0]["mode_to"] == "none"

    def test_whitespace_reason_rejected(self, db):
        from governance.kill_switch import clear_kill_mode
        with pytest.raises(ValueError):
            clear_kill_mode(reason="   ", cleared_by="admin")

    def test_whitespace_cleared_by_rejected(self, db):
        from governance.kill_switch import clear_kill_mode
        with pytest.raises(ValueError):
            clear_kill_mode(reason="ok", cleared_by="  \t ")


class TestIsHardKilled:
    def test_hard_mode_returns_true(self, db):
        from governance.kill_switch import set_kill_mode, is_hard_killed
        set_kill_mode("hard", reason="test")
        assert is_hard_killed() is True

    def test_manual_mode_returns_true(self, db):
        from governance.kill_switch import set_kill_mode, is_hard_killed
        set_kill_mode("manual", reason="test")
        assert is_hard_killed() is True

    def test_soft_mode_returns_false(self, db):
        from governance.kill_switch import set_kill_mode, is_hard_killed
        set_kill_mode("soft", reason="test")
        assert is_hard_killed() is False

    def test_after_clear_returns_false(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode, is_hard_killed
        set_kill_mode("hard", reason="test")
        assert is_hard_killed() is True
        clear_kill_mode(reason="Situation gelöst", cleared_by="admin")
        assert is_hard_killed() is False


class TestGetKillSwitchEvents:
    def test_returns_events_newest_first(self, db):
        from governance.kill_switch import set_kill_mode, clear_kill_mode, get_kill_switch_events
        set_kill_mode("soft", reason="erster")
        set_kill_mode("hard", reason="zweiter")
        clear_kill_mode(reason="dritter", cleared_by="admin")
        events = get_kill_switch_events(limit=10)
        assert len(events) == 3
        assert events[0]["action"] == "clear"
        assert events[2]["mode_to"] == "soft"

    def test_limit_respected(self, db):
        from governance.kill_switch import set_kill_mode, get_kill_switch_events
        conn = _open_conn()
        for i in range(5):
            conn.execute(
                "INSERT INTO kill_switch_events (ts, action, mode_from, mode_to, reason) "
                "VALUES (datetime('now'), 'set', 'none', 'soft', ?)",
                (f"event {i}",),
            )
        conn.commit()
        conn.close()
        events = get_kill_switch_events(limit=3)
        assert len(events) == 3
