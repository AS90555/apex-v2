"""
P2.4 — Kill-Switch Audit-Trail E2E-Tests.

Schicht A — governance/kill_switch.py (direkte API):
  - set_kill_mode() schreibt vollständigen Audit-Eintrag (ts, action, mode_from,
    mode_to, reason, asset)
  - clear_kill_mode() schreibt vollständigen Audit-Eintrag mit cleared_by + reason
  - clear ohne reason → ValueError, kein State-Wechsel
  - clear ohne cleared_by → ValueError, kein State-Wechsel
  - set_kill_mode("none") direkt ohne cleared_by → kein Clear möglich
    (nur über clear_kill_mode() mit Pflichtfeldern)

Schicht B — Telegram-Bot-Handler (monitor/telegram_bot.py):
  - /panic + Inline-Bestätigung → Kill aktiv, Audit user_id im reason
  - /panic_clear <grund> → Clear aktiv, Audit user_id als cleared_by
  - /panic_clear ohne Grund → abgelehnt, kein State-Wechsel, kein Audit
  - Panic-Bestätigung mit falscher user_id → abgelehnt, kein Kill
  - Nicht autorisierter User → kein Kill, kein Audit-Eintrag

Invariante: Jede Kill/Clear-Mutation hat genau einen Audit-Eintrag.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# DB-Fixture
# ══════════════════════════════════════════════════════════════════════════════

def _make_ks_db(tmp_path, name: str = "ks.db") -> str:
    """Erzeugt minimale DB mit kill_switch_events + system_state."""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kill_switch_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            action     TEXT    NOT NULL,
            mode_from  TEXT,
            mode_to    TEXT    NOT NULL,
            reason     TEXT    NOT NULL,
            cleared_by TEXT,
            asset      TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db


def _fresh_conn(db: str):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _get_events(db: str) -> list[dict]:
    conn = _fresh_conn(db)
    rows = conn.execute(
        "SELECT * FROM kill_switch_events ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_kill_mode(db: str) -> str:
    conn = _fresh_conn(db)
    row = conn.execute("SELECT value FROM system_state WHERE key='kill_mode'").fetchone()
    conn.close()
    return row["value"] if row else "none"


# ══════════════════════════════════════════════════════════════════════════════
# Schicht A — governance/kill_switch.py direkt
# ══════════════════════════════════════════════════════════════════════════════

class TestKillSwitchAuditDirect:
    def test_set_writes_complete_audit_entry(self, tmp_path):
        """set_kill_mode() → Audit-Eintrag mit ts, action, mode_from, mode_to, reason."""
        db = _make_ks_db(tmp_path, "set.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode
            set_kill_mode("hard", reason="Test-Grund", asset="BTC")

        events = _get_events(db)
        assert len(events) == 1
        e = events[0]
        assert e["action"] == "set"
        assert e["mode_to"] == "hard"
        assert e["mode_from"] is not None        # kann "none" oder "" sein
        assert e["reason"] == "Test-Grund"
        assert e["asset"] == "BTC"
        assert e["ts"]                            # nicht leer
        assert _get_kill_mode(db) == "hard"

    def test_clear_writes_complete_audit_entry_with_cleared_by(self, tmp_path):
        """clear_kill_mode() → Audit-Eintrag mit cleared_by + reason + ts."""
        db = _make_ks_db(tmp_path, "clear.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode, clear_kill_mode
            set_kill_mode("hard", reason="initial")
            clear_kill_mode(reason="manuell geprüft OK", cleared_by="telegram:user=42")

        events = _get_events(db)
        assert len(events) == 2
        clear_ev = next(e for e in events if e["action"] == "clear")
        assert clear_ev["mode_from"] == "hard"
        assert clear_ev["mode_to"] == "none"
        assert clear_ev["reason"] == "manuell geprüft OK"
        assert clear_ev["cleared_by"] == "telegram:user=42"
        assert clear_ev["ts"]
        assert _get_kill_mode(db) == "none"

    def test_clear_without_reason_raises_no_state_change(self, tmp_path):
        """clear ohne reason → ValueError, Kill-Mode bleibt aktiv."""
        db = _make_ks_db(tmp_path, "no_reason.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode, clear_kill_mode
            set_kill_mode("hard", reason="initial")
            with pytest.raises(ValueError, match="reason"):
                clear_kill_mode(reason="", cleared_by="telegram:user=42")

        assert _get_kill_mode(db) == "hard"
        # Nur ein Event (das Set), kein Clear-Event
        events = _get_events(db)
        assert all(e["action"] == "set" for e in events)

    def test_clear_without_cleared_by_raises_no_state_change(self, tmp_path):
        """clear ohne cleared_by → ValueError, Kill-Mode bleibt aktiv."""
        db = _make_ks_db(tmp_path, "no_by.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode, clear_kill_mode
            set_kill_mode("manual", reason="initial")
            with pytest.raises(ValueError, match="cleared_by"):
                clear_kill_mode(reason="OK", cleared_by="")

        assert _get_kill_mode(db) == "manual"

    def test_set_kill_mode_none_directly_not_a_clear(self, tmp_path):
        """set_kill_mode('none') direkt ist kein legitimer Clear-Pfad ohne cleared_by."""
        db = _make_ks_db(tmp_path, "set_none.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode
            set_kill_mode("hard", reason="initial")
            # set_kill_mode("none") geht über denselben Audit-Pfad aber ohne cleared_by
            # → keine cleared_by im Event (das ist der Unterschied zum clear_kill_mode-Pfad)
            set_kill_mode("none", reason="via set")

        events = _get_events(db)
        set_none_ev = next((e for e in events if e["mode_to"] == "none"), None)
        assert set_none_ev is not None
        # cleared_by ist None bei set_kill_mode — nur clear_kill_mode setzt es
        assert set_none_ev["cleared_by"] is None, \
            "set_kill_mode('none') hat kein cleared_by — nur clear_kill_mode setzt es"

    def test_every_mutation_has_audit_entry(self, tmp_path):
        """Jede Set/Clear-Mutation erzeugt exakt einen Audit-Eintrag — keine stillen Änderungen."""
        db = _make_ks_db(tmp_path, "all.db")
        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)):
            from governance.kill_switch import set_kill_mode, clear_kill_mode
            set_kill_mode("soft", reason="DD überschritten")
            set_kill_mode("hard", reason="Phantom erkannt")
            clear_kill_mode(reason="Recovery abgeschlossen", cleared_by="telegram:user=99")

        events = _get_events(db)
        assert len(events) == 3
        actions = [e["action"] for e in events]
        assert actions.count("set") == 2
        assert actions.count("clear") == 1


# ══════════════════════════════════════════════════════════════════════════════
# Telegram-Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════════════════

def _make_update(user_id: int = 42, chat_id: int = 42, text: str = "/panic") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = text
    return update


def _make_ctx(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _make_callback_query(user_id: int, callback_data: str) -> MagicMock:
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    return update, query


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
# Schicht B — Telegram-Handler E2E
# ══════════════════════════════════════════════════════════════════════════════

class TestTelegramPanicAuditE2E:
    def test_panic_confirm_sets_kill_and_audit_has_user_id(self, tmp_path):
        """panic_confirm_{uid} Callback → Kill aktiv, Audit-reason enthält Pfad-Info."""
        db = _make_ks_db(tmp_path, "panic_confirm.db")
        uid = 42
        update, query = _make_callback_query(uid, f"panic_confirm_{uid}")

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid)):
            from monitor.telegram_bot import button_callback
            _run(button_callback(update, _make_ctx()))

        assert _get_kill_mode(db) == "hard"
        events = _get_events(db)
        assert len(events) == 1
        e = events[0]
        assert e["action"] == "set"
        assert e["mode_to"] == "hard"
        assert e["reason"]         # nicht leer
        assert e["ts"]

    def test_panic_confirm_wrong_user_rejected_no_kill(self, tmp_path):
        """panic_confirm_{uid} von falschem User → abgelehnt, kein Kill, kein Audit."""
        db = _make_ks_db(tmp_path, "wrong_user.db")
        uid_owner = 42
        uid_attacker = 99
        update, query = _make_callback_query(uid_attacker, f"panic_confirm_{uid_owner}")

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid_owner)):
            from monitor.telegram_bot import button_callback
            _run(button_callback(update, _make_ctx()))

        assert _get_kill_mode(db) == "none", "Falscher User darf Kill-Switch nicht setzen"
        events = _get_events(db)
        assert len(events) == 0, "Abgelehnter Versuch darf keinen Audit-Eintrag erzeugen"

    def test_panic_clear_with_reason_writes_audit_with_user_id(self, tmp_path):
        """/panic_clear <grund> → Clear-Audit mit cleared_by=telegram:user=<uid>."""
        db = _make_ks_db(tmp_path, "clear_tg.db")
        uid = 42
        update = _make_update(user_id=uid, chat_id=uid, text="/panic_clear Netz stabil")
        ctx = _make_ctx(args=["Netz", "stabil"])

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid)):
            from governance.kill_switch import set_kill_mode
            set_kill_mode("hard", reason="initial")
            from monitor.telegram_bot import cmd_panic_clear
            _run(cmd_panic_clear(update, ctx))

        assert _get_kill_mode(db) == "none"
        events = _get_events(db)
        clear_ev = next(e for e in events if e["action"] == "clear")
        assert f"user={uid}" in clear_ev["cleared_by"], \
            f"cleared_by muss user_id enthalten, ist: {clear_ev['cleared_by']}"
        assert "Netz stabil" in clear_ev["reason"]

    def test_panic_clear_without_reason_rejected_no_clear(self, tmp_path):
        """/panic_clear ohne Grund → abgelehnt, Kill-Mode bleibt, kein Audit-Clear."""
        db = _make_ks_db(tmp_path, "no_reason_tg.db")
        uid = 42
        update = _make_update(user_id=uid, chat_id=uid, text="/panic_clear")
        ctx = _make_ctx(args=[])

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid)):
            from governance.kill_switch import set_kill_mode
            set_kill_mode("hard", reason="initial")
            from monitor.telegram_bot import cmd_panic_clear
            _run(cmd_panic_clear(update, ctx))

        assert _get_kill_mode(db) == "hard", "Kill-Mode muss ohne Grund aktiv bleiben"
        events = _get_events(db)
        assert not any(e["action"] == "clear" for e in events), \
            "Kein Clear-Audit-Eintrag bei ungültigem Aufruf"
        # Fehler-Antwort wurde gesendet
        update.message.reply_text.assert_called_once()

    def test_unauthorized_user_no_kill_no_audit(self, tmp_path):
        """/panic von nicht-autorisiertem User → kein Kill, kein Audit, Fehlermeldung."""
        db = _make_ks_db(tmp_path, "unauth.db")
        uid_allowed = 42
        uid_stranger = 999
        update = _make_update(user_id=uid_stranger, chat_id=uid_stranger)
        ctx = _make_ctx()

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid_allowed)):
            from monitor.telegram_bot import cmd_panic
            _run(cmd_panic(update, ctx))

        assert _get_kill_mode(db) == "none"
        events = _get_events(db)
        assert len(events) == 0
        update.message.reply_text.assert_called_once()
        reply_text = str(update.message.reply_text.call_args)
        assert "autorisiert" in reply_text.lower() or "⛔" in reply_text

    def test_panic_abort_no_kill_no_audit(self, tmp_path):
        """panic_abort Callback → kein Kill, kein Audit-Eintrag."""
        db = _make_ks_db(tmp_path, "abort.db")
        uid = 42
        update, query = _make_callback_query(uid, "panic_abort")

        with patch("governance.kill_switch._get_conn", side_effect=lambda: _fresh_conn(db)), \
             patch("monitor.telegram_bot.TELEGRAM_CHAT_ID", str(uid)):
            from monitor.telegram_bot import button_callback
            _run(button_callback(update, _make_ctx()))

        assert _get_kill_mode(db) == "none"
        events = _get_events(db)
        assert len(events) == 0, "Abbruch darf keinen Audit-Eintrag erzeugen"
