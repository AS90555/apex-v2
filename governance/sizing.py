"""
Constant-Volatility-Targeting (Phase 6, opt-in per Strategie).

size = (capital * TARGET_VOLATILITY_PCT) / atr_value
     × REGIME_SIZE_MULTIPLIERS[regime]
     × funding_adjustment   ← v7 Phase 4

Per default INAKTIV (V6_VOL_TARGETING=False). Opt-in via Strategie-Config.
RISK_USDT bleibt als Risikobudget-Cap unverändert (Hard Rule #5).

v7 Ergänzung:
  expected_funding_8h reduziert die Positionsgröße risiko-additiv:
    funding_drag = expected_funding_8h × (expected_holding_h / 8)
    funding_adj  = max(0, 1 - FUNDING_SIZE_K × funding_drag / risk_per_trade_pct)
  Aktiviert wenn expected_funding_8h > 0 übergeben wird.
"""
from __future__ import annotations

from core.db import get_connection
from core.utils import log
from config.settings import (
    TARGET_VOLATILITY_PCT,
    ATR_SIZING_PERIOD,
    REGIME_SIZE_MULTIPLIERS,
    RISK_USDT,
    V6_VOL_TARGETING,
    FUNDING_SIZE_K,
)


def _get_atr(asset: str, interval: str = "1h", period: int = ATR_SIZING_PERIOD) -> float | None:
    """Liest den letzten ATR-Wert aus der features_cache-Tabelle."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT value FROM features_cache
               WHERE asset=? AND interval=? AND feature=?
               ORDER BY ts DESC LIMIT 1""",
            (asset, interval, f"atr_{period}"),
        ).fetchone()
        return float(row["value"]) if row else None
    except Exception as e:
        log(f"[Sizing] ATR-Fehler für {asset}: {e}")
        return None
    finally:
        conn.close()


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


def _funding_adjustment(
    expected_funding_8h: float,
    expected_holding_h: float,
    risk_per_trade_pct: float,
) -> float:
    """
    Risiko-additiver Funding-Adjuster (v7 Phase 4).

    Reduziert Positionsgröße um den erwarteten Funding-Drag relativ zum
    Trade-Risiko. Gibt Faktor in [0, 1] zurück.
    """
    if expected_funding_8h <= 0 or risk_per_trade_pct <= 0:
        return 1.0
    funding_drag = abs(expected_funding_8h) * (max(expected_holding_h, 1.0) / 8.0)
    adj = 1.0 - FUNDING_SIZE_K * funding_drag / risk_per_trade_pct
    return max(0.0, adj)


def compute_position_size(
    asset: str,
    entry_price: float,
    sl_distance: float,
    capital: float,
    interval: str = "1h",
    regime: str | None = None,
    expected_funding_8h: float = 0.0,
    expected_holding_h: float = 8.0,
) -> float:
    """
    Gibt optimale Positionsgröße zurück.

    Bei V6_VOL_TARGETING=False → klassische RISK_USDT/sl_distance Logik.
    Bei V6_VOL_TARGETING=True  → ATR-skaliert mit Regime-Multiplikator.

    sl_distance: absoluter Abstand Entry→SL (immer positiv).
    capital: verfügbares Kapital in USDT.
    expected_funding_8h: aktuelle Funding-Rate (8h-Periode). 0 = kein Adjustment.
    expected_holding_h: erwartete Haltedauer in Stunden (für Funding-Drag-Kalkulation).
    """
    if sl_distance <= 0 or entry_price <= 0:
        return 0.0

    # Funding-Adjustment (v7) — gilt für beide Sizing-Pfade
    risk_pct = sl_distance / entry_price if entry_price > 0 else 0.01
    f_adj = _funding_adjustment(expected_funding_8h, expected_holding_h, risk_pct)

    if not V6_VOL_TARGETING:
        size = RISK_USDT / sl_distance * f_adj
        if f_adj < 1.0:
            log(f"[Sizing] {asset} funding_adj={f_adj:.4f} (funding={expected_funding_8h:.5f})")
        return size

    # Vol-Targeting
    atr = _get_atr(asset, interval)
    if atr is None or atr <= 0:
        log(f"[Sizing] Kein ATR für {asset} — Fallback auf Legacy-Sizing")
        return RISK_USDT / sl_distance * f_adj

    if regime is None:
        regime = _get_regime(asset)

    multiplier = REGIME_SIZE_MULTIPLIERS.get(regime, REGIME_SIZE_MULTIPLIERS.get("UNDEFINED", 0.25))
    raw_size = (capital * TARGET_VOLATILITY_PCT) / atr * multiplier * f_adj

    # Cap: nie mehr als das klassische RISK_USDT-Budget erlaubt
    max_size = RISK_USDT / sl_distance
    size = min(raw_size, max_size)

    log(
        f"[Sizing] {asset} atr={atr:.4f} regime={regime} mult={multiplier} "
        f"f_adj={f_adj:.4f} raw={raw_size:.4f} cap={max_size:.4f} → size={size:.4f}"
    )
    return size


def get_latest_funding_rate(asset: str) -> float:
    """Liest die jüngste Funding-Rate aus der DB. Gibt 0.0 zurück bei Fehler."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT funding_rate FROM funding_rates
               WHERE asset=? ORDER BY funding_time DESC LIMIT 1""",
            (asset,),
        ).fetchone()
        return float(row["funding_rate"]) if row else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()
