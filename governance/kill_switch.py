"""
Kill-Switch-Hierarchie (Phase 6) — 4 Stufen.

Stufe 1 (Soft):   Tages-DD überschritten → halbe Größe, kein neues Risiko
Stufe 2 (Hard):   HWM-Kill oder Phantom-Position → alle Positionen schließen
Stufe 3 (Vol):    HMM=HIGH_VOL über N Bars → Größe auf Faktor reduziert
Stufe 4 (Manual): Telegram-Override → sofort Hard-Kill

system_state-Key: kill_mode  →  'none' | 'soft' | 'hard' | 'vol' | 'manual'
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
    conn.commit()
    conn.close()
    log(f"[KillSwitch] Modus → {mode.upper()} | Grund: {reason}")


def clear_kill_mode() -> None:
    """Setzt kill_mode auf 'none' — nur durch Admin nach manueller Prüfung."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_mode", "none", _now_iso()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_reason", "", _now_iso()),
    )
    conn.commit()
    conn.close()
    log("[KillSwitch] Kill-Mode gelöscht (zurück auf 'none')")


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
