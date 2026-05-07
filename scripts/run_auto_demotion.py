"""
Auto-Demotion: Prüft dry_run-Deployments auf Demotion und Go-Live-Readiness.
Demotion: n >= 30 Trades AND Live-PF < 1.20 → archivieren.
Go-Live:  n >= 30 Trades AND Live-PF >= 1.40 AND drift=ok AND Regime ok → Notification.
NIEMALS live-Deployments anfassen.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from core.db import get_connection
from core.utils import log
from config.settings import STRATEGY_ALLOWED_REGIMES

_TG_BOT    = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")
_TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
_BASE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_NAME   = "apex_v2"
DB_PATH    = os.path.join(_BASE, "data", f"{_DB_NAME}.db")
BACKUP_DIR = os.path.join(_BASE, "data", "backups")

PF_DEMOTION_MAX = 1.20
PF_GOLIVE_MIN   = 1.40
N_MIN           = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backup_db() -> str:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"{_DB_NAME}_{ts}_pre-auto-demotion.db")
    shutil.copy2(DB_PATH, dst)
    log(f"[AUTO_DEMOTION] Backup: {dst}")
    return dst


def _send_telegram(text: str) -> None:
    if not _TG_BOT or not _TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_BOT}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
    except Exception as e:
        log(f"[AUTO_DEMOTION] Telegram-Fehler: {e}")


def _esc(s: str) -> str:
    for c in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(c, f"\\{c}")
    return s


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "auto_demotion", status, message, round(latency_ms, 1)),
    )


def _calc_live_pf(strategy_key: str, asset: str, conn) -> tuple[int, float | None]:
    """Berechnet n_closed_trades und Live-PF (gross_win / gross_loss) aus trades."""
    rows = conn.execute(
        "SELECT pnl_r FROM trades "
        "WHERE strategy=? AND asset=? AND exit_ts IS NOT NULL",
        (strategy_key, asset),
    ).fetchall()

    n = len(rows)
    if n == 0:
        return 0, None

    gross_win  = sum(r[0] for r in rows if r[0] is not None and r[0] > 0)
    gross_loss = abs(sum(r[0] for r in rows if r[0] is not None and r[0] < 0))

    if gross_loss == 0:
        pf = None  # Kein Verlust — PF nicht berechenbar (unendlich)
    else:
        pf = gross_win / gross_loss

    return n, pf


def _get_drift_status(strategy_key: str, conn) -> str:
    row = conn.execute(
        "SELECT status FROM live_vs_backtest_drift "
        "WHERE strategy_key=? ORDER BY checked_at DESC LIMIT 1",
        (strategy_key,),
    ).fetchone()
    return row[0] if row else "unknown"


def _get_current_regime(asset: str, conn) -> str:
    try:
        from research.train_hmm import get_current_regime
        return get_current_regime(asset, conn)
    except Exception:
        return "UNKNOWN"


def _regime_ok(base_strategy: str, regime: str) -> bool:
    allowed = STRATEGY_ALLOWED_REGIMES.get(base_strategy)
    if allowed is None:
        return True  # Kein Filter → immer ok
    return regime in allowed


def main() -> None:
    t0 = time.monotonic()
    log("[AUTO_DEMOTION] Start")

    conn = get_connection()

    dry_runs = conn.execute(
        "SELECT id, strategy_key, base_strategy, asset, discovery_id "
        "FROM active_deployments "
        "WHERE mode='dry_run' AND active=1"
    ).fetchall()

    log(f"[AUTO_DEMOTION] {len(dry_runs)} aktive dry_run-Deployments geprüft")

    demoted      = 0
    golive_ready = 0
    needs_backup = False

    # Demotion-Kandidaten sammeln, Backup nur wenn nötig
    to_demote = []
    for dep in dry_runs:
        dep = dict(dep)
        n, pf = _calc_live_pf(dep["strategy_key"], dep["asset"], conn)
        dep["n_trades"] = n
        dep["pf_live"]  = pf

        if n >= N_MIN and pf is not None and pf < PF_DEMOTION_MAX:
            to_demote.append(dep)

    if to_demote:
        _backup_db()
        needs_backup = True

    for dep in to_demote:
        n  = dep["n_trades"]
        pf = dep["pf_live"]
        note = f"Auto-demoted: live_pf={pf:.2f} < {PF_DEMOTION_MAX} nach {n} Trades"

        conn.execute(
            "UPDATE active_deployments "
            "SET active=0, mode='archived', note=? "
            "WHERE id=?",
            (note, dep["id"]),
        )

        if dep["discovery_id"]:
            conn.execute(
                "UPDATE lab_discoveries SET deployment_status='archived' WHERE id=?",
                (dep["discovery_id"],),
            )

        conn.commit()
        demoted += 1
        log(f"[AUTO_DEMOTION] DEMOTED: {dep['strategy_key']}/{dep['asset']} "
            f"| PF={pf:.2f} | n={n}")

        _send_telegram(
            f"⚠️ *Dry\\-Run demoted*\n"
            f"`{_esc(dep['strategy_key'])}/{_esc(dep['asset'])}`\n"
            f"Live\\-PF: `{pf:.2f}` \\< `{PF_DEMOTION_MAX}` nach `{n}` Trades"
        )

    # ── Go-Live-Check (nur Notification, keine DB-Aktion) ─────────────────────
    remaining = conn.execute(
        "SELECT id, strategy_key, base_strategy, asset "
        "FROM active_deployments "
        "WHERE mode='dry_run' AND active=1"
    ).fetchall()

    for dep in remaining:
        dep = dict(dep)
        n, pf = _calc_live_pf(dep["strategy_key"], dep["asset"], conn)

        if n < N_MIN or pf is None or pf < PF_GOLIVE_MIN:
            continue

        drift  = _get_drift_status(dep["strategy_key"], conn)
        if drift != "ok":
            log(f"[AUTO_DEMOTION] Go-Live Skip {dep['strategy_key']}: drift={drift}")
            continue

        regime = _get_current_regime(dep["asset"], conn)
        if not _regime_ok(dep["base_strategy"], regime):
            log(f"[AUTO_DEMOTION] Go-Live Skip {dep['strategy_key']}: "
                f"Regime {regime} not in ALLOWED")
            continue

        golive_ready += 1
        log(f"[AUTO_DEMOTION] GO-LIVE BEREIT: {dep['strategy_key']}/{dep['asset']} "
            f"| PF={pf:.2f} | Regime={regime} | deployment_id={dep['id']}")

        _send_telegram(
            f"🚀 *Go\\-Live bereit*\n"
            f"`{_esc(dep['strategy_key'])}/{_esc(dep['asset'])}`\n"
            f"Live\\-PF: `{pf:.2f}` \\| Regime: `{_esc(regime)}`\n"
            f"→ Bestätige mit `/apex\\_promote {dep['id']}`"
        )

    latency_ms = (time.monotonic() - t0) * 1000
    _write_heartbeat(
        conn,
        status="ok",
        message=f"dry_runs={len(dry_runs)} demoted={demoted} golive_ready={golive_ready}",
        latency_ms=latency_ms,
    )
    conn.commit()
    conn.close()

    log(f"[AUTO_DEMOTION] Fertig: {demoted} demoted | {golive_ready} go-live-ready | "
        f"{latency_ms:.0f}ms")


if __name__ == "__main__":
    main()
