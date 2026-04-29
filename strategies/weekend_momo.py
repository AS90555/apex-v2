"""
WeekendMomo Strategy Agent — AVAX Wochenend-Momentum

Liest 1d- und 4h-Candles aus SQLite.
Schreibt Signale in die signals-Tabelle. Kein Order-Code.

Strategie (identisch zu V1 weekend_momo.py):
  - 3-Tage-Momentum: Freitag-Close / Dienstag-Close - 1
  - |Momentum| >= MOMENTUM_THRESHOLD (3%)  → Trade in Momentum-Richtung
  - Entry: Samstag ~00:05 UTC
  - SL:  ATR_SL_MULTIPLIER × ATR(14) auf 4h
  - TP:  ATR_TP_MULTIPLIER × ATR(14)
  - Exit: Sonntagabend wenn SL/TP nicht getroffen
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
from features.indicators import atr_wilder
from config.settings import (
    WEEKEND_ASSET, MOMENTUM_THRESHOLD, ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER,
    MAX_RISK_PCT, CAPITAL, TELEGRAM_V2_PREFIX,
)


def _get_candles(conn, asset: str, interval: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT ?""",
        (asset, interval, limit),
    ).fetchall()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in reversed(rows)]


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


def _calc_3day_momentum(candles_1d: list[dict]) -> tuple[float | None, float | None, float | None]:
    """
    Berechnet Freitag-Close / Dienstag-Close - 1.
    Gibt (momentum, tue_close, fri_close) zurück.
    """
    tuesday_close = None
    friday_close  = None

    for c in candles_1d:
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.weekday()
        if day == 1:   # Dienstag
            tuesday_close = c["close"]
        elif day == 4: # Freitag
            friday_close = c["close"]

    if not tuesday_close or not friday_close:
        return None, None, None

    return (friday_close / tuesday_close) - 1, tuesday_close, friday_close


class WeekendMomoStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "weekend_momo"

    @property
    def assets(self) -> list[str]:
        return [WEEKEND_ASSET]

    def generate_signals(self) -> list[Signal]:
        # Nur Samstags ausführen (UTC)
        now = datetime.now(timezone.utc)
        if now.weekday() != 5:  # 5 = Samstag
            log("[WEEKEND_MOMO] Kein Wochenende → Skip")
            return []

        today = now.strftime("%Y-%m-%d")
        asset = WEEKEND_ASSET
        mode  = get_strategy_mode("weekend_momo", asset)
        conn  = get_connection()

        # ── 1d-Candles für Momentum-Berechnung ───────────────────────────────
        candles_1d = _get_candles(conn, asset, "1d", 10)
        if len(candles_1d) < 5:
            log(f"[WEEKEND_MOMO] {asset}: Zu wenige 1d-Candles ({len(candles_1d)}) → Skip")
            conn.close()
            return []

        momentum, tue_close, fri_close = _calc_3day_momentum(candles_1d)
        if momentum is None:
            log(f"[WEEKEND_MOMO] {asset}: Momentum nicht berechenbar (Di/Fr nicht gefunden) → Skip")
            conn.close()
            return []

        momentum_pct = momentum * 100
        log(f"[WEEKEND_MOMO] {asset}: 3-Tage-Momentum={momentum_pct:+.2f}% (Di={tue_close:.4f}, Fr={fri_close:.4f})")

        if abs(momentum) < MOMENTUM_THRESHOLD:
            log(f"[WEEKEND_MOMO] {asset}: |Momentum| {abs(momentum_pct):.2f}% < {MOMENTUM_THRESHOLD*100:.0f}% → kein Signal")
            conn.close()
            return []

        direction = "long" if momentum > 0 else "short"

        # ── ATR(14) auf 4h für SL/TP ─────────────────────────────────────────
        candles_4h = _get_candles(conn, asset, "4h", 20)
        if len(candles_4h) < 15:
            log(f"[WEEKEND_MOMO] {asset}: Zu wenige 4h-Candles → Skip")
            conn.close()
            return []

        atr = atr_wilder(candles_4h, 14)
        if atr <= 0:
            log(f"[WEEKEND_MOMO] {asset}: ATR=0 → Skip")
            conn.close()
            return []

        entry   = candles_4h[-1]["close"]
        sl_dist = ATR_SL_MULTIPLIER * atr
        tp_dist = ATR_TP_MULTIPLIER * atr

        if direction == "long":
            sl = entry - sl_dist
            tp1 = entry + tp_dist * 0.5
            tp2 = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp1 = entry - tp_dist * 0.5
            tp2 = entry - tp_dist

        risk_usd = CAPITAL * MAX_RISK_PCT
        size     = risk_usd / sl_dist if sl_dist > 0 else 0.0

        # ── Duplikat-Schutz ───────────────────────────────────────────────────
        existing = conn.execute(
            """SELECT id FROM signals
               WHERE strategy='weekend_momo' AND asset=? AND date(created_at)=? AND mode=?
               AND status NOT IN ('rejected', 'expired')""",
            (asset, today, mode),
        ).fetchone()
        if existing:
            log(f"[WEEKEND_MOMO] {asset}: Signal für {today} bereits vorhanden → Skip")
            conn.close()
            return []

        signal = Signal(
            strategy="weekend_momo", asset=asset, direction=direction, mode=mode,
            entry_price=round(entry, 4), stop_loss=round(sl, 4),
            take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
            size=round(size, 4), risk_usd=round(risk_usd, 4),
            session="weekend", created_at=now_iso(), status="pending",
        )
        sig_id = _save_signal(conn, signal)
        signal.id = sig_id
        conn.close()

        log(f"[WEEKEND_MOMO] {TELEGRAM_V2_PREFIX} Signal: {asset} {direction.upper()} "
            f"momentum={momentum_pct:+.2f}% @ {entry:.4f} | SL={sl:.4f} | TP2={tp2:.4f} | mode={mode}")
        return [signal]
