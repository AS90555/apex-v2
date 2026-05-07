"""
HMM Wöchentliches Re-Training — APEX V2 (P-02).
Trainiert GaussianHMM für alle aktiven Deployment-Assets und schreibt Heartbeat.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime, timezone

from core.db import get_connection
from core.utils import log
from research.train_hmm import train_hmm, save_model


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    t0 = time.monotonic()
    log("[hmm_retrain] Start")

    conn = get_connection()

    assets: list[str] = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT asset FROM active_deployments WHERE active=1"
        ).fetchall()
    ]

    if not assets:
        log("[hmm_retrain] Keine aktiven Deployments — nichts zu trainieren")
        _write_heartbeat(conn, "ok", "assets=0", (time.monotonic() - t0) * 1000)
        conn.commit()
        conn.close()
        return

    log(f"[hmm_retrain] Assets: {assets}")
    trained, failed = [], []

    for asset in assets:
        try:
            model, scaler = train_hmm(asset, conn)
            path = save_model(asset, model, scaler)
            log(f"[hmm_retrain] {asset} → {path} (konvergiert={model.monitor_.converged})")
            trained.append(asset)
        except Exception as exc:
            log(f"[hmm_retrain] {asset} FEHLER: {exc}")
            failed.append(asset)

    latency_ms = (time.monotonic() - t0) * 1000
    status = "ok" if not failed else "warn"
    message = f"trained={trained} failed={failed}"
    _write_heartbeat(conn, status, message, latency_ms)
    conn.commit()
    conn.close()
    log(f"[hmm_retrain] Fertig: {len(trained)} OK, {len(failed)} FEHLER | {latency_ms:.0f}ms")


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) VALUES (?,?,?,?,?)",
        (_now(), "hmm_retrain", status, message, round(latency_ms, 1)),
    )


if __name__ == "__main__":
    main()
