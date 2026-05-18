"""
run_drift_check.py — Täglicher Live-vs-Backtest-Drift-Check.

Cron: 0 6 * * * cd /root/apex-v2 && python3 scripts/run_drift_check.py >> logs/drift_check.log 2>&1

Ablauf:
  1. Alle aktiven Deployments laden (JOIN active_deployments + lab_discoveries)
  2. Live-PF aus geschlossenen Trades berechnen
  3. Drift berechnen und in live_vs_backtest_drift schreiben
  4. Bei critical (drift < DRIFT_CRITICAL_PCT UND n >= DRIFT_MIN_TRADES):
     → active_deployments.mode = 'shadow'
     → Telegram-Push
  5. Heartbeat schreiben
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log, now_iso
from config.settings import (
    DRIFT_WARNING_PCT, DRIFT_CRITICAL_PCT, DRIFT_MIN_TRADES,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_telegram(msg: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(msg)


def _load_deployments(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT
            ad.id            AS deployment_id,
            ad.strategy_key,
            ad.asset,
            ad.mode,
            ld.pf_test       AS pf_oos_brutto,
            COALESCE(ld.pf_test_netto, ld.pf_test * 0.75) AS pf_oos,
            ld.pf_test_netto IS NOT NULL               AS pf_netto_known,
            ld.n_test        AS oos_n
        FROM active_deployments ad
        LEFT JOIN lab_discoveries ld ON ld.id = ad.discovery_id
        WHERE ad.active = 1
          AND ad.mode IN ('live', 'dry_run')
    """).fetchall()
    return [dict(r) for r in rows]


def _calc_live_pf(conn, strategy_key: str) -> dict:
    row = conn.execute("""
        SELECT
            COUNT(*)                                               AS n_live,
            SUM(CASE WHEN pnl_r > 0 THEN pnl_r  ELSE 0 END)      AS gross_win,
            SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END)  AS gross_loss
        FROM trades
        WHERE exit_ts IS NOT NULL
          AND mode IN ('live', 'dry_run')
          AND strategy = ?
    """, (strategy_key,)).fetchone()

    n         = row["n_live"] or 0
    gross_win  = row["gross_win"]  or 0.0
    gross_loss = row["gross_loss"] or 0.0
    pf_live    = (gross_win / gross_loss) if gross_loss > 0 else None

    return {"n_live": n, "gross_win": gross_win, "gross_loss": gross_loss, "pf_live": pf_live}


def _classify(drift_pct: float | None, n_live: int) -> str:
    if drift_pct is None:
        return "ok"
    if drift_pct < DRIFT_CRITICAL_PCT and n_live >= DRIFT_MIN_TRADES:
        return "critical"
    if drift_pct < DRIFT_WARNING_PCT:
        return "warning"
    return "ok"


def _write_drift_row(conn, dep: dict, live: dict, drift_pct: float | None,
                     status: str, action: str | None) -> None:
    conn.execute("""
        INSERT INTO live_vs_backtest_drift
            (checked_at, deployment_id, strategy_key, asset, mode,
             n_live, pf_live, pf_oos, drift_pct, status, action_taken)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        _now(),
        dep["deployment_id"], dep["strategy_key"], dep["asset"], dep["mode"],
        live["n_live"], live["pf_live"], dep["pf_oos"],
        drift_pct, status, action,
    ))


def _auto_pause(conn, dep: dict) -> None:
    note = (f"Auto-pause 2026-05-06: Live-PF zu weit unter OOS-PF "
            f"(drift < {DRIFT_CRITICAL_PCT}% bei n >= {DRIFT_MIN_TRADES})")
    conn.execute(
        "UPDATE active_deployments SET mode='shadow', note=? WHERE id=?",
        (note, dep["deployment_id"]),
    )
    log(f"[DRIFT] AUTO-PAUSE: {dep['strategy_key']} {dep['asset']} → shadow")


def run() -> None:
    t0 = time.time()
    log("[DRIFT] Drift-Check gestartet")

    conn = get_connection()
    deployments = _load_deployments(conn)
    log(f"[DRIFT] {len(deployments)} aktive Deployments geladen")

    results = []
    critical_msgs = []

    for dep in deployments:
        pf_oos = dep.get("pf_oos")
        if pf_oos is None or pf_oos <= 0:
            log(f"[DRIFT] {dep['strategy_key']}: kein OOS-PF in lab_discoveries — überspringe")
            continue

        live = _calc_live_pf(conn, dep["strategy_key"])
        n_live   = live["n_live"]
        pf_live  = live["pf_live"]

        # Drift nur berechenbar wenn mind. 2 Trades mit Verlust (sonst PF = ∞)
        drift_pct = None
        if pf_live is not None:
            drift_pct = (pf_live - pf_oos) / pf_oos * 100

        status = _classify(drift_pct, n_live)
        action = None

        pf_live_str  = f"{pf_live:.2f}"  if pf_live  is not None else "n/a"
        drift_str    = f"{drift_pct:.1f}%" if drift_pct is not None else "n/a"
        pf_basis     = "netto" if dep.get("pf_netto_known") else "schätzung(brutto×0.75)"
        log(f"[DRIFT] {dep['strategy_key']} {dep['asset']} | "
            f"n={n_live} | pf_live={pf_live_str} | "
            f"pf_oos_netto={pf_oos:.2f}({pf_basis}) | drift={drift_str} | "
            f"status={status}")

        if status == "critical":
            _auto_pause(conn, dep)
            action = "shadow_downgrade"
            critical_msgs.append(
                f"🔴 *AUTO-PAUSE*: `{dep['strategy_key']}` ({dep['asset']})\n"
                f"Live-PF: {pf_live:.2f} | OOS-PF: {pf_oos:.2f} | "
                f"Drift: {drift_pct:.1f}% | n={n_live}"
            )
        elif status == "warning":
            critical_msgs.append(
                f"⚠️ *DRIFT WARNING*: `{dep['strategy_key']}` ({dep['asset']})\n"
                f"Live-PF: {pf_live:.2f} | OOS-PF: {pf_oos:.2f} | "
                f"Drift: {drift_pct:.1f}% | n={n_live}"
            )

        _write_drift_row(conn, dep, live, drift_pct, status, action)
        results.append({"key": dep["strategy_key"], "status": status,
                        "n": n_live, "drift": drift_pct})

    conn.commit()

    # Telegram-Push bei warning/critical
    if critical_msgs:
        msg = "📊 *APEX Drift-Check*\n\n" + "\n\n".join(critical_msgs)
        _send_telegram(msg)

    # Heartbeat
    latency_ms = (time.time() - t0) * 1000
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) VALUES (?,?,?,?,?)",
        (_now(), "drift_check", "ok",
         f"checked={len(results)} critical={sum(1 for r in results if r['status']=='critical')}",
         round(latency_ms, 1)),
    )
    conn.commit()
    conn.close()

    log(f"[DRIFT] Fertig: {len(results)} Deployments geprüft | {latency_ms:.0f}ms")


if __name__ == "__main__":
    run()
