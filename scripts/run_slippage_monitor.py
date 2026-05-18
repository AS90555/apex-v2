"""
Slippage-Monitor (Phase 5) — Cron 1× stündlich.

Prüft Median-Slippage der letzten N Trades pro Deployment.
Bei Überschreitung SLIPPAGE_ALERT_THRESHOLD_BPS:
  - Deployment → shadow
  - Telegram-Alert
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log
from config.settings import SLIPPAGE_ALERT_THRESHOLD_BPS

_TG_BOT  = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
_LOOKBACK_TRADES = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2
    return s[n // 2]


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "slippage_monitor", status, message, round(latency_ms, 1)),
    )


def monitor_once() -> dict:
    t0 = time.monotonic()
    conn = get_connection()

    deployments = conn.execute(
        "SELECT strategy_key, base_strategy, asset FROM active_deployments WHERE active=1"
    ).fetchall()

    alerted = 0
    checked = 0

    for dep in deployments:
        sk   = dep["strategy_key"]
        strat = dep["base_strategy"]
        asset = dep["asset"]

        rows = conn.execute(
            f"""SELECT slippage_bps FROM trades
                WHERE strategy_key=? AND slippage_bps IS NOT NULL
                ORDER BY id DESC LIMIT {_LOOKBACK_TRADES}""",
            (sk,),
        ).fetchall()

        slippages = [r["slippage_bps"] for r in rows if r["slippage_bps"] is not None]
        if len(slippages) < 5:
            continue

        checked += 1
        median_slip = _median(slippages)

        if median_slip > SLIPPAGE_ALERT_THRESHOLD_BPS:
            log(
                f"[SLIPPAGE] ALERT {sk}: Median={median_slip:.1f}bps > "
                f"Threshold={SLIPPAGE_ALERT_THRESHOLD_BPS}bps → shadow"
            )
            conn.execute(
                "UPDATE active_deployments SET mode='shadow' WHERE strategy_key=?",
                (sk,),
            )
            _send_telegram(
                f"⚠️ Slippage-Alert: {strat}/{asset}\n"
                f"Median={median_slip:.1f}bps (Limit={SLIPPAGE_ALERT_THRESHOLD_BPS}bps)\n"
                f"→ auf shadow gesetzt"
            )
            alerted += 1
        else:
            log(f"[SLIPPAGE] OK {sk}: Median={median_slip:.1f}bps")

    latency_ms = (time.monotonic() - t0) * 1000
    summary = f"checked={checked} alerted={alerted}"
    _write_heartbeat(conn, "ok" if alerted == 0 else "warning", summary, latency_ms)
    conn.commit()
    conn.close()
    log(f"[SLIPPAGE] Fertig — {summary} ({latency_ms:.0f}ms)")
    return {"checked": checked, "alerted": alerted}


if __name__ == "__main__":
    monitor_once()
