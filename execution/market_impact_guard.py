"""
Market-Impact-Guard (Phase 7).

Bestimmt Order-Typ und IOC-Toleranz adaptiv basierend auf:
  - Live-Orderbuch-Snapshot (Stufe 1)
  - 24h-Liquiditäts-Matrix aus asset_liquidity_metrics (Stufe 2)
  - Regime-Zustand (Stufe 2)

Rückgabe: MarketImpactDecision mit order_type ('market' | 'ioc_limit')
und ioc_tolerance_bps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.db import get_connection
from core.utils import log
from config.settings import (
    MARKET_IMPACT_THRESHOLD,
    IOC_SLIPPAGE_TOLERANCE_BASE,
    WORST_CASE_TOLERANCE,
    LIQUIDITY_DEGRADATION_THRESHOLD,
    LIQUIDITY_STRESS_MULTIPLIER,
    V6_MARKET_IMPACT_GUARD,
)


@dataclass
class MarketImpactDecision:
    order_type:              str    # 'market' | 'ioc_limit'
    ioc_tolerance_bps:       float
    liquidity_score:         float
    spread_at_snapshot_bps:  float
    market_impact_check:     str    # 'ok' | 'degraded' | 'stale' | 'disabled'


def _get_liquidity_metrics(asset: str) -> dict | None:
    """Liest die aktuellsten Liquiditätsdaten aus der DB."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT liquidity_score, avg_spread_bps, avg_depth_level1_usd, measured_at
               FROM asset_liquidity_metrics
               WHERE asset=?
               ORDER BY measured_at DESC LIMIT 1""",
            (asset,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_current_spread_bps(client, asset: str) -> float | None:
    """Holt aktuellen Spread via Orderbuch-Snapshot (Stufe 1)."""
    try:
        symbol = f"{asset}USDT_UMCBL"
        data = client._get(
            "/api/v2/mix/market/merge-depth",
            {"symbol": symbol, "productType": "USDT-FUTURES", "limit": "3"},
        )
        if not data:
            return None
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if not asks or not bids:
            return None
        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        mid = (best_ask + best_bid) / 2
        return (best_ask - best_bid) / mid * 10_000 if mid > 0 else None
    except Exception as e:
        log(f"[MarketImpactGuard] Orderbuch-Fehler {asset}: {e}")
        return None


def _get_regime(asset: str) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            (f"regime_{asset}",),
        ).fetchone()
        return row["value"] if row else "UNDEFINED"
    finally:
        conn.close()


def evaluate(
    asset: str,
    order_size_usd: float,
    client=None,
) -> MarketImpactDecision:
    """
    Gibt MarketImpactDecision zurück.

    Wenn V6_MARKET_IMPACT_GUARD=False → immer Market Order mit Basis-Toleranz.
    """
    if not V6_MARKET_IMPACT_GUARD:
        return MarketImpactDecision(
            order_type="market",
            ioc_tolerance_bps=IOC_SLIPPAGE_TOLERANCE_BASE,
            liquidity_score=1.0,
            spread_at_snapshot_bps=0.0,
            market_impact_check="disabled",
        )

    # Stufe 2: Historische Liquiditätsdaten
    liq = _get_liquidity_metrics(asset)
    liquidity_score = liq["liquidity_score"] if liq else 0.5
    spread_24h_bps  = liq["avg_spread_bps"] if liq else 0.0
    depth_l1        = liq["avg_depth_level1_usd"] if liq else 0.0

    # Stale-Daten → worst-case Toleranz
    if liq:
        try:
            measured = datetime.fromisoformat(liq["measured_at"].replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - measured).total_seconds() / 3600
            if age_h > 48:
                log(f"[MarketImpactGuard] {asset}: Liquiditätsdaten stale ({age_h:.1f}h)")
                return MarketImpactDecision(
                    order_type="ioc_limit",
                    ioc_tolerance_bps=WORST_CASE_TOLERANCE,
                    liquidity_score=liquidity_score,
                    spread_at_snapshot_bps=spread_24h_bps,
                    market_impact_check="stale",
                )
        except Exception:
            pass

    # Stufe 1: Live-Orderbuch-Snapshot
    spread_now_bps = None
    if client is not None:
        spread_now_bps = _get_current_spread_bps(client, asset)

    spread_bps = spread_now_bps if spread_now_bps is not None else spread_24h_bps

    # Regime-Anpassung
    regime = _get_regime(asset)
    regime_factor = {"HIGH_VOL": 1.5, "TREND": 1.0, "SIDEWAYS": 0.8}.get(regime, 1.0)

    # IOC-Toleranz berechnen
    base_tolerance = IOC_SLIPPAGE_TOLERANCE_BASE

    # Liquiditäts-Degradation
    impact_check = "ok"
    if liquidity_score < LIQUIDITY_DEGRADATION_THRESHOLD:
        base_tolerance *= LIQUIDITY_STRESS_MULTIPLIER
        impact_check = "degraded"

    ioc_tolerance = min(base_tolerance * regime_factor, WORST_CASE_TOLERANCE)

    # Stufe 3: Order-Typ bestimmen
    # Kleine Order relativ zu L1-Tiefe → Market Order
    if depth_l1 > 0 and order_size_usd <= MARKET_IMPACT_THRESHOLD * depth_l1:
        order_type = "market"
    else:
        order_type = "ioc_limit"

    log(
        f"[MarketImpactGuard] {asset}: score={liquidity_score:.3f} "
        f"spread={spread_bps:.2f}bps regime={regime} "
        f"→ {order_type} tol={ioc_tolerance:.1f}bps [{impact_check}]"
    )

    return MarketImpactDecision(
        order_type=order_type,
        ioc_tolerance_bps=ioc_tolerance,
        liquidity_score=liquidity_score,
        spread_at_snapshot_bps=spread_bps,
        market_impact_check=impact_check,
    )
