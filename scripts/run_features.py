#!/usr/bin/env python3
"""
Cron Entry-Point: Feature-Berechnung
Läuft direkt nach run_intake.py.
Aufruf: */5 * * * * sleep 20 && python3 /root/apex-v2/scripts/run_features.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection, run_migrations, set_state
from core.utils import log
from config.settings import INTAKE_MATRIX
from features.feature_agent import run_all_features
from features.indicators import detect_regime
from core.autopilot import check_regime_change


# Assets für Regime-Detection: nur jene mit 1h-Daten
REGIME_ASSETS = [a for a, intervals in INTAKE_MATRIX.items() if "1h" in intervals]
REGIME_MIN_CANDLES = 70   # EMA(50) + 15 Puffer + 5 Reserve


_STALE_CANDLE_MINUTES = 180   # Candle älter als 3h → Regime-Update überspringen


def _compute_and_store_regimes():
    """Berechnet das aktuelle Markt-Regime für alle Assets und speichert in system_state."""
    import time as _time
    conn = get_connection()
    now_ms = int(_time.time() * 1000)
    updated = []
    for asset in REGIME_ASSETS:
        rows = conn.execute(
            """SELECT open, high, low, close, volume, ts FROM candles
               WHERE asset=? AND interval='1h'
               ORDER BY ts DESC LIMIT ?""",
            (asset, REGIME_MIN_CANDLES),
        ).fetchall()

        if len(rows) < REGIME_MIN_CANDLES:
            log(f"[run_features] Regime {asset}: zu wenig Candles ({len(rows)}<{REGIME_MIN_CANDLES})")
            continue

        # Fallback-Schutz: letzten Candle auf Frische prüfen
        last_candle_ts = rows[0][5]   # DESC → erstes Element ist neuester
        age_min = (now_ms - last_candle_ts) / 60_000
        if age_min > _STALE_CANDLE_MINUTES:
            log(f"[run_features] ⚠️ Regime {asset}: letzter Candle {age_min:.0f} Min alt "
                f"— Regime-Update übersprungen (Fallback-Schutz)")
            continue

        # Umkehren: DESC → ASC für Indikator-Berechnung
        candles = [{"open": r[0], "high": r[1], "low": r[2],
                    "close": r[3], "volume": r[4]} for r in reversed(rows)]
        regime  = detect_regime(candles)
        set_state(f"regime_{asset}", regime)
        updated.append(f"{asset}={regime}")

        # Auto-Pilot: Regime-Wechsel prüfen + ggf. Auto-Deploy auslösen
        check_regime_change(asset, regime)

    conn.close()
    return updated


def write_heartbeat(status: str, message: str, latency_ms: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "features", status, message, latency_ms),
    )
    conn.commit()
    conn.close()


def main():
    run_migrations()
    t0 = time.monotonic()
    log("[run_features] Start")

    try:
        summary  = run_all_features(INTAKE_MATRIX)
        computed = sum(v for v in summary.values() if v > 0)

        regimes  = _compute_and_store_regimes()
        log(f"[run_features] Regimes: {' | '.join(regimes)}")

        latency  = (time.monotonic() - t0) * 1000
        message  = f"features_computed={computed} regimes={len(regimes)}"
        write_heartbeat("ok", message, latency)
        log(f"[run_features] Fertig: {message} ({latency:.0f}ms)")

    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        write_heartbeat("error", str(e)[:200], latency)
        log(f"[run_features] KRITISCHER FEHLER: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
