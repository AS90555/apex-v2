"""
Regime-Monitor: Vergleicht aktuelles HMM-Regime mit zuletzt gespeichertem.
Erkennt Regime-Wechsel und sendet Telegram-Push.
Systemd-Timer: alle 4 Stunden.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

import requests
from core.db import get_connection
from core.utils import log
from config.settings import STRATEGY_ALLOWED_REGIMES
from research.train_hmm import get_current_regime

_TG_BOT    = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")
_TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_KEY_PREFIX = "regime_"   # bestehende Konvention in system_state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


def _esc(s: str) -> str:
    for c in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(c, f"\\{c}")
    return s


def _get_stored_regime(asset: str, conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM system_state WHERE key=?",
        (f"{STATE_KEY_PREFIX}{asset}",),
    ).fetchone()
    return row[0] if row else None


def _upsert_regime(asset: str, regime: str, conn) -> None:
    now = _now_iso()
    existing = conn.execute(
        "SELECT 1 FROM system_state WHERE key=?",
        (f"{STATE_KEY_PREFIX}{asset}",),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE system_state SET value=?, updated_at=? WHERE key=?",
            (regime, now, f"{STATE_KEY_PREFIX}{asset}"),
        )
    else:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?,?,?)",
            (f"{STATE_KEY_PREFIX}{asset}", regime, now),
        )


def _strategies_for_asset(asset: str, conn) -> list[str]:
    rows = conn.execute(
        "SELECT base_strategy FROM active_deployments WHERE asset=? AND active=1",
        (asset,),
    ).fetchall()
    return [r[0] for r in rows]


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "regime_monitor", status, message, round(latency_ms, 1)),
    )


def main() -> None:
    t0 = time.monotonic()
    log("[REGIME_MONITOR] Start")

    conn = get_connection()

    # Alle aktiven Assets aus Deployments + bekannte aus system_state
    dep_assets = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM active_deployments WHERE active=1"
        ).fetchall()
    ]

    changes   = 0
    checked   = 0
    errors    = 0

    for asset in dep_assets:
        try:
            stored  = _get_stored_regime(asset, conn)
            current = get_current_regime(asset, conn)
            checked += 1

            if stored is None:
                # Erstinitalisierung
                _upsert_regime(asset, current, conn)
                log(f"[REGIME_MONITOR] {asset}: initialisiert mit {current}")
                continue

            if current == stored:
                log(f"[REGIME_MONITOR] {asset}: {current} (unverändert)")
                continue

            # Regime-Wechsel erkannt
            _upsert_regime(asset, current, conn)
            changes += 1
            log(f"[REGIME_MONITOR] {asset}: WECHSEL {stored} → {current}")

            strategies = _strategies_for_asset(asset, conn)
            strat_lines = []
            for strat in strategies:
                allowed = STRATEGY_ALLOWED_REGIMES.get(strat, ["TREND", "SIDEWAYS", "HIGH_VOL"])
                status  = "aktiv ✅" if current in allowed else "gedämpft ⚠️"
                strat_lines.append(f"  `{_esc(strat)}`: {status}")

            strat_text = "\n".join(strat_lines) if strat_lines else "  _kein aktives Deployment_"

            _send_telegram(
                f"🔄 *Regime\\-Wechsel: {_esc(asset)}*\n"
                f"`{_esc(stored)}` → `{_esc(current)}`\n\n"
                f"Strategien:\n{strat_text}"
            )

        except Exception as e:
            errors += 1
            log(f"[REGIME_MONITOR] {asset}: FEHLER {e}")

    conn.commit()

    latency_ms = (time.monotonic() - t0) * 1000
    _write_heartbeat(
        conn,
        status="ok" if errors == 0 else "warn",
        message=f"checked={checked} changes={changes} errors={errors}",
        latency_ms=latency_ms,
    )
    conn.commit()
    conn.close()

    log(f"[REGIME_MONITOR] Fertig: {checked} geprüft | {changes} Wechsel | "
        f"{errors} Fehler | {latency_ms:.0f}ms")


if __name__ == "__main__":
    main()
