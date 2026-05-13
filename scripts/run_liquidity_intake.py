"""
Liquidity-Intake (Phase 7) — Cron 1× täglich (z. B. 02:00 UTC).

Holt Orderbuch-Snapshot von Bitget pro Asset und schreibt
Spread, Depth-L1, Depth-L3 und Liquidity-Score in asset_liquidity_metrics.
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

_ORDERBOOK_LEVELS = 10  # Orderbuch-Tiefe für Depth-Berechnung


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "liquidity_intake", status, message, round(latency_ms, 1)),
    )


def _compute_liquidity_score(spread_bps: float, depth_l1: float, depth_l3: float) -> float:
    """
    Score 0–1.

    Formel: normalisierte Kombination aus inversem Spread und Tiefe.
    Spread 0bps = perfekt, 50bps = sehr schlecht.
    Depth L1 ≥ 50k USD = sehr gut.
    """
    spread_score = max(0.0, 1.0 - spread_bps / 50.0)
    depth_score  = min(1.0, depth_l1 / 50_000.0)
    return round(0.5 * spread_score + 0.5 * depth_score, 4)


def _fetch_orderbook(client, asset: str) -> dict | None:
    """Holt Orderbuch von Bitget, gibt parsed metrics zurück."""
    try:
        symbol = f"{asset}USDT_UMCBL"
        data = client._get(
            "/api/v2/mix/market/merge-depth",
            {"symbol": symbol, "productType": "USDT-FUTURES", "limit": str(_ORDERBOOK_LEVELS)},
        )
        if not data:
            return None

        asks = data.get("asks", [])
        bids = data.get("bids", [])

        if not asks or not bids:
            return None

        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        mid      = (best_ask + best_bid) / 2
        spread_bps = (best_ask - best_bid) / mid * 10_000 if mid > 0 else 0.0

        def _depth_usd(levels: list, n: int) -> float:
            total = 0.0
            for i, level in enumerate(levels):
                if i >= n:
                    break
                price = float(level[0])
                qty   = float(level[1])
                total += price * qty
            return total

        depth_l1 = _depth_usd(asks, 1) + _depth_usd(bids, 1)
        depth_l3 = _depth_usd(asks, 3) + _depth_usd(bids, 3)

        score = _compute_liquidity_score(spread_bps, depth_l1, depth_l3)
        return {
            "avg_spread_bps":      round(spread_bps, 4),
            "avg_depth_level1_usd": round(depth_l1, 2),
            "avg_depth_level3_usd": round(depth_l3, 2),
            "liquidity_score":     score,
        }
    except Exception as e:
        log(f"[LIQUIDITY_INTAKE] Fehler bei {asset}: {e}")
        return None


def intake_once() -> dict:
    t0 = time.monotonic()
    conn = get_connection()
    written = 0

    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient()
    except Exception as e:
        log(f"[LIQUIDITY_INTAKE] BitgetClient-Fehler: {e}")
        _write_heartbeat(conn, "error", f"client_init_failed: {e}", 0)
        conn.commit()
        conn.close()
        return {"written": 0}

    now_iso = _now_iso()
    for asset in LIVE_ASSETS:
        metrics = _fetch_orderbook(client, asset)
        if not metrics:
            log(f"[LIQUIDITY_INTAKE] Keine Daten für {asset} — übersprungen")
            continue

        conn.execute(
            """INSERT INTO asset_liquidity_metrics
               (asset, avg_spread_bps, avg_depth_level1_usd, avg_depth_level3_usd,
                liquidity_score, measured_at, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                asset,
                metrics["avg_spread_bps"],
                metrics["avg_depth_level1_usd"],
                metrics["avg_depth_level3_usd"],
                metrics["liquidity_score"],
                now_iso,
                now_iso,
            ),
        )
        log(
            f"[LIQUIDITY_INTAKE] {asset}: spread={metrics['avg_spread_bps']:.2f}bps "
            f"depth_l1=${metrics['avg_depth_level1_usd']:,.0f} "
            f"score={metrics['liquidity_score']:.3f}"
        )
        written += 1

    latency_ms = (time.monotonic() - t0) * 1000
    summary = f"assets={len(LIVE_ASSETS)} written={written}"
    _write_heartbeat(conn, "ok" if written > 0 else "warning", summary, latency_ms)
    conn.commit()
    conn.close()
    log(f"[LIQUIDITY_INTAKE] Fertig — {summary} ({latency_ms:.0f}ms)")
    return {"written": written}


if __name__ == "__main__":
    intake_once()
