"""
AsianFade Strategy — Asien-Pump / London-Open Mean Reversion

Hypothese: Krypto pumpt häufig in der Asien-Session (00:00–08:00 UTC) bei
dünnem Volumen. Wenn London aufwacht und der Pump unnatürlich groß ist
(RSI overbought), wird er abverkauft.

Bedingungen (alle müssen zur vollen Stunde 08:00 UTC erfüllt sein):
  1. Zeit = 08:00 UTC (London Open)
  2. Overnight Pump: Close@08:00 > Close@00:00 + PUMP_THRESHOLD (1.5%)
  3. RSI(14) auf 1h > RSI_OB (70)

Entry:  Close der 08:00-UTC-Kerze (Sell-Stop / Market-Short)
SL:     Entry + ATR(14) × SL_ATR_MULT  (über Entry = Short-SL)
TP:     Entry - (SL − Entry) × TP_MULT (1.5R)
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
from features.indicators import atr_wilder, rsi as calc_rsi
from config.settings import (
    ASIAN_FADE_ENABLED, ASIAN_FADE_ASSET,
    ASIAN_FADE_PUMP_THRESHOLD, ASIAN_FADE_RSI_OB,
    ASIAN_FADE_SL_ATR_MULT, ASIAN_FADE_TP_MULT,
    ASIAN_FADE_MAX_RISK_PCT, CAPITAL, TELEGRAM_V2_PREFIX,
)

ENTRY_HOUR_UTC = 8   # London Open


def _get_candles(conn, asset: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval='1h' ORDER BY ts DESC LIMIT ?""",
        (asset, limit),
    ).fetchall()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in reversed(rows)]


def _make_signal_key(signal: Signal) -> str:
    from datetime import datetime, timezone
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{signal.strategy}__{signal.asset}__{signal.session or 'nosess'}__{signal.mode}__{bucket}"


def _save_signal(conn, signal: Signal) -> int:
    signal_key = _make_signal_key(signal)
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
           (created_at, strategy, asset, direction, entry_price, stop_loss,
            take_profit_1, take_profit_2, size, risk_usd, session, status, mode, signal_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signal.created_at, signal.strategy, signal.asset, signal.direction,
         signal.entry_price, signal.stop_loss, signal.take_profit_1, signal.take_profit_2,
         signal.size, signal.risk_usd, signal.session, signal.status, signal.mode, signal_key),
    )
    conn.commit()
    return cur.lastrowid


class AsianFadeStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "asian_fade"

    @property
    def assets(self) -> list[str]:
        return [ASIAN_FADE_ASSET]

    def generate_signals(self) -> list[Signal]:
        if not ASIAN_FADE_ENABLED:
            return []

        now = datetime.now(timezone.utc)

        # ── Bedingung 1: Zeit = 08:00 UTC ────────────────────────────────────
        if now.hour != ENTRY_HOUR_UTC or now.minute > 5:
            log(f"[ASIAN_FADE] Kein London-Open-Fenster ({now.hour:02d}:{now.minute:02d} UTC) → Skip")
            return []

        today  = now.strftime("%Y-%m-%d")
        asset  = ASIAN_FADE_ASSET
        mode   = get_strategy_mode("asian_fade", asset)
        conn   = get_connection()

        # 30 1h-Candles laden (RSI braucht mindestens 15, ATR 14)
        candles = _get_candles(conn, asset, 30)
        if len(candles) < 16:
            log(f"[ASIAN_FADE] {asset}: Zu wenige Candles ({len(candles)}) → Skip")
            conn.close()
            return []

        # ── Bedingung 2: Overnight Pump ───────────────────────────────────────
        # Letzte Candle = 08:00-Kerze, Midnight-Candle = 8 Bars davor
        current = candles[-1]
        midnight_idx = None
        for i, c in enumerate(candles):
            dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            if dt.hour == 0:
                midnight_idx = i

        if midnight_idx is None:
            log(f"[ASIAN_FADE] {asset}: Midnight-Candle (00:00 UTC) nicht gefunden → Skip")
            conn.close()
            return []

        midnight_close  = candles[midnight_idx]["close"]
        current_close   = current["close"]
        pump_pct        = (current_close - midnight_close) / midnight_close

        log(f"[ASIAN_FADE] {asset}: Pump seit Midnight = {pump_pct:+.2%} "
            f"(threshold: {ASIAN_FADE_PUMP_THRESHOLD:+.1%})")

        if pump_pct < ASIAN_FADE_PUMP_THRESHOLD:
            log(f"[ASIAN_FADE] {asset}: Pump {pump_pct:+.2%} < {ASIAN_FADE_PUMP_THRESHOLD:.1%} → kein Signal")
            conn.close()
            return []

        # ── Bedingung 3: RSI(14) > 70 ─────────────────────────────────────────
        rsi_val = calc_rsi(candles, period=14)
        log(f"[ASIAN_FADE] {asset}: RSI(14) = {rsi_val:.1f} (Schwelle: {ASIAN_FADE_RSI_OB})")

        if rsi_val < ASIAN_FADE_RSI_OB:
            log(f"[ASIAN_FADE] {asset}: RSI {rsi_val:.1f} < {ASIAN_FADE_RSI_OB} → kein Signal")
            conn.close()
            return []

        # ── Signal-Sizing ─────────────────────────────────────────────────────
        atr     = atr_wilder(candles, period=14)
        if atr <= 0:
            log(f"[ASIAN_FADE] {asset}: ATR=0 → Skip")
            conn.close()
            return []

        entry   = current_close
        sl_dist = atr * ASIAN_FADE_SL_ATR_MULT
        sl      = entry + sl_dist          # Short: SL liegt über Entry
        tp_dist = sl_dist * ASIAN_FADE_TP_MULT
        tp      = entry - tp_dist          # Short: TP liegt unter Entry

        risk_usd = CAPITAL * ASIAN_FADE_MAX_RISK_PCT
        size     = risk_usd / sl_dist if sl_dist > 0 else 0.0

        # ── Duplikat-Schutz ───────────────────────────────────────────────────
        existing = conn.execute(
            """SELECT id FROM signals
               WHERE strategy='asian_fade' AND asset=? AND date(created_at)=? AND mode=?
               AND status NOT IN ('rejected', 'expired')""",
            (asset, today, mode),
        ).fetchone()
        if existing:
            log(f"[ASIAN_FADE] {asset}: Signal für {today} bereits vorhanden → Skip")
            conn.close()
            return []

        signal = Signal(
            strategy="asian_fade", asset=asset, direction="short", mode=mode,
            entry_price=round(entry, 4), stop_loss=round(sl, 4),
            take_profit_1=round(tp, 4), take_profit_2=round(tp, 4),
            size=round(size, 4), risk_usd=round(risk_usd, 4),
            session="london_open", created_at=now_iso(), status="pending",
        )
        sig_id = _save_signal(conn, signal)
        signal.id = sig_id
        conn.close()

        log(f"[ASIAN_FADE] {TELEGRAM_V2_PREFIX} Signal: {asset} SHORT @ {entry:.4f} "
            f"pump={pump_pct:+.2%} RSI={rsi_val:.1f} | SL={sl:.4f} | TP={tp:.4f} | "
            f"atr={atr:.4f} | mode={mode}")
        return [signal]
