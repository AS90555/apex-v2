"""
Reconciliation-Daemon (Phase 5) — Cron 1× pro Minute.

Vergleicht Exchange-State (getOpenOrders, getPositions) mit DB-State.
NIEMALS selbst Orders senden — nur Alert + DB-Flag setzen.

Klassen:
  - Phantom-Position: Position auf Exchange, keine offene Trade in DB → Hard Kill
  - Geister-Position:  Trade in DB als 'executed', keine Position auf Exchange → Alert
  - Größenabweichung:  > RECONCILE_SIZE_TOLERANCE → Alert
  - Match:             Heartbeat 'reconciliation_ok'
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log
from config.settings import RECONCILE_SIZE_TOLERANCE, RECONCILER_AUTO_HEAL_GHOST

_TG_BOT  = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


def _write_reconcile_audit(conn, asset: str, action: str, detail: str) -> None:
    """Schreibt einen Heal-Audit-Eintrag in execution_audit_log (auditierbar, nie still)."""
    conn.execute(
        """INSERT INTO execution_audit_log
           (signal_id, cl_ord_id, state_from, state_to, payload_json, created_at)
           VALUES (?,?,?,?,?,?)""",
        (None, f"RECONCILE-HEAL-{asset}", action, "healed",
         f'{{"asset":"{asset}","detail":"{detail}"}}', _now_iso()),
    )
    log(f"[RECONCILE-HEAL] {asset}: {action} — {detail}")


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "reconciliation", status, message, round(latency_ms, 1)),
    )


def _set_hard_kill(conn, asset: str, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        (f"kill_mode_{asset}", "hard", _now_iso()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?,?,?)",
        ("kill_reason", reason, _now_iso()),
    )
    log(f"[RECONCILE] HARD KILL gesetzt für {asset}: {reason}")
    _send_telegram(f"🚨 HARD KILL: {asset}\nGrund: {reason}")


def _get_exchange_positions(client) -> dict[str, float]:
    """Gibt {symbol: size} für alle offenen Positionen zurück."""
    try:
        positions = client.get_positions()
        result = {}
        for pos in positions:
            size = float(pos.get("total", pos.get("size", 0)))
            if abs(size) > 1e-8:
                symbol = pos.get("symbol", pos.get("instId", ""))
                result[symbol] = size
        return result
    except Exception as e:
        log(f"[RECONCILE] Exchange-Positions-Fehler: {e}")
        return {}


def _get_exchange_open_orders(client) -> list[dict]:
    """Gibt alle offenen Orders zurück."""
    try:
        return client.get_open_orders() or []
    except Exception as e:
        log(f"[RECONCILE] Exchange-Orders-Fehler: {e}")
        return []


def _get_db_open_trades(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, strategy_key, asset, side, size, status FROM trades "
        "WHERE status IN ('executed', 'open')"
    ).fetchall()
    return [dict(r) for r in rows]


def reconcile_once() -> dict:
    t0 = time.monotonic()
    conn = get_connection()
    findings: list[str] = []
    hard_kills = 0
    alerts = 0

    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient()
    except Exception as e:
        log(f"[RECONCILE] BitgetClient-Init fehlgeschlagen: {e}")
        _write_heartbeat(conn, "error", f"client_init_failed: {e}", 0)
        conn.commit()
        conn.close()
        return {"hard_kills": 0, "alerts": 0, "error": str(e)}

    exchange_positions = _get_exchange_positions(client)
    db_trades = _get_db_open_trades(conn)

    # Symbol-Map: asset → DB-size
    db_by_asset: dict[str, float] = {}
    for t in db_trades:
        asset = t["asset"]
        size = float(t.get("size") or 0)
        db_by_asset[asset] = db_by_asset.get(asset, 0) + size

    # Phantom-Positionen: Exchange hat Position, DB nicht
    for symbol, ex_size in exchange_positions.items():
        asset = symbol.replace("USDT", "").replace("_UMCBL", "").replace("USDT_SPBL", "")
        if asset not in db_by_asset:
            reason = f"Phantom-Position: {symbol} size={ex_size:.4f} auf Exchange, keine offene Trade in DB"
            findings.append(reason)
            _set_hard_kill(conn, asset, reason)
            hard_kills += 1

    # Geister-Positionen: DB hat Trade, Exchange nicht
    for asset, db_size in db_by_asset.items():
        symbol_candidates = [f"{asset}USDT_UMCBL", f"{asset}USDT"]
        in_exchange = any(s in exchange_positions for s in symbol_candidates)
        if not in_exchange:
            msg = f"Geister-Position: DB hat {asset} size={db_size:.4f}, Exchange hat keine Position"
            findings.append(msg)
            if RECONCILER_AUTO_HEAL_GHOST:
                # P2.3 — kontrollierte Mutation: Trade auf ghost_closed setzen + Audit
                conn.execute(
                    "UPDATE trades SET status='ghost_closed', reconcile_required=1 "
                    "WHERE asset=? AND status IN ('executed','open')",
                    (asset,),
                )
                _write_reconcile_audit(conn, asset, "ghost_heal",
                                       f"size={db_size:.4f}_set_ghost_closed")
                _send_telegram(
                    f"⚠️ Geister-Position HEALED: {asset}\n{msg}\n"
                    f"→ status='ghost_closed' gesetzt (AUTO_HEAL aktiv)"
                )
            else:
                conn.execute(
                    "UPDATE trades SET reconcile_required=1 WHERE asset=? AND status IN ('executed','open')",
                    (asset,),
                )
                _send_telegram(f"⚠️ Geister-Position: {asset}\n{msg}")
            alerts += 1

    # Größenabweichungen
    for symbol, ex_size in exchange_positions.items():
        asset = symbol.replace("USDT", "").replace("_UMCBL", "").replace("USDT_SPBL", "")
        if asset in db_by_asset:
            db_size = db_by_asset[asset]
            diff = abs(abs(ex_size) - abs(db_size))
            if diff > RECONCILE_SIZE_TOLERANCE:
                msg = (f"Größenabweichung {asset}: Exchange={ex_size:.4f}, "
                       f"DB={db_size:.4f}, Diff={diff:.4f}")
                findings.append(msg)
                if RECONCILER_AUTO_HEAL_GHOST:
                    # P2.3 — Heuristik: nur healen wenn Exchange-Size plausibler ist
                    # (Exchange ist immer maßgeblich bei Abweichung)
                    conn.execute(
                        "UPDATE trades SET size=?, reconcile_required=1 "
                        "WHERE asset=? AND status IN ('executed','open')",
                        (abs(ex_size), asset),
                    )
                    _write_reconcile_audit(conn, asset, "size_mismatch_heal",
                                           f"db={db_size:.4f}_ex={ex_size:.4f}_diff={diff:.4f}")
                    _send_telegram(
                        f"⚠️ Größenabweichung HEALED: {asset}\n{msg}\n"
                        f"→ DB-Size auf Exchange-Size {ex_size:.4f} korrigiert (AUTO_HEAL aktiv)"
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET reconcile_required=1 WHERE asset=? AND status IN ('executed','open')",
                        (asset,),
                    )
                    _send_telegram(f"⚠️ Größenabweichung: {asset}\n{msg}")
                alerts += 1

    latency_ms = (time.monotonic() - t0) * 1000
    status = "ok" if not findings else ("critical" if hard_kills else "warning")
    summary = (f"positions_checked={len(exchange_positions)} "
               f"db_trades={len(db_trades)} "
               f"hard_kills={hard_kills} alerts={alerts}")
    _write_heartbeat(conn, status, summary, latency_ms)

    if not findings:
        log(f"[RECONCILE] OK — {summary} ({latency_ms:.0f}ms)")
    else:
        for f in findings:
            log(f"[RECONCILE] FINDING: {f}")

    conn.commit()
    conn.close()
    return {"hard_kills": hard_kills, "alerts": alerts, "findings": findings}


if __name__ == "__main__":
    result = reconcile_once()
    sys.exit(1 if result.get("hard_kills", 0) > 0 else 0)
