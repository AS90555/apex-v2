#!/usr/bin/env python3
"""
Squeeze Breakout Signal Generator für Backtest-Engine.
Volatility-Expansion Scout statt Mean-Reversion.

Entry-Logik:
  1. Erkenne Squeeze-Release: is_squeeze[t-1]=1.0 und is_squeeze[t]=0.0
  2. Direction: wenn close > ema(20) → LONG, sonst SHORT
  3. Size & Risk: standard ATR-basiert
"""

from typing import Optional
from features.indicators import is_squeeze, ema, atr_wilder


class BtSignal:
    """Placeholder für BtSignal aus engine.py."""
    def __init__(self, ts, strategy, asset, direction, entry_price, stop_loss,
                 take_profit_1, take_profit_2, size, risk_usd):
        self.ts = ts
        self.strategy = strategy
        self.asset = asset
        self.direction = direction
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit_1 = take_profit_1
        self.take_profit_2 = take_profit_2
        self.size = size
        self.risk_usd = risk_usd


def squeeze_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Squeeze Breakout Entry auf 15m-Basis mit EMA(20)-Direktions-Filter.

    cfg-Parameter:
      - SQUEEZE_PERIOD (default 20): Periode für Squeeze-Detektion (BB/KC)
      - EMA_PERIOD (default 20): Periode für Richtungs-Filter
      - TP_R (default 3.0): Take-Profit in R (multiples von SL-distance)
      - SL_ATR_MULT (default 1.0): Stop-Loss = entry ± (ATR × mult)
      - CAPITAL, MAX_RISK_PCT: Standard
    """
    # Lade 22 letzte 15m-Candles (20 für Squeeze + 2 für State-Change)
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval='15m' AND ts <= ?
           ORDER BY ts DESC LIMIT 22""",
        (asset, as_of_ts),
    ).fetchall()
    if len(rows) < 22:
        return None

    candles = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5]} for r in reversed(rows)]

    squeeze_period = cfg.get("SQUEEZE_PERIOD", 20)

    # Prüfe Squeeze-Release: [t-2:] = 20-Candles, [t-1:] = 19-Candles
    squeeze_last_2 = is_squeeze(candles[-22:-2], squeeze_period)  # [0:20]
    squeeze_last_1 = is_squeeze(candles[-21:],  squeeze_period)  # [1:21]

    # Nur eingehen wenn Squeeze EBEN eben gelöst (Release)
    if squeeze_last_2 or not squeeze_last_1:
        return None

    # Richtungs-Filter: EMA(20) auf aktuellem Close
    ema_period = cfg.get("EMA_PERIOD", 20)
    ema_20 = ema(candles, ema_period)
    current_close = candles[-1]["close"]
    direction = "long" if current_close > ema_20 else "short"

    # ATR für SL/TP
    atr = atr_wilder(candles, 14)
    if atr <= 0:
        return None

    # Entry = Current Close
    entry = current_close
    sl_mult = cfg.get("SL_ATR_MULT", 1.0)
    sl_dist = atr * sl_mult

    if direction == "long":
        sl = entry - sl_dist
        tp = entry + sl_dist * cfg.get("TP_R", 3.0)
    else:
        sl = entry + sl_dist
        tp = entry - sl_dist * cfg.get("TP_R", 3.0)

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    return BtSignal(
        ts=as_of_ts, strategy="squeeze", asset=asset, direction=direction,
        entry_price=round(entry, 4), stop_loss=round(sl, 4),
        take_profit_1=round(tp, 4), take_profit_2=round(tp, 4),
        size=round(risk_usd / sl_dist, 4) if sl_dist > 0 else 0.0,
        risk_usd=round(risk_usd, 4),
    )
