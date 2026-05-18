"""
A.3 — Watchdog-Hülle für master_run.py.

Prüft ob master_run.py noch aktiv ist, indem es den frischesten Heartbeat
aus der DB prüft. Falls alle Komponenten still sind, wird ein Alarm ausgelöst.

In A.3: nur Detection und Logging.
In D.1: Telegram-Alert wird aktiviert.

Aufruf (Cron alle 5 Min): python3 scripts/master_watchdog.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.utils import log

# Nach dieser Stille-Dauer gilt master_run als ausgefallen
STALE_THRESHOLD_MIN = 15

# Datei-Heartbeat-Verzeichnis (core/process_lock.py schreibt hier hin)
_HB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "heartbeats")


def _newest_db_heartbeat() -> datetime | None:
    """Liest den neuesten Heartbeat-Timestamp aus trading.db."""
    try:
        from core.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT ts FROM heartbeats ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            ts_str = row[0] if isinstance(row, (list, tuple)) else row["ts"]
            return datetime.fromisoformat(ts_str)
    except Exception:
        pass
    return None


def _newest_file_heartbeat() -> datetime | None:
    """Liest den neuesten Heartbeat aus data/heartbeats/-Dateien."""
    try:
        newest = None
        for f in os.listdir(_HB_DIR):
            fp = os.path.join(_HB_DIR, f)
            if os.path.isfile(fp):
                mtime = datetime.fromtimestamp(os.path.getmtime(fp), tz=timezone.utc)
                if newest is None or mtime > newest:
                    newest = mtime
        return newest
    except Exception:
        pass
    return None


def check_master_alive() -> dict:
    """
    Gibt {'alive': bool, 'age_min': float, 'source': str} zurück.
    alive=True wenn mindestens eine Heartbeat-Quelle innerhalb STALE_THRESHOLD_MIN ist.
    """
    now = datetime.now(timezone.utc)
    candidates = []

    db_hb = _newest_db_heartbeat()
    if db_hb:
        candidates.append(("db_heartbeats", db_hb))

    file_hb = _newest_file_heartbeat()
    if file_hb:
        candidates.append(("file_heartbeats", file_hb))

    if not candidates:
        return {"alive": False, "age_min": float("inf"), "source": "none"}

    newest_src, newest_ts = max(candidates, key=lambda x: x[1])
    age_min = (now - newest_ts).total_seconds() / 60

    return {
        "alive": age_min <= STALE_THRESHOLD_MIN,
        "age_min": round(age_min, 1),
        "source": newest_src,
    }


def main() -> int:
    status = check_master_alive()
    if status["alive"]:
        log(f"[watchdog] master_run: OK — letzter Heartbeat vor {status['age_min']}min "
            f"({status['source']})")
        return 0

    log(f"[watchdog] ALARM: master_run still seit {status['age_min']}min "
        f"(Schwelle: {STALE_THRESHOLD_MIN}min, Quelle: {status['source']})")

    # Telegram-Alarm (D.1 — noch nicht aktiv in A.3)
    # from core.telegram_dispatcher import send  # wird in D.1 aktiviert
    # send("watchdog", f"master_run nicht aktiv seit {status['age_min']:.0f}min")

    return 1


if __name__ == "__main__":
    sys.exit(main())
