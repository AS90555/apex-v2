"""
KDT Strategy Agent — Kinetic Deceleration Trap

ETH SHORT-only, 1h-Timeframe. Liest Candles und Features aus SQLite.
Schreibt Signale in die signals-Tabelle. Kein Order-Code.

Bedingungen (identisch zu V1 kdt_bot.py):
  1. Close[-1] > EMA(50)                    — Aufwärtstrend
  2. Letzte 3 Kerzen grün (close > open)
  3. Schrumpfende Bodies: body[-1] < body[-2] < body[-3]
  4. Schrumpfendes Volumen: vol[-1] < vol[-2] < vol[-3]
  5. F-04 Tight-SL: SL-Distanz < KDT_SL_ATR_MULT × ATR(14)

SL    = candle[-1].high
Entry = candle[-1].low (Sell-Stop)
TP    = entry - (SL - entry) × KDT_TP_R
"""

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
    KDT_ENABLED, KDT_ASSET,
    KDT_TP_R, KDT_SL_ATR_MULT, KDT_MAX_RISK_PCT, CAPITAL, TELEGRAM_V2_PREFIX,
)


def _get_candles(conn, asset: str, interval: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT ?""",
        (asset, interval, limit),
    ).fetchall()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in reversed(rows)]


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


class KDTStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "kdt"

    @property
    def assets(self) -> list[str]:
        return [KDT_ASSET]

    def generate_signals(self) -> list[Signal]:
        if not KDT_ENABLED:
            return []

        today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        asset  = KDT_ASSET
        mode   = get_strategy_mode("kdt", asset)
        conn   = get_connection()

        # Letzte 5 Kerzen laden (brauchen 3 für Muster + 2 Puffer)
        candles = _get_candles(conn, asset, "1h", 5)
        if len(candles) < 4:
            log(f"[KDT] {asset}: Zu wenige Candles ({len(candles)}) → Skip")
            conn.close()
            return []

        # Features der neuesten Kerze
        ts_last = candles[-1]["time"]
        ema50  = _get_feature(conn, asset, "1h", ts_last, "ema_50_1h")
        atr14  = _get_feature(conn, asset, "1h", ts_last, "atr_14_1h")

        if not ema50 or not atr14 or ema50 <= 0 or atr14 <= 0:
            log(f"[KDT] {asset}: Features fehlen (ema50={ema50}, atr14={atr14}) → Skip")
            conn.close()
            return []

        # Die 3 maßgeblichen Kerzen: c0 = neueste, c1 = vorherige, c2 = davor
        c0, c1, c2 = candles[-1], candles[-2], candles[-3]

        body0 = abs(c0["close"] - c0["open"])
        body1 = abs(c1["close"] - c1["open"])
        body2 = abs(c2["close"] - c2["open"])

        # ── KDT-Bedingungen ───────────────────────────────────────────────────
        cond_trend    = c0["close"] > ema50
        cond_green    = (c0["close"] > c0["open"] and
                         c1["close"] > c1["open"] and
                         c2["close"] > c2["open"])
        cond_bodies   = body0 < body1 < body2 and body0 > 0
        cond_volumes  = c0["volume"] < c1["volume"] < c2["volume"]

        sl_price = c0["high"]
        entry    = c0["low"]
        sl_dist  = sl_price - entry
        cond_sl  = (sl_dist > 0 and sl_dist < KDT_SL_ATR_MULT * atr14
                    and 0.0005 < sl_dist / entry < 0.15)

        log(f"[KDT] {asset}: trend={'✓' if cond_trend else '✗'} green={'✓' if cond_green else '✗'} "
            f"bodies={'✓' if cond_bodies else '✗'} vols={'✓' if cond_volumes else '✗'} sl={'✓' if cond_sl else '✗'}")

        if not all([cond_trend, cond_green, cond_bodies, cond_volumes, cond_sl]):
            conn.close()
            return []

        # ── Duplikat-Schutz ───────────────────────────────────────────────────
        existing = conn.execute(
            """SELECT id FROM signals
               WHERE strategy='kdt' AND asset=? AND date(created_at)=? AND mode=?
               AND status NOT IN ('rejected', 'expired')""",
            (asset, today, mode),
        ).fetchone()
        if existing:
            log(f"[KDT] {asset}: Signal für {today} bereits vorhanden → Skip")
            conn.close()
            return []

        # ── Signal-Sizing ─────────────────────────────────────────────────────
        risk_usd = CAPITAL * KDT_MAX_RISK_PCT
        size     = risk_usd / sl_dist
        tp1      = entry - sl_dist * 1.0
        tp2      = entry - sl_dist * KDT_TP_R

        signal = Signal(
            strategy="kdt", asset=asset, direction="short", mode=mode,
            entry_price=round(entry, 4), stop_loss=round(sl_price, 4),
            take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
            size=round(size, 4), risk_usd=round(risk_usd, 4),
            session="1h_scan", created_at=now_iso(), status="pending",
        )
        sig_id = _save_signal(conn, signal)
        signal.id = sig_id
        conn.close()

        log(f"[KDT] {TELEGRAM_V2_PREFIX} Signal: {asset} SHORT @ {entry:.4f} | SL={sl_price:.4f} | TP2={tp2:.4f} | mode={mode}")
        return [signal]
