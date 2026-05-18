"""
Heartbeat-Monitor — prüft ob alle Komponenten regelmäßig melden.
Gibt eine Liste von Alarmen zurück; leer = alles OK.
"""

from datetime import datetime, timezone
from config.settings import HEARTBEAT_THRESHOLDS_MIN
from core.db import get_connection
from core.utils import log

# D.2: Schwellen zentral in config/settings.py — HEARTBEAT_THRESHOLDS_MIN
THRESHOLDS_MIN = HEARTBEAT_THRESHOLDS_MIN


def check_all_heartbeats() -> list[dict]:
    """
    Gibt Liste von Alarmen zurück: [{component, last_ts, age_min, threshold_min}].
    Leere Liste = alle Komponenten gesund.
    """
    conn = get_connection()
    now  = datetime.now(timezone.utc)
    alerts = []

    for component, threshold in THRESHOLDS_MIN.items():
        row = conn.execute(
            """SELECT ts, status, message FROM heartbeats
               WHERE component=? ORDER BY ts DESC LIMIT 1""",
            (component,),
        ).fetchone()

        if not row:
            alerts.append({
                "component": component,
                "last_ts": None,
                "age_min": None,
                "threshold_min": threshold,
                "reason": "never_reported",
            })
            log(f"[HEARTBEAT] ⚠ {component}: noch nie gemeldet")
            continue

        last_ts_str = row[0]
        try:
            last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            age_min = (now - last_ts).total_seconds() / 60
        except Exception:
            age_min = float("inf")

        if age_min > threshold:
            alerts.append({
                "component": component,
                "last_ts": last_ts_str,
                "age_min": round(age_min, 1),
                "threshold_min": threshold,
                "reason": f"stale: {age_min:.0f}min > {threshold}min",
                "last_status": row[1],
                "last_message": row[2],
            })
            log(f"[HEARTBEAT] ⚠ {component}: {age_min:.0f}min alt (Schwelle: {threshold}min)")
        else:
            log(f"[HEARTBEAT] ✓ {component}: {age_min:.1f}min alt (OK)")

    conn.close()
    return alerts


def write_heartbeat(component: str, status: str, message: str, latency_ms: float = 0.0):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), component, status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def cleanup_old_heartbeats(ttl_days: int = 7) -> int:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    conn = get_connection()
    cur = conn.execute("DELETE FROM heartbeats WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()
    return cur.rowcount
