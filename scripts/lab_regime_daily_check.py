"""
Täglicher Regime-Check für alle Assets.

Cron: 0 6 * * * python3 /root/apex-v2/scripts/lab_regime_daily_check.py

CLI:
    python3 scripts/lab_regime_daily_check.py --assets BTC ETH SOL XRP LINK
    python3 scripts/lab_regime_daily_check.py --assets BTC --send-telegram
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent.parent / "config" / ".env")
except ImportError:
    pass

from core.lab_regime_detector import daily_snapshot
from core.lab_state_db import get_lab_state_connection, LAB_STATE_DB_PATH
from core.telegram_dispatcher import dispatch
from core.utils import log

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "XRP", "LINK"]
LOOKBACK_DAYS = 60


def _load_prices(asset: str, days: int) -> list[float]:
    """Lädt Close-Preise aus Bitget-OHLCV für den Regime-Check."""
    try:
        import time
        import requests
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 24 * 3600 * 1000
        symbol = f"{asset}USDT"
        url = "https://api.bitget.com/api/v2/mix/market/candles"
        params = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "granularity": "1D",
            "startTime": str(start_ms),
            "endTime": str(end_ms),
            "limit": "200",
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        candles = data.get("data", [])
        closes = [float(c[4]) for c in candles if len(c) > 4]
        return closes
    except Exception as e:
        log(f"[regime-daily] FEHLER beim Laden von {asset}-Preisen: {e}")
        return []


def _send_drift_telegram(asset: str, prev: str, current: str) -> None:
    """E.1 — Regime-Wechsel Push-Alert via Dispatcher (Dedupe/Rate-Limit inklusive)."""
    try:
        dispatch(
            f"📊 <b>Regime-Wechsel: {asset}</b>\n"
            f"{prev} → {current}"
        )
    except Exception as exc:
        log(f"[regime-daily] Telegram-Fehler: {exc}")


def run_daily_check(
    assets: list[str],
    db_path: str = LAB_STATE_DB_PATH,
    send_telegram: bool = False,
) -> dict[str, str]:
    """Gibt dict[asset -> regime] zurück."""
    conn = get_lab_state_connection(db_path)
    results = {}

    for asset in assets:
        log(f"[regime-daily] Pruefe {asset}...")
        prices = _load_prices(asset, LOOKBACK_DAYS)
        if not prices:
            log(f"[regime-daily] {asset}: Keine Preisdaten — ueberspringe")
            results[asset] = "UNKNOWN"
            continue

        entry = daily_snapshot(asset, conn, prices)
        results[asset] = entry.regime

        if entry.change_detected and send_telegram:
            _send_drift_telegram(
                asset=asset,
                prev=entry.prev_regime or "UNKNOWN",
                current=entry.regime,
            )

        hurst_str = f"{entry.hurst_exponent:.3f}" if entry.hurst_exponent is not None else "N/A"
        log(
            f"[regime-daily] {asset}: regime={entry.regime} "
            f"hurst={hurst_str} "
            f"change={entry.change_detected}"
        )

    conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Taeglicher Regime-Check")
    parser.add_argument("--assets", nargs="+", default=DEFAULT_ASSETS)
    parser.add_argument("--db-path", default=LAB_STATE_DB_PATH)
    parser.add_argument("--send-telegram", action="store_true")
    args = parser.parse_args()

    results = run_daily_check(args.assets, args.db_path, args.send_telegram)
    for asset, regime in results.items():
        log(f"[regime-daily] Ergebnis: {asset} -> {regime}")


if __name__ == "__main__":
    main()
