"""
P2.3 — Reconciler Ghost-Heal Tests.

Verifiziert RECONCILER_AUTO_HEAL_GHOST=False (Default) und =True:

Flag OFF (Standard):
  - Ghost-Trade: nur Alert + reconcile_required=1, kein Status-Wechsel
  - Size-Mismatch: nur Alert + reconcile_required=1, keine DB-Korrektur

Flag ON (explizit aktiviert):
  - Ghost-Trade: status='ghost_closed' + Audit-Log-Eintrag
  - Size-Mismatch: DB-Size auf Exchange-Size korrigiert + Audit-Log-Eintrag
  - Audit-Eintrag enthält Asset + Action + Detail (nie still)

Invarianten:
  - RECONCILER_AUTO_HEAL_GHOST=False ist der Import-Default
  - Phantom-Positionen (Exchange hat, DB nicht) sind NICHT von Auto-Heal betroffen
    — Hard-Kill bleibt unverändert aktiv
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# DB-Fixture
# ══════════════════════════════════════════════════════════════════════════════

def _db_path(tmp_path, name: str = "test_reconcile.db") -> str:
    return str(tmp_path / name)


def _setup_db(db: str) -> None:
    """Erzeugt Schema in der DB-Datei."""
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_key TEXT, asset TEXT, side TEXT,
            size REAL DEFAULT 0.01, status TEXT DEFAULT 'executed',
            reconcile_required INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS execution_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER, cl_ord_id TEXT,
            state_from TEXT, state_to TEXT,
            payload_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, component TEXT, status TEXT,
            message TEXT, latency_ms REAL
        );
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        );
    """)
    conn.commit()
    conn.close()


def _open(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_trade(db: str, asset: str, size: float = 0.01,
                  status: str = "executed") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO trades (asset, size, status, strategy_key, side) VALUES (?,?,?,?,?)",
        (asset, size, status, "donchian", "long"),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def _run_reconcile(db: str, exchange_positions: dict, auto_heal: bool):
    """Ruft reconcile_once() mit gepatchter DB-Connection und Flag."""
    import scripts.run_reconciliation as rec_mod

    mock_client = MagicMock()
    mock_client.get_positions.return_value = [
        {"symbol": f"{asset}USDT_UMCBL", "total": str(size)}
        for asset, size in exchange_positions.items()
    ]
    mock_client.get_open_orders.return_value = []

    def _fresh_conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    with patch("scripts.run_reconciliation.get_connection", side_effect=_fresh_conn), \
         patch("scripts.run_reconciliation.RECONCILER_AUTO_HEAL_GHOST", auto_heal), \
         patch("scripts.run_reconciliation._send_telegram") as mock_tg, \
         patch("execution.bitget_client.BitgetClient", return_value=mock_client):
        result = rec_mod.reconcile_once()

    return result, mock_tg


# ══════════════════════════════════════════════════════════════════════════════
# Default OFF — kein automatisches Healen
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoHealFlagDefault:
    def test_default_is_false(self):
        """RECONCILER_AUTO_HEAL_GHOST muss False sein (Import-Default)."""
        from config.settings import RECONCILER_AUTO_HEAL_GHOST
        assert RECONCILER_AUTO_HEAL_GHOST is False

    def test_ghost_flag_off_only_alert_no_status_change(self, tmp_path):
        """Flag OFF: Ghost-Trade → nur Alert, status bleibt 'executed'."""
        db = _db_path(tmp_path, "ghost_off.db")
        _setup_db(db)
        _insert_trade(db, "BTC", size=0.01, status="executed")

        result, mock_tg = _run_reconcile(db, exchange_positions={}, auto_heal=False)

        assert result["alerts"] >= 1
        conn = _open(db)
        row = conn.execute("SELECT status FROM trades WHERE asset='BTC'").fetchone()
        assert row["status"] == "executed", \
            f"Flag OFF: Status darf nicht geändert werden, ist: {row['status']}"
        row2 = conn.execute("SELECT reconcile_required FROM trades WHERE asset='BTC'").fetchone()
        assert row2["reconcile_required"] == 1
        audit = conn.execute(
            "SELECT * FROM execution_audit_log WHERE state_to='healed'"
        ).fetchall()
        conn.close()
        assert len(audit) == 0, "Flag OFF: kein Audit-Heal-Eintrag erwartet"

    def test_size_mismatch_flag_off_only_alert_no_db_correction(self, tmp_path):
        """Flag OFF: Size-Mismatch → nur Alert, DB-Size bleibt unverändert."""
        db = _db_path(tmp_path, "size_off.db")
        _setup_db(db)
        _insert_trade(db, "ETH", size=1.0, status="executed")

        result, mock_tg = _run_reconcile(db, exchange_positions={"ETH": 0.5}, auto_heal=False)

        assert result["alerts"] >= 1
        conn = _open(db)
        row = conn.execute("SELECT size FROM trades WHERE asset='ETH'").fetchone()
        conn.close()
        assert abs(row["size"] - 1.0) < 1e-6, \
            f"Flag OFF: DB-Size darf nicht verändert werden, ist: {row['size']}"
        conn2 = _open(db)
        audit = conn2.execute(
            "SELECT * FROM execution_audit_log WHERE state_to='healed'"
        ).fetchall()
        conn2.close()
        assert len(audit) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Flag ON — kontrollierte, auditierte Mutation
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoHealFlagOn:
    def test_ghost_heal_sets_ghost_closed_and_audit(self, tmp_path):
        """Flag ON: Ghost-Trade → status='ghost_closed' + Audit-Eintrag."""
        db = _db_path(tmp_path, "ghost_on.db")
        _setup_db(db)
        _insert_trade(db, "SOL", size=5.0, status="executed")

        result, mock_tg = _run_reconcile(db, exchange_positions={}, auto_heal=True)

        assert result["alerts"] >= 1
        conn = _open(db)
        row = conn.execute("SELECT status, reconcile_required FROM trades WHERE asset='SOL'").fetchone()
        assert row["status"] == "ghost_closed", \
            f"Flag ON: status muss 'ghost_closed' sein, ist: {row['status']}"
        assert row["reconcile_required"] == 1
        audit = conn.execute(
            "SELECT cl_ord_id, state_from, payload_json FROM execution_audit_log "
            "WHERE state_to='healed'"
        ).fetchall()
        conn.close()
        assert len(audit) >= 1
        assert any("SOL" in a["cl_ord_id"] for a in audit), \
            "Audit-Eintrag muss Asset enthalten"
        assert any("ghost_heal" in a["state_from"] for a in audit)
        tg_calls = [str(c) for c in mock_tg.call_args_list]
        assert any("HEALED" in s or "ghost_closed" in s for s in tg_calls), \
            "Telegram-Alert muss auf Heal hinweisen"

    def test_size_mismatch_heal_corrects_db_size_and_audit(self, tmp_path):
        """Flag ON: Size-Mismatch → DB-Size auf Exchange-Size korrigiert + Audit."""
        db = _db_path(tmp_path, "size_on.db")
        _setup_db(db)
        _insert_trade(db, "BTC", size=0.02, status="executed")

        result, mock_tg = _run_reconcile(db, exchange_positions={"BTC": 0.01}, auto_heal=True)

        assert result["alerts"] >= 1
        conn = _open(db)
        row = conn.execute("SELECT size FROM trades WHERE asset='BTC'").fetchone()
        audit = conn.execute(
            "SELECT state_from, payload_json FROM execution_audit_log WHERE state_to='healed'"
        ).fetchall()
        conn.close()
        assert abs(row["size"] - 0.01) < 1e-6, \
            f"Flag ON: DB-Size muss auf Exchange-Size 0.01 korrigiert sein, ist: {row['size']}"
        assert len(audit) >= 1
        assert any("size_mismatch_heal" in a["state_from"] for a in audit)
        assert any("0.02" in (a["payload_json"] or "") or "0.01" in (a["payload_json"] or "")
                   for a in audit)

    def test_phantom_unaffected_by_heal_flag(self, tmp_path):
        """Phantom-Position (Exchange hat, DB nicht) → Hard-Kill, Flag hat keinen Einfluss."""
        db = _db_path(tmp_path, "phantom.db")
        _setup_db(db)

        result_off, _ = _run_reconcile(db, exchange_positions={"XRP": 100.0}, auto_heal=False)
        assert result_off["hard_kills"] >= 1

        # Kill-State zurücksetzen für zweiten Run
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM system_state")
        conn.commit()
        conn.close()

        result_on, _ = _run_reconcile(db, exchange_positions={"XRP": 100.0}, auto_heal=True)
        assert result_on["hard_kills"] >= 1, \
            "Phantom → Hard-Kill muss unabhängig vom Heal-Flag feuern"

    def test_heal_audit_never_silent(self, tmp_path):
        """Jede Heal-Mutation hat einen Audit-Eintrag — nie stille DB-Änderung."""
        db = _db_path(tmp_path, "audit.db")
        _setup_db(db)
        _insert_trade(db, "LINK", size=10.0, status="executed")

        _run_reconcile(db, exchange_positions={}, auto_heal=True)

        conn = _open(db)
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM execution_audit_log WHERE state_to='healed'"
        ).fetchone()[0]
        conn.close()
        assert audit_count >= 1, "Jede Heal-Mutation muss einen Audit-Eintrag haben"
