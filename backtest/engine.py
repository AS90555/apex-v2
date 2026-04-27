"""
Backtest-Engine — deterministisches Backtesting auf historischen DB-Daten.

Nutzt dieselbe Feature-Registry und dieselben Strategie-Bedingungen wie
der Live-Code, aber:
  - Liest Candles/Features mit WHERE ts <= as_of_ts (point-in-time korrekt)
  - Kein Aufruf von Executor oder Bitget-Client
  - Simuliert Exits bar-by-bar (SL/TP-Hit oder Timeout)

Unterstützte Strategien: vaa, kdt, weekend_momo
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.db import get_connection
from core.utils import log
from features.indicators import atr_wilder, ema, vol_sma, body_sma


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class BtSignal:
    ts:          int
    strategy:    str
    asset:       str
    direction:   str   # 'long' | 'short'
    entry_price: float
    stop_loss:   float
    take_profit_1: float
    take_profit_2: float
    size:        float
    risk_usd:    float


@dataclass
class BtTrade:
    signal:      BtSignal
    entry_ts:    int
    exit_ts:     Optional[int]    = None
    exit_price:  Optional[float]  = None
    exit_reason: Optional[str]    = None   # 'sl', 'tp1', 'tp2', 'timeout'
    pnl_usd:     float            = 0.0
    pnl_r:       float            = 0.0

    @property
    def closed(self) -> bool:
        return self.exit_ts is not None


@dataclass
class BtResult:
    strategy:    str
    asset:       str
    trades:      list[BtTrade]   = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_r > 0)

    @property
    def winrate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades)

    @property
    def avg_r(self) -> float:
        return self.total_r / self.total if self.total > 0 else 0.0

    def summary(self) -> dict:
        return {
            "strategy": self.strategy, "asset": self.asset,
            "trades": self.total, "wins": self.wins,
            "winrate": round(self.winrate * 100, 1),
            "total_r": round(self.total_r, 2),
            "avg_r": round(self.avg_r, 3),
        }


# ── DB-Helpers (point-in-time) ────────────────────────────────────────────────

def _candles(conn, asset: str, interval: str, as_of_ts: int, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval=? AND ts <= ?
           ORDER BY ts DESC LIMIT ?""",
        (asset, interval, as_of_ts, limit),
    ).fetchall()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in reversed(rows)]


def _feature(conn, asset: str, interval: str, as_of_ts: int, name: str) -> Optional[float]:
    row = conn.execute(
        """SELECT value FROM features
           WHERE asset=? AND interval=? AND ts<=? AND feature_name=?
           ORDER BY ts DESC LIMIT 1""",
        (asset, interval, as_of_ts, name),
    ).fetchone()
    return row[0] if row else None


# ── Exit-Simulation ───────────────────────────────────────────────────────────

def _simulate_exit(conn, trade: BtTrade, asset: str, interval: str,
                   max_bars: int = 48) -> BtTrade:
    """
    Iteriert die nächsten Bars nach Entry und prüft SL/TP-Hits.
    Nutzt High/Low der Bars für konservative Simulation (kein Look-ahead).
    """
    sig = trade.signal
    sl_dist = abs(sig.entry_price - sig.stop_loss)
    if sl_dist <= 0:
        trade.exit_reason = "invalid_sl"
        return trade

    rows = conn.execute(
        """SELECT ts, high, low, close FROM candles
           WHERE asset=? AND interval=? AND ts > ?
           ORDER BY ts ASC LIMIT ?""",
        (asset, interval, trade.entry_ts, max_bars),
    ).fetchall()

    for bar_ts, high, low, close in rows:
        if sig.direction == "long":
            if low <= sig.stop_loss:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.stop_loss
                trade.exit_reason = "sl"
                break
            if high >= sig.take_profit_2:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.take_profit_2
                trade.exit_reason = "tp2"
                break
            if high >= sig.take_profit_1:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.take_profit_1
                trade.exit_reason = "tp1"
                break
        else:  # short
            if high >= sig.stop_loss:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.stop_loss
                trade.exit_reason = "sl"
                break
            if low <= sig.take_profit_2:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.take_profit_2
                trade.exit_reason = "tp2"
                break
            if low <= sig.take_profit_1:
                trade.exit_ts    = bar_ts
                trade.exit_price = sig.take_profit_1
                trade.exit_reason = "tp1"
                break
    else:
        # Timeout: letzten Close nehmen
        if rows:
            trade.exit_ts    = rows[-1][0]
            trade.exit_price = rows[-1][3]
            trade.exit_reason = "timeout"

    if trade.exit_price is not None:
        if sig.direction == "long":
            raw_pnl = (trade.exit_price - sig.entry_price) * sig.size
        else:
            raw_pnl = (sig.entry_price - trade.exit_price) * sig.size
        trade.pnl_usd = round(raw_pnl, 4)
        trade.pnl_r   = round(raw_pnl / (sl_dist * sig.size), 3) if sl_dist * sig.size > 0 else 0.0

    return trade


# ── Strategie-Signalgeneratoren (point-in-time) ───────────────────────────────

def _vaa_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    candles = _candles(conn, asset, "1h", as_of_ts, 1)
    if not candles:
        return None
    c = candles[-1]
    ts = c["time"]

    vol_sma50  = _feature(conn, asset, "1h", ts, "vol_sma_50_1h")
    body_sma50 = _feature(conn, asset, "1h", ts, "body_sma_50_1h")
    ema20      = _feature(conn, asset, "1h", ts, "ema_20_1h")
    atr14      = _feature(conn, asset, "1h", ts, "atr_14_1h")
    atr_sma20  = _feature(conn, asset, "1h", ts, "atr_sma_20_1h")

    if not all([vol_sma50, body_sma50, ema20]):
        return None

    vol_ratio  = c["volume"] / vol_sma50  if vol_sma50  > 0 else 0
    body       = abs(c["open"] - c["close"])
    body_ratio = body / body_sma50 if body_sma50 > 0 else 0
    atr_ratio  = atr14 / atr_sma20 if (atr14 and atr_sma20 and atr_sma20 > 0) else 0

    if not (vol_ratio  > cfg.get("VOL_MULT",  2.5) and
            body_ratio < cfg.get("BODY_MULT", 0.6) and
            c["close"] > ema20 and
            atr_ratio  > cfg.get("ATR_EXPAND", 1.2)):
        return None

    entry   = c["close"]
    sl      = c["high"]
    sl_dist = abs(sl - entry)
    if sl_dist <= 0:
        return None

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    tp_r     = cfg.get("TP_R", 3.0)
    return BtSignal(
        ts=ts, strategy="vaa", asset=asset, direction="short",
        entry_price=entry, stop_loss=sl,
        take_profit_1=round(entry - sl_dist * 1.0, 6),
        take_profit_2=round(entry - sl_dist * tp_r, 6),
        size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
    )


def _kdt_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    candles = _candles(conn, asset, "1h", as_of_ts, 5)
    if len(candles) < 4:
        return None

    ts_last = candles[-1]["time"]
    ema50  = _feature(conn, asset, "1h", ts_last, "ema_50_1h")
    atr14  = _feature(conn, asset, "1h", ts_last, "atr_14_1h")
    if not ema50 or not atr14 or ema50 <= 0 or atr14 <= 0:
        return None

    c0, c1, c2 = candles[-1], candles[-2], candles[-3]
    body0 = abs(c0["close"] - c0["open"])
    body1 = abs(c1["close"] - c1["open"])
    body2 = abs(c2["close"] - c2["open"])

    cond_trend   = c0["close"] > ema50
    cond_green   = (c0["close"] > c0["open"] and
                    c1["close"] > c1["open"] and
                    c2["close"] > c2["open"])
    cond_bodies  = body0 < body1 < body2 and body0 > 0
    cond_vols    = c0["volume"] < c1["volume"] < c2["volume"]

    sl_price = c0["high"]
    entry    = c0["low"]
    sl_dist  = sl_price - entry
    sl_mult  = cfg.get("SL_ATR_MULT", 0.5)
    cond_sl  = (sl_dist > 0 and sl_dist < sl_mult * atr14
                and 0.0005 < sl_dist / entry < 0.15)

    if not all([cond_trend, cond_green, cond_bodies, cond_vols, cond_sl]):
        return None

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    tp_r     = cfg.get("TP_R", 3.0)
    return BtSignal(
        ts=ts_last, strategy="kdt", asset=asset, direction="short",
        entry_price=round(entry, 4), stop_loss=round(sl_price, 4),
        take_profit_1=round(entry - sl_dist * 1.0, 4),
        take_profit_2=round(entry - sl_dist * tp_r, 4),
        size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
    )


def _weekend_momo_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    dt = datetime.fromtimestamp(as_of_ts / 1000, tz=timezone.utc)
    if dt.weekday() != 5:   # nur Samstag
        return None

    candles_1d = _candles(conn, asset, "1d", as_of_ts, 10)
    if len(candles_1d) < 5:
        return None

    tue_close = fri_close = None
    for c in candles_1d:
        day = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc).weekday()
        if day == 1: tue_close = c["close"]
        elif day == 4: fri_close = c["close"]

    if not tue_close or not fri_close:
        return None

    momentum = (fri_close / tue_close) - 1
    if abs(momentum) < cfg.get("MOMENTUM_THRESHOLD", 0.03):
        return None

    candles_4h = _candles(conn, asset, "4h", as_of_ts, 20)
    if len(candles_4h) < 15:
        return None

    atr = atr_wilder(candles_4h, 14)
    if atr <= 0:
        return None

    entry     = candles_4h[-1]["close"]
    direction = "long" if momentum > 0 else "short"
    sl_dist   = cfg.get("ATR_SL_MULT", 1.5) * atr
    tp_dist   = cfg.get("ATR_TP_MULT", 3.0) * atr

    if direction == "long":
        sl  = entry - sl_dist
        tp1 = entry + tp_dist * 0.5
        tp2 = entry + tp_dist
    else:
        sl  = entry + sl_dist
        tp1 = entry - tp_dist * 0.5
        tp2 = entry - tp_dist

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    return BtSignal(
        ts=as_of_ts, strategy="weekend_momo", asset=asset, direction=direction,
        entry_price=round(entry, 4), stop_loss=round(sl, 4),
        take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
        size=round(risk_usd / sl_dist, 4) if sl_dist > 0 else 0.0,
        risk_usd=round(risk_usd, 4),
    )


# ── Haupt-API ─────────────────────────────────────────────────────────────────

SIGNAL_FNS = {
    "vaa":          _vaa_signal,
    "kdt":          _kdt_signal,
    "weekend_momo": _weekend_momo_signal,
}

STRATEGY_INTERVAL = {
    "vaa":          "1h",
    "kdt":          "1h",
    "weekend_momo": "1d",
}

EXIT_INTERVAL = {
    "vaa":          "1h",
    "kdt":          "1h",
    "weekend_momo": "4h",
}


def run_backtest(
    strategy: str,
    asset: str,
    start_ts: int,
    end_ts: int,
    cfg: dict = None,
    max_exit_bars: int = 48,
    verbose: bool = False,
) -> BtResult:
    """
    Führt einen Backtest für eine Strategie auf einem Asset durch.

    Args:
        strategy:      'vaa', 'kdt', 'weekend_momo'
        asset:         z.B. 'ETH', 'SOL', 'AVAX'
        start_ts:      Start-Timestamp ms (Unix)
        end_ts:        End-Timestamp ms (Unix)
        cfg:           Strategy-Parameter (Standard aus config/settings.py)
        max_exit_bars: Maximale Bars bis Timeout-Exit
        verbose:       Detaillierte Bar-Logs

    Returns:
        BtResult mit allen simulierten Trades
    """
    if strategy not in SIGNAL_FNS:
        raise ValueError(f"Unbekannte Strategie: {strategy}. Verfügbar: {list(SIGNAL_FNS)}")

    if cfg is None:
        cfg = _default_cfg(strategy)

    signal_fn   = SIGNAL_FNS[strategy]
    interval    = STRATEGY_INTERVAL[strategy]
    exit_intv   = EXIT_INTERVAL[strategy]
    result      = BtResult(strategy=strategy, asset=asset)
    conn        = get_connection()

    # Alle Timestamps im Bereich laden
    timestamps = [
        r[0] for r in conn.execute(
            """SELECT DISTINCT ts FROM candles
               WHERE asset=? AND interval=? AND ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (asset, interval, start_ts, end_ts),
        ).fetchall()
    ]

    log(f"[BACKTEST] {strategy}/{asset}: {len(timestamps)} Bars von "
        f"{datetime.fromtimestamp(start_ts/1000, tz=timezone.utc).date()} bis "
        f"{datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).date()}")

    open_trade: Optional[BtTrade] = None

    for ts in timestamps:
        # Offenen Trade schließen falls Exit-Bedingung erreicht
        if open_trade and not open_trade.closed and ts > open_trade.signal.ts:
            open_trade = _simulate_exit(conn, open_trade, asset, exit_intv, max_exit_bars)
            if open_trade.closed:
                result.trades.append(open_trade)
                if verbose:
                    log(f"[BACKTEST]   EXIT {open_trade.exit_reason} "
                        f"pnl={open_trade.pnl_r:+.2f}R @ {open_trade.exit_price}")
                open_trade = None

        if open_trade:
            continue  # kein neuer Trade solange Position offen

        sig = signal_fn(conn, asset, ts, cfg)
        if sig:
            open_trade = BtTrade(signal=sig, entry_ts=ts)
            if verbose:
                log(f"[BACKTEST]   SIGNAL {sig.direction.upper()} @ {sig.entry_price} "
                    f"SL={sig.stop_loss} TP2={sig.take_profit_2}")

    # Letzten offenen Trade schließen (Timeout)
    if open_trade and not open_trade.closed:
        open_trade = _simulate_exit(conn, open_trade, asset, exit_intv, max_exit_bars)
        if open_trade.closed:
            result.trades.append(open_trade)

    conn.close()
    s = result.summary()
    log(f"[BACKTEST] {strategy}/{asset}: trades={s['trades']} wr={s['winrate']}% "
        f"total_r={s['total_r']:+.2f}R avg_r={s['avg_r']:+.3f}R")
    return result


def _default_cfg(strategy: str) -> dict:
    from config.settings import (
        CAPITAL, MAX_RISK_PCT,
        VAA_VOL_MULT, VAA_BODY_MULT, VAA_ATR_EXPAND, VAA_TP_R,
        KDT_SL_ATR_MULT, KDT_TP_R, KDT_MAX_RISK_PCT,
        MOMENTUM_THRESHOLD, ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER, MAX_RISK_PCT,
    )
    base = {"CAPITAL": CAPITAL}
    if strategy == "vaa":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "VOL_MULT": VAA_VOL_MULT, "BODY_MULT": VAA_BODY_MULT,
                "ATR_EXPAND": VAA_ATR_EXPAND, "TP_R": VAA_TP_R}
    if strategy == "kdt":
        return {**base, "MAX_RISK_PCT": KDT_MAX_RISK_PCT,
                "SL_ATR_MULT": KDT_SL_ATR_MULT, "TP_R": KDT_TP_R}
    if strategy == "weekend_momo":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "MOMENTUM_THRESHOLD": MOMENTUM_THRESHOLD,
                "ATR_SL_MULT": ATR_SL_MULTIPLIER, "ATR_TP_MULT": ATR_TP_MULTIPLIER}
    return base
