"""
Kill-Switch-Hierarchie (Phase 6) — 4 Stufen.

Stufe 1 (Soft):   Tages-DD überschritten → halbe Größe, kein neues Risiko
Stufe 2 (Hard):   HWM-Kill oder Phantom-Position → alle Positionen schließen
Stufe 3 (Vol):    HMM=HIGH_VOL über N Bars → Größe auf Faktor reduziert
Stufe 4 (Manual): Telegram-Override → sofort Hard-Kill

system_state-Key: kill_mode  →  'none' | 'soft' | 'hard' | 'vol' | 'manual'

C.3 — Clear-Pfad:
  clear_kill_mode(reason, cleared_by) ist die einzige öffentliche API zum
  Zurücksetzen. Jeder Set- und Clear-Event wird in kill_switch_events geschrieben.
  Stille automatische Freigabe ist nicht möglich.
"""
from __future__ import annotations

from datetime import datetime, timezone
from core.db import get_connection
from core.utils import log

_LEVELS = ("none", "soft", "vol", "hard", "manual")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(_LEVELS)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn():
    return get_connection()


def _log_event(
    conn,
    action: str,
    mode_from: str | None,
    mode_to: str,
    reason: str,
    cleared_by: str | None = None,
    asset: str | None = None,
) -> None:
    """Schreibt einen Audit-Eintrag in kill_switch_events."""
    try:
        conn.execute(
            """INSERT INTO kill_switch_events
               (ts, action, mode_from, mode_to, reason, cleared_by, asset)
               VALUES (?,?,?,?,?,?,?)""",
            (_now_iso(), action, mode_from, mode_to, reason, cleared_by, asset),
        )
    except Exception as exc:
        # Audit-Log ist best-effort — nie den eigentlichen Kill-Switch-Pfad blockieren
        log(f"[KillSwitch] WARNUNG: Audit-Log fehlgeschlagen: {exc}")


def get_kill_mode() -> str:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM system_state WHERE key='kill_mode'"
    ).fetchone()
    conn.close()
    return row["value"] if row else "none"


def set_kill_mode(mode: str, reason: str = "", asset: str | None = None) -> None:
    if mode not in _LEVELS:
        raise ValueError(f"Ungültiger Kill-Mode: {mode}")

    current = get_kill_mode()
    if _LEVEL_RANK.get(mode, 0) <= _LEVEL_RANK.get(current, 0) and mode != "none":
        log(f"[KillSwitch] Ignoriere {mode} (aktuell: {current} ist höher)")
        return

    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_mode", mode, _now_iso()),
    )
    if reason:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
            ("kill_reason", reason, _now_iso()),
        )
    if asset:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
            (f"kill_mode_{asset}", mode, _now_iso()),
        )

    _log_event(conn, action="set", mode_from=current, mode_to=mode,
               reason=reason or "(kein Grund angegeben)", asset=asset)

    conn.commit()
    conn.close()
    log(f"[KillSwitch] Modus → {mode.upper()} | Grund: {reason}")


def clear_kill_mode(reason: str, cleared_by: str) -> None:
    """
    Setzt kill_mode auf 'none' — explizite Clear-Aktion mit Pflicht-Parametern.

    Args:
        reason:     Warum der Kill-Switch gelöscht wird (z.B. "manuelle Prüfung OK").
        cleared_by: Wer die Aktion ausgelöst hat (z.B. "telegram:/panic_clear user=42").

    Schreibt immer einen Eintrag in kill_switch_events.
    """
    if not reason or not reason.strip():
        raise ValueError("clear_kill_mode: reason darf nicht leer sein")
    if not cleared_by or not cleared_by.strip():
        raise ValueError("clear_kill_mode: cleared_by darf nicht leer sein")

    current = get_kill_mode()
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_mode", "none", _now_iso()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_reason", "", _now_iso()),
    )

    _log_event(conn, action="clear", mode_from=current, mode_to="none",
               reason=reason, cleared_by=cleared_by)

    conn.commit()
    conn.close()
    log(f"[KillSwitch] Kill-Mode gelöscht → 'none' | von: {cleared_by} | Grund: {reason}")


def manual_override(reason: str = "Telegram-Override") -> None:
    """Stufe 4: Manueller Override via Telegram-Bot."""
    set_kill_mode("manual", reason=reason)


def is_hard_killed(asset: str | None = None) -> bool:
    global_mode = get_kill_mode()
    if global_mode in ("hard", "manual"):
        return True
    if asset:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            (f"kill_mode_{asset}",),
        ).fetchone()
        conn.close()
        if row and row["value"] in ("hard", "manual"):
            return True
    return False


def get_kill_switch_events(limit: int = 50) -> list[dict]:
    """Gibt die letzten N Kill-Switch-Events für Audit/Reporting zurück."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT ts, action, mode_from, mode_to, reason, cleared_by, asset
           FROM kill_switch_events ORDER BY ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
