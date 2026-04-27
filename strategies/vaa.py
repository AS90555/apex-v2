"""
VAA Strategy Agent — Volume Absorption Anomaly

SHORT-only Strategie. Liest 1h-Candles und Features aus SQLite.
Schreibt Signale in die signals-Tabelle. Kein Order-Code.

Bedingungen (identisch zu V1 vaa_bot.py):
  - Volumen > VAA_VOL_MULT × vol_sma_50
  - Kerzenkörper < VAA_BODY_MULT × body_sma_50
  - Close > EMA(20)  (kurzfristiger Aufwärtstrend)
  - ATR(14) / ATR_SMA(20) > VAA_ATR_EXPAND  (F-06 ATR-Expansion)

SL  = Candle-High der Anomalie-Kerze
TP  = Entry - (SL - Entry) × VAA_TP_R
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection
from core.models import Signal
from core.utils import log, now_iso
from core.state import get_strategy_mode
from strategies.base import BaseStrategy
from config.settings import (
    VAA_ENABLED, VAA_ASSETS,
    VAA_VOL_MULT, VAA_BODY_MULT, VAA_ATR_EXPAND, VAA_TP_R, VAA_ENTRY_WINDOW,
    VAA_MAX_RISK_PCT, CAPITAL, TELEGRAM_V2_PREFIX,
)


def _get_latest_candle(conn, asset: str, interval: str) -> dict | None:
    row = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT 1""",
        (asset, interval),
    ).fetchone()
    return dict(row) if row else None


def _get_feature(conn, asset: str, interval: str, ts: int, name: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM features WHERE asset=? AND interval=? AND ts=? AND feature_name=?",
        (asset, interval, ts, name),
    ).fetchone()
    return row[0] if row else None


def _save_signal(conn, signal: Signal) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
           (created_at, strategy, asset, direction, entry_price, stop_loss,
            take_profit_1, take_profit_2, size, risk_usd, session, status, mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signal.created_at, signal.strategy, signal.asset, signal.direction,
         signal.entry_price, signal.stop_loss, signal.take_profit_1, signal.take_profit_2,
         signal.size, signal.risk_usd, signal.session, signal.status, signal.mode),
    )
    conn.commit()
    return cur.lastrowid


class VAAStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "vaa"

    @property
    def assets(self) -> list[str]:
        return VAA_ASSETS

    def generate_signals(self) -> list[Signal]:
        if not VAA_ENABLED:
            return []

        today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_h   = datetime.now(timezone.utc).hour
        conn    = get_connection()
        signals = []

        for asset in VAA_ASSETS:
            mode = get_strategy_mode("vaa", asset)

            # Neueste abgeschlossene 1h-Kerze (candle[-2] wäre sicherer, aber
            # der Intake läuft nach Kerzenabschluss → letzte Kerze = abgeschlossen)
            candle = _get_latest_candle(conn, asset, "1h")
            if not candle:
                log(f"[VAA] {asset}: Keine 1h-Candle in DB → Skip")
                continue

            ts = candle["ts"]

            # Features aus Feature-Registry lesen
            vol_sma50  = _get_feature(conn, asset, "1h", ts, "vol_sma_50_1h")
            body_sma50 = _get_feature(conn, asset, "1h", ts, "body_sma_50_1h")
            ema20      = _get_feature(conn, asset, "1h", ts, "ema_20_1h")
            atr14      = _get_feature(conn, asset, "1h", ts, "atr_14_1h")
            atr_sma20  = _get_feature(conn, asset, "1h", ts, "atr_sma_20_1h")

            # Fail-safe: wenn Features fehlen → kein Block, Skip mit Log
            if not all([vol_sma50, body_sma50, ema20]):
                log(f"[VAA] {asset}: Feature(s) fehlen (vol_sma50={vol_sma50}, body_sma50={body_sma50}, ema20={ema20}) → Skip")
                continue

            # ── VAA-Bedingungen ───────────────────────────────────────────────
            vol_ratio  = candle["volume"] / vol_sma50  if vol_sma50  > 0 else 0.0
            body       = abs(candle["open"] - candle["close"])
            body_ratio = body / body_sma50 if body_sma50 > 0 else 0.0
            atr_ratio  = atr14 / atr_sma20 if (atr14 and atr_sma20 and atr_sma20 > 0) else 0.0

            big_vol    = vol_ratio  > VAA_VOL_MULT        # Volumen-Anomalie
            small_body = body_ratio < VAA_BODY_MULT        # kleiner Kerzenkörper
            trend_up   = candle["close"] > ema20           # kurzfristiger Aufwärtstrend
            atr_expand = atr_ratio > VAA_ATR_EXPAND        # F-06: ATR-Expansion

            log(f"[VAA] {asset}: vol={vol_ratio:.2f}x body={body_ratio:.2f}x "
                f"trend={'↑' if trend_up else '↓'} atr_exp={'✓' if atr_expand else '✗'}")

            if not (big_vol and small_body and trend_up and atr_expand):
                continue

            # ── Signal-Sizing ─────────────────────────────────────────────────
            sl     = candle["high"]      # SL = Kerzen-High der Anomalie
            entry  = candle["close"]     # Sell-Stop = Close der Anomalie-Kerze
            sl_dist = abs(sl - entry)
            if sl_dist <= 0:
                continue

            risk_usd = CAPITAL * VAA_MAX_RISK_PCT
            size     = risk_usd / sl_dist
            tp1      = entry - sl_dist * 1.0   # 1R
            tp2      = entry - sl_dist * VAA_TP_R  # 3R

            # ── Duplikat-Schutz ───────────────────────────────────────────────
            existing = conn.execute(
                """SELECT id FROM signals
                   WHERE strategy='vaa' AND asset=? AND date(created_at)=? AND mode=?
                   AND status NOT IN ('rejected', 'expired')""",
                (asset, today, mode),
            ).fetchone()
            if existing:
                log(f"[VAA] {asset}: Signal für {today} bereits vorhanden → Skip")
                continue

            signal = Signal(
                strategy="vaa", asset=asset, direction="short", mode=mode,
                entry_price=round(entry, 6), stop_loss=round(sl, 6),
                take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6),
                size=round(size, 4), risk_usd=round(risk_usd, 4),
                session="1h_scan", created_at=now_iso(), status="pending",
            )
            sig_id = _save_signal(conn, signal)
            signal.id = sig_id
            signals.append(signal)
            log(f"[VAA] {TELEGRAM_V2_PREFIX} Signal: {asset} SHORT @ {entry:.6f} | SL={sl:.6f} | TP2={tp2:.6f} | mode={mode}")

        conn.close()
        return signals
