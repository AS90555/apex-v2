"""
Constant-Volatility-Targeting (Phase 6, opt-in per Strategie).

size = (capital * TARGET_VOLATILITY_PCT) / atr_value
     × REGIME_SIZE_MULTIPLIERS[regime]

Per default INAKTIV (V6_VOL_TARGETING=False). Opt-in via Strategie-Config.
RISK_USDT bleibt als Risikobudget-Cap unverändert (Hard Rule #5).
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


def compute_position_size(
    asset: str,
    entry_price: float,
    sl_distance: float,
    capital: float,
    interval: str = "1h",
    regime: str | None = None,
) -> float:
    """
    Gibt optimale Positionsgröße zurück.

    Bei V6_VOL_TARGETING=False → klassische RISK_USDT/sl_distance Logik.
    Bei V6_VOL_TARGETING=True  → ATR-skaliert mit Regime-Multiplikator.

    sl_distance: absoluter Abstand Entry→SL (immer positiv).
    capital: verfügbares Kapital in USDT.
    """
    if sl_distance <= 0 or entry_price <= 0:
        return 0.0

    if not V6_VOL_TARGETING:
        # Legacy: festes Risiko-Budget
        return RISK_USDT / sl_distance

    # Vol-Targeting
    atr = _get_atr(asset, interval)
    if atr is None or atr <= 0:
        log(f"[Sizing] Kein ATR für {asset} — Fallback auf Legacy-Sizing")
        return RISK_USDT / sl_distance

    if regime is None:
        regime = _get_regime(asset)

    multiplier = REGIME_SIZE_MULTIPLIERS.get(regime, REGIME_SIZE_MULTIPLIERS.get("UNDEFINED", 0.25))
    raw_size = (capital * TARGET_VOLATILITY_PCT) / atr * multiplier

    # Cap: nie mehr als das klassische RISK_USDT-Budget erlaubt
    max_size = RISK_USDT / sl_distance
    size = min(raw_size, max_size)

    log(
        f"[Sizing] {asset} atr={atr:.4f} regime={regime} mult={multiplier} "
        f"raw={raw_size:.4f} cap={max_size:.4f} → size={size:.4f}"
    )
    return size
