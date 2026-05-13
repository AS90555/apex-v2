"""
Funding-Rate-Intake (Phase 6) — Cron alle 5 Minuten.

Holt aktuelle Funding-Rates von Bitget pro Asset aus LIVE_ASSETS,
schreibt sie in die funding_rates-Tabelle (angelegt in Phase 2).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log
from config.settings import LIVE_ASSETS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "funding_intake", status, message, round(latency_ms, 1)),
    )


def fetch_funding_rates() -> dict[str, float]:
    """Holt Funding-Rates von Bitget. Gibt {asset: rate} zurück."""
    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient()
        rates = {}
        for asset in LIVE_ASSETS:
            try:
                symbol = f"{asset}USDT_UMCBL"
                result = client.get_funding_rate(symbol)
                if result is not None:
                    rates[asset] = float(result)
            except Exception as e:
                log(f"[FUNDING_INTAKE] Fehler bei {asset}: {e}")
        return rates
    except Exception as e:
        log(f"[FUNDING_INTAKE] BitgetClient-Fehler: {e}")
        return {}


def intake_once() -> dict:
    t0 = time.monotonic()
    conn = get_connection()

    rates = fetch_funding_rates()
    written = 0

    now_iso = _now_iso()
    for asset, rate in rates.items():
        conn.execute(
            "INSERT INTO funding_rates (asset, funding_rate, funding_time, created_at) "
            "VALUES (?,?,?,?)",
            (asset, rate, now_iso, now_iso),
        )
        written += 1

    latency_ms = (time.monotonic() - t0) * 1000
    status = "ok" if written > 0 or not LIVE_ASSETS else "warning"
    summary = f"assets={len(LIVE_ASSETS)} written={written}"
    _write_heartbeat(conn, status, summary, latency_ms)
    conn.commit()
    conn.close()
    log(f"[FUNDING_INTAKE] {summary} ({latency_ms:.0f}ms)")
    return {"written": written}


if __name__ == "__main__":
    intake_once()
