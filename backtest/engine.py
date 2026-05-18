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
from features.indicators import atr_wilder, ema, sma, bollinger_bands, vol_sma, body_sma, rsi as calc_rsi, is_squeeze
from strategies.orb_signal_fn import orb_engine_adapter


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
    exit_reason: Optional[str]    = None   # 'sl', 'tp1', 'tp1_be_sl', 'tp2', 'timeout'
    pnl_usd:     float            = 0.0
    pnl_r:       float            = 0.0
    # Partial-TP-Felder (v6)
    tp1_hit:             bool  = False
    remaining_size:      float = 0.0
    realized_pnl_tp1:   float = 0.0
    be_sl_active:        bool  = False
    intrabar_model_used: str   = "static"

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

def _get_1m_bars(conn, asset: str, bar_ts: int, bar_ms: int) -> list[tuple]:
    """Lädt 1m-Kerzen innerhalb eines Bars [bar_ts, bar_ts+bar_ms)."""
    return conn.execute(
        """SELECT ts, high, low, open, close FROM candles
           WHERE asset=? AND interval='1m' AND ts >= ? AND ts < ?
           ORDER BY ts ASC""",
        (asset, bar_ts, bar_ts + bar_ms),
    ).fetchall()


def _exit_within_bar(
    direction: str, open_: float, high: float, low: float,
    sl: float, tp1: float, tp2: float,
    sub_bars: list[tuple],       # (ts, high, low, open, close) — ggf. leer
    history_candles: list[dict], # für GBM-Kalibrierung
    be_sl: Optional[float],      # wenn not None: BE-Stop statt sl
) -> tuple[Optional[str], float, str]:
    """
    Bestimmt Exit innerhalb eines Bars via 1m-Zoom oder GBM.
    Gibt (exit_reason, exit_price, model_used) zurück.
    exit_reason None = kein Hit in diesem Bar.
    """
    effective_sl = be_sl if be_sl is not None else sl

    if sub_bars:
        # 1m-Zoom: deterministisch
        for _, sh, sl_val, so, sc in sub_bars:
            if direction == "long":
                if sl_val <= effective_sl:
                    return "sl" if be_sl is None else "tp1_be_sl", effective_sl, "1m_zoom"
                if tp2 and tp2 > 0 and sh >= tp2:
                    return "tp2", tp2, "1m_zoom"
                if sh >= tp1:
                    return "tp1", tp1, "1m_zoom"
            else:
                if sh >= effective_sl:
                    return "sl" if be_sl is None else "tp1_be_sl", effective_sl, "1m_zoom"
                if tp2 and tp2 > 0 and sl_val <= tp2:
                    return "tp2", tp2, "1m_zoom"
                if sl_val <= tp1:
                    return "tp1", tp1, "1m_zoom"
        return None, 0.0, "1m_zoom"

    # GBM-Fallback
    from backtest.intrabar_gbm import simulate_intrabar
    reason, price = simulate_intrabar(
        entry_price=open_,
        stop_loss=effective_sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        direction=direction,
        bar_open=open_, bar_high=high, bar_low=low,
        candles_history=history_candles,
    )
    if reason == "sl" and be_sl is not None:
        reason = "tp1_be_sl"
    return reason, price, "gbm"


def _simulate_exit(conn, trade: BtTrade, asset: str, interval: str,
                   max_bars: int = 48, history_candles: list[dict] = None) -> BtTrade:
    """
    Iteriert die nächsten Bars nach Entry, prüft SL/TP-Hits mit Partial-TP-Logik.

    Partial-TP (spiegelt Live-Verhalten in position_monitor.py):
      - Bei TP1-Hit: 50 % der Position schließen, pnl_r anteilig buchen.
      - Rest-Position läuft mit BE-Stop (= entry_price) weiter bis TP2 oder BE-SL.
      - Gesamt-pnl_r = 0.5 * R_TP1 + 0.5 * R_Remainder.

    Intrabar-Reihenfolge via:
      1. 1m-Zoom falls 1m-Daten vorhanden (deterministisch).
      2. GBM-Simulation (N_PATHS=500) als Fallback.
      Falls nur static möglich (High/Low-Check), nutzen wir das weiterhin
      als schnellstes Verfahren wenn weder 1m noch GBM verfügbar sind.
    """
    from config.settings import INTRABAR_MODEL
    sig = trade.signal
    sl_dist = abs(sig.entry_price - sig.stop_loss)
    if sl_dist <= 0:
        trade.exit_reason = "invalid_sl"
        return trade

    interval_ms = INTERVAL_MS.get(interval, 3_600_000)
    tp2_valid = sig.take_profit_2 and sig.take_profit_2 > 0 and sig.take_profit_2 != sig.take_profit_1

    rows = conn.execute(
        """SELECT ts, high, low, open, close FROM candles
           WHERE asset=? AND interval=? AND ts > ?
           ORDER BY ts ASC LIMIT ?""",
        (asset, interval, trade.entry_ts, max_bars),
    ).fetchall()

    if not rows:
        return trade

    # Geschichte für GBM-Kalibrierung
    hist = history_candles or []
    be_sl: Optional[float] = None   # None = original SL, float = BE-Stop aktiv

    for bar_ts, high, low, open_, close in rows:
        current_sl = be_sl if be_sl is not None else sig.stop_loss
        tp2_level  = sig.take_profit_2 if tp2_valid else 0.0

        # Schnell-Check: Trifft der Bar überhaupt ein Level?
        if sig.direction == "long":
            bar_hits_sl  = low  <= current_sl
            bar_hits_tp1 = high >= sig.take_profit_1 and not trade.tp1_hit
            bar_hits_tp2 = tp2_valid and high >= tp2_level and trade.tp1_hit
        else:
            bar_hits_sl  = high >= current_sl
            bar_hits_tp1 = low  <= sig.take_profit_1 and not trade.tp1_hit
            bar_hits_tp2 = tp2_valid and low <= tp2_level and trade.tp1_hit

        if not (bar_hits_sl or bar_hits_tp1 or bar_hits_tp2):
            continue  # kein Hit in diesem Bar

        # Intrabar-Auflösung
        if INTRABAR_MODEL == "static":
            sub_bars = []
            use_model = "static"
        else:
            sub_bars = _get_1m_bars(conn, asset, bar_ts, interval_ms)
            use_model = "1m_zoom" if sub_bars else "gbm"

        # ── Phase A: TP1 noch nicht erreicht ──────────────────────────────────
        if not trade.tp1_hit:
            if INTRABAR_MODEL == "static":
                # Statisch: SL-Priorität vor TP (konservativ)
                if bar_hits_sl and bar_hits_tp1:
                    # Beide im selben Bar → SL gewinnt (konservativ)
                    reason, price, model = "sl", current_sl, "static"
                elif bar_hits_sl:
                    reason, price, model = "sl", current_sl, "static"
                elif bar_hits_tp1:
                    reason, price, model = "tp1", sig.take_profit_1, "static"
                else:
                    continue
            else:
                reason, price, model = _exit_within_bar(
                    sig.direction, open_, high, low,
                    sig.stop_loss, sig.take_profit_1, tp2_level,
                    sub_bars, hist, be_sl,
                )
                if reason is None:
                    continue

            trade.intrabar_model_used = model

            if reason == "sl":
                trade.exit_ts    = bar_ts
                trade.exit_price = price
                trade.exit_reason = "sl"
                break

            if reason == "tp1":
                # Partial-TP: 50 % Exit
                trade.tp1_hit = True
                trade.realized_pnl_tp1 = _calc_r(sig, price, 0.5)
                trade.remaining_size   = sig.size * 0.5
                be_sl = sig.entry_price   # BE-Stop aktivieren
                # Falls TP2 auch in diesem Bar:
                if sig.direction == "long":
                    if tp2_valid and high >= tp2_level:
                        trade.exit_ts    = bar_ts
                        trade.exit_price = tp2_level
                        trade.exit_reason = "tp2"
                        break
                else:
                    if tp2_valid and low <= tp2_level:
                        trade.exit_ts    = bar_ts
                        trade.exit_price = tp2_level
                        trade.exit_reason = "tp2"
                        break
                # Kein TP2 in diesem Bar → weiter zum nächsten Bar mit BE-Stop
                continue

        # ── Phase B: TP1 bereits erreicht, läuft mit BE-Stop ──────────────────
        else:
            if INTRABAR_MODEL == "static":
                if sig.direction == "long":
                    if bar_hits_sl:
                        reason, price, model = "tp1_be_sl", current_sl, "static"
                    elif bar_hits_tp2:
                        reason, price, model = "tp2", tp2_level, "static"
                    else:
                        continue
                else:
                    if bar_hits_sl:
                        reason, price, model = "tp1_be_sl", current_sl, "static"
                    elif bar_hits_tp2:
                        reason, price, model = "tp2", tp2_level, "static"
                    else:
                        continue
            else:
                reason, price, model = _exit_within_bar(
                    sig.direction, open_, high, low,
                    sig.stop_loss, sig.take_profit_1, tp2_level,
                    sub_bars, hist, be_sl,
                )
                if reason is None:
                    continue

            trade.intrabar_model_used = model
            trade.exit_ts    = bar_ts
            trade.exit_price = price
            trade.exit_reason = reason
            break

    else:
        # Timeout
        if rows:
            trade.exit_ts    = rows[-1][0]
            trade.exit_price = rows[-1][4]   # close
            trade.exit_reason = "timeout"

    # ── PnL-Berechnung ────────────────────────────────────────────────────────
    if trade.exit_price is not None:
        denom = sl_dist * sig.size
        if trade.tp1_hit:
            # Partial: TP1 (50 %) + Rest-Exit (50 %)
            r_remainder = _calc_r(sig, trade.exit_price, 0.5)
            total_pnl_r = trade.realized_pnl_tp1 + r_remainder
            trade.pnl_r   = round(total_pnl_r, 3)
            trade.pnl_usd = round(total_pnl_r * denom, 4)
        else:
            if sig.direction == "long":
                raw = (trade.exit_price - sig.entry_price) * sig.size
            else:
                raw = (sig.entry_price - trade.exit_price) * sig.size
            trade.pnl_usd = round(raw, 4)
            trade.pnl_r   = round(raw / denom, 3) if denom > 0 else 0.0

    return trade


def _calc_r(sig: "BtSignal", exit_price: float, fraction: float) -> float:
    """Berechnet pnl_r für einen Partial-Exit (fraction der Position)."""
    sl_dist = abs(sig.entry_price - sig.stop_loss)
    denom   = sl_dist * sig.size
    if denom <= 0:
        return 0.0
    if sig.direction == "long":
        raw = (exit_price - sig.entry_price) * sig.size * fraction
    else:
        raw = (sig.entry_price - exit_price) * sig.size * fraction
    return raw / (denom * fraction) * fraction   # = raw / denom


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

    direction = cfg.get("DIRECTION", "short")
    risk_usd  = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    tp_r      = cfg.get("TP_R", 3.0)
    sign      = -1 if direction == "short" else 1
    return BtSignal(
        ts=ts, strategy="vaa", asset=asset, direction=direction,
        entry_price=entry, stop_loss=sl,
        take_profit_1=round(entry + sign * sl_dist * 1.0, 6),
        take_profit_2=round(entry + sign * sl_dist * tp_r, 6),
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

    direction = cfg.get("DIRECTION", "short")
    risk_usd  = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    tp_r      = cfg.get("TP_R", 3.0)
    sign      = -1 if direction == "short" else 1
    return BtSignal(
        ts=ts_last, strategy="kdt", asset=asset, direction=direction,
        entry_price=round(entry, 4), stop_loss=round(sl_price, 4),
        take_profit_1=round(entry + sign * sl_dist * 1.0, 4),
        take_profit_2=round(entry + sign * sl_dist * tp_r, 4),
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

def _squeeze_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    # Squeeze-Release Detection auf 1h Basis (beste verfügbare Daten)
    candles = _candles(conn, asset, "1h", as_of_ts, 22)
    if len(candles) < 22:
        return None

    squeeze_period = cfg.get("SQUEEZE_PERIOD", 20)
    squeeze_last_2 = is_squeeze(candles[-22:-2], squeeze_period)
    squeeze_last_1 = is_squeeze(candles[-21:],  squeeze_period)

    # Release = war in Squeeze, ist jetzt raus
    if squeeze_last_2 or not squeeze_last_1:
        return None

    ema_period = cfg.get("EMA_PERIOD", 20)
    closes = [c["close"] for c in candles]
    ema_20 = ema(closes, ema_period)
    current_close = candles[-1]["close"]
    direction = "long" if current_close > ema_20 else "short"

    atr = atr_wilder(candles, 14)
    if atr <= 0:
        return None

    entry   = current_close
    sl_mult = cfg.get("SL_ATR_MULT", 1.0)
    sl_dist = atr * sl_mult

    if direction == "long":
        sl = entry - sl_dist
        tp = entry + sl_dist * cfg.get("TP_R", 3.0)
    else:
        sl = entry + sl_dist
        tp = entry - sl_dist * cfg.get("TP_R", 3.0)

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    # TP1 = 50 % des TP-Abstands, TP2 = 100 % (TP1 ≠ TP2 für Partial-Exit)
    sign = 1 if direction == "long" else -1
    tp1  = entry + sign * sl_dist * cfg.get("TP_R", 3.0) * 0.5
    tp2  = entry + sign * sl_dist * cfg.get("TP_R", 3.0)
    return BtSignal(
        ts=as_of_ts, strategy="squeeze", asset=asset, direction=direction,
        entry_price=round(entry, 4), stop_loss=round(sl, 4),
        take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
        size=round(risk_usd / sl_dist, 4) if sl_dist > 0 else 0.0,
        risk_usd=round(risk_usd, 4),
    )


def _asian_fade_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    dt = datetime.fromtimestamp(as_of_ts / 1000, tz=timezone.utc)
    if dt.hour != 8:
        return None

    candles = _candles(conn, asset, "1h", as_of_ts, 30)
    if len(candles) < 16:
        return None

    midnight_ts = as_of_ts - 8 * 3_600_000
    row = conn.execute(
        "SELECT close FROM candles WHERE asset=? AND interval='1h' AND ts=?",
        (asset, midnight_ts),
    ).fetchone()
    if not row:
        return None

    midnight_close = row[0]
    current_close  = candles[-1]["close"]
    pump_pct       = (current_close - midnight_close) / midnight_close

    direction  = cfg.get("DIRECTION", "short")
    dump_mode  = cfg.get("DUMP_MODE", False)
    threshold  = cfg.get("PUMP_THRESHOLD", 0.015)
    rsi_ob     = cfg.get("RSI_OB", 70)
    rsi_os     = cfg.get("RSI_OS", 30)

    if dump_mode:
        if pump_pct > -threshold:
            return None
        rsi_val = calc_rsi(candles, period=14)
        if rsi_val > rsi_os:
            return None
    else:
        if pump_pct < threshold:
            return None
        rsi_val = calc_rsi(candles, period=14)
        if direction == "short" and rsi_val < rsi_ob:
            return None
        if direction == "long" and rsi_val < rsi_ob:
            return None

    atr = atr_wilder(candles, period=14)
    if atr <= 0:
        return None

    entry   = current_close
    sl_dist = atr * cfg.get("SL_ATR_MULT", 1.0)

    if direction == "short":
        sl = entry + sl_dist
        tp = entry - sl_dist * cfg.get("TP_MULT", 1.5)
    else:
        sl = entry - sl_dist
        tp = entry + sl_dist * cfg.get("TP_MULT", 1.5)

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    # TP1 = halber TP-Abstand (Partial-Exit), TP2 = voller TP-Abstand
    tp_mult = cfg.get("TP_MULT", 1.5)
    sign    = -1 if direction == "short" else 1
    tp1     = entry + sign * sl_dist * tp_mult * 0.5
    tp2     = entry + sign * sl_dist * tp_mult
    return BtSignal(
        ts=as_of_ts, strategy="asian_fade", asset=asset, direction=direction,
        entry_price=round(entry, 4), stop_loss=round(sl, 4),
        take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
        size=round(risk_usd / sl_dist, 4) if sl_dist > 0 else 0.0,
        risk_usd=round(risk_usd, 4),
    )


def _mean_reversion_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Mean Reversion auf 1h:
      Long:  Close < unteres BB UND RSI < RSI_OS (überverkauft)
      Short: Close > oberes BB  UND RSI > RSI_OB (überkauft)
    TP = mittleres BB (SMA), SL = ATR-Abstand
    """
    bb_period  = int(cfg.get("BB_PERIOD",  20))
    bb_mult    = cfg.get("BB_MULT",   2.0)
    rsi_period = int(cfg.get("RSI_PERIOD", 14))
    rsi_os     = cfg.get("RSI_OS",    35.0)
    rsi_ob     = 100.0 - rsi_os
    sl_mult    = cfg.get("SL_ATR_MULT", 1.0)
    tp_r       = cfg.get("TP_R",       2.0)

    limit = max(bb_period, rsi_period) + 20
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < bb_period + 2:
        return None

    closes = [c["close"] for c in candles]
    upper, mid, lower = bollinger_bands(closes, bb_period, bb_mult)
    rsi_val = calc_rsi(candles, rsi_period)
    atr     = atr_wilder(candles, 14)

    if upper <= lower or atr <= 0:
        return None

    c   = candles[-1]
    ts  = c["time"]
    cls = c["close"]

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cls < lower and rsi_val < rsi_os:
        # Long: Preis unter unterem BB, überverkauft
        sl_dist = atr * sl_mult
        sl      = cls - sl_dist
        tp1     = mid                         # zurück zur Mitte
        tp2     = cls + sl_dist * tp_r
        if sl_dist <= 0 or sl <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="mean_reversion", asset=asset, direction="long",
            entry_price=round(cls, 6), stop_loss=round(sl, 6),
            take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cls > upper and rsi_val > rsi_ob:
        # Short: Preis über oberem BB, überkauft
        sl_dist = atr * sl_mult
        sl      = cls + sl_dist
        tp1     = mid
        tp2     = cls - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="mean_reversion", asset=asset, direction="short",
            entry_price=round(cls, 6), stop_loss=round(sl, 6),
            take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _vwap_bounce_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    VWAP Bounce auf 1h:
      VWAP = rollender typischer Preis × Volumen über VWAP_PERIOD Bars.
      Long:  Preis zieht auf VWAP zurück (innerhalb VWAP_BAND × ATR)
             UND Trend aufwärts (Close > EMA) UND RSI > RSI_MIN
      Short: Preis steigt auf VWAP zurück (innerhalb VWAP_BAND × ATR)
             UND Trend abwärts (Close < EMA) UND RSI < (100 - RSI_MIN)
    """
    vwap_period = int(cfg.get("VWAP_PERIOD",  24))
    vwap_band   = cfg.get("VWAP_BAND",    0.25)
    ema_period  = int(cfg.get("EMA_PERIOD",   50))
    rsi_min     = cfg.get("RSI_MIN",      50.0)
    sl_mult     = cfg.get("SL_ATR_MULT",  1.0)
    tp_r        = cfg.get("TP_R",         2.5)

    limit = max(vwap_period, ema_period) + 20
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < vwap_period + 2:
        return None

    # Rollender VWAP über die letzten vwap_period Bars
    window = candles[-vwap_period:]
    cum_tp_vol = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in window)
    cum_vol    = sum(c["volume"] for c in window)
    if cum_vol <= 0:
        return None
    vwap = cum_tp_vol / cum_vol

    closes  = [c["close"] for c in candles]
    ema_val = ema(closes, ema_period)
    rsi_val = calc_rsi(candles, 14)
    atr     = atr_wilder(candles, 14)

    if ema_val <= 0 or atr <= 0:
        return None

    c   = candles[-1]
    ts  = c["time"]
    cls = c["close"]

    dist_to_vwap = abs(cls - vwap)
    band_width   = atr * vwap_band
    near_vwap    = dist_to_vwap <= band_width

    if not near_vwap:
        return None

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cls > ema_val and rsi_val > rsi_min:
        # Long-Bounce: Aufwärtstrend, Preis nahe VWAP
        sl_dist = atr * sl_mult
        sl      = cls - sl_dist
        tp1     = cls + sl_dist
        tp2     = cls + sl_dist * tp_r
        if sl_dist <= 0 or sl <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="vwap_bounce", asset=asset, direction="long",
            entry_price=round(cls, 6), stop_loss=round(sl, 6),
            take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cls < ema_val and rsi_val < (100.0 - rsi_min):
        # Short-Bounce: Abwärtstrend, Preis nahe VWAP
        sl_dist = atr * sl_mult
        sl      = cls + sl_dist
        tp1     = cls - sl_dist
        tp2     = cls - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="vwap_bounce", asset=asset, direction="short",
            entry_price=round(cls, 6), stop_loss=round(sl, 6),
            take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _ema_pullback_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    EMA Pullback auf 1h:
      Long:  Close > EMA_SLOW (Uptrend), vorherige Kerze berührt/unterschreitet EMA_FAST,
             aktuelle Kerze schließt bullish über EMA_FAST → Pullback-Ende
      Short: Spiegelbildlich im Downtrend
    Bestätigung: Körper der aktuellen Kerze > BODY_FACTOR × ATR (kein Doji)
    """
    slow_period  = int(cfg.get("EMA_SLOW",    200))
    fast_period  = int(cfg.get("EMA_FAST",     50))
    body_factor  = cfg.get("BODY_FACTOR",   0.3)
    sl_mult      = cfg.get("SL_ATR_MULT",   1.0)
    tp_r         = cfg.get("TP_R",          2.5)

    limit = slow_period + 10
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < slow_period + 2:
        return None

    closes   = [c["close"] for c in candles]
    ema_slow = ema(closes, slow_period)
    ema_fast = ema(closes, fast_period)
    atr      = atr_wilder(candles, 14)
    if ema_slow <= 0 or ema_fast <= 0 or atr <= 0:
        return None

    cur  = candles[-1]
    prev = candles[-2]
    ts   = cur["time"]

    body_cur  = abs(cur["close"]  - cur["open"])
    body_prev = abs(prev["close"] - prev["open"])
    min_body  = atr * body_factor

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    # Long: Uptrend, Pullback auf EMA_FAST, bullishe Bestätigungskerze
    if (cur["close"] > ema_slow and
            prev["low"] <= ema_fast and            # Vorkerze touchte EMA_FAST
            cur["close"] > ema_fast and            # Erholung darüber
            cur["close"] > cur["open"] and         # bullish
            body_cur >= min_body):
        sl_dist = atr * sl_mult
        sl      = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="ema_pullback", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    # Short: Downtrend, Rally auf EMA_FAST, bearishe Bestätigungskerze
    if (cur["close"] < ema_slow and
            prev["high"] >= ema_fast and
            cur["close"] < ema_fast and
            cur["close"] < cur["open"] and
            body_cur >= min_body):
        sl_dist = atr * sl_mult
        sl      = cur["close"] + sl_dist
        tp2     = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="ema_pullback", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _donchian_breakout_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Donchian Channel Breakout auf 1h:
      Long:  Aktuelle Kerze schließt über dem N-Bar-Hoch der VORHERIGEN Kerzen
             UND Volumen > VOL_FACTOR × Vol-SMA (Bestätigung kein False Break)
             UND ATR-Expansion: ATR > ATR_MIN_MULT × ATR-SMA (Momentum vorhanden)
      Short: Spiegelbildlich
    TP bewusst eng (1.5–3R) für WR-freundliche Exits.
    """
    dc_period   = int(cfg.get("DC_PERIOD",    20))
    vol_factor  = cfg.get("VOL_FACTOR",    1.5)
    atr_min     = cfg.get("ATR_MIN_MULT",  1.0)
    sl_mult     = cfg.get("SL_ATR_MULT",   1.0)
    tp_r        = cfg.get("TP_R",          2.0)

    limit = dc_period + 30
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < dc_period + 5:
        return None

    cur    = candles[-1]
    ts     = cur["time"]
    # Donchian über die N Kerzen VOR der aktuellen (kein Look-ahead)
    window = candles[-(dc_period + 1):-1]
    dc_high = max(c["high"]  for c in window)
    dc_low  = min(c["low"]   for c in window)

    atr       = atr_wilder(candles, 14)
    atr_avg   = atr_wilder(candles[:-14], 14) if len(candles) > 28 else atr
    vol_avg   = vol_sma(candles, 20)

    if atr <= 0 or vol_avg <= 0:
        return None

    atr_expanding = atr >= atr_min * atr_avg if atr_avg > 0 else True
    vol_ok        = cur["volume"] >= vol_factor * vol_avg

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cur["close"] > dc_high and vol_ok and atr_expanding:
        sl_dist = atr * sl_mult
        sl      = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="donchian_breakout", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cur["close"] < dc_low and vol_ok and atr_expanding:
        sl_dist = atr * sl_mult
        sl      = cur["close"] + sl_dist
        tp2     = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="donchian_breakout", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _inside_bar_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Inside Bar Breakout auf 1h:
      Bedingung: Aktuelle Kerze vollständig innerhalb der Mutter-Kerze (High < Mother.High,
                 Low > Mother.Low) → Kompression.
      Signal wird NICHT auf der Inside Bar selbst gegeben, sondern auf der Breakout-Kerze:
        - Nächste Kerze schließt über Mother.High → Long
        - Nächste Kerze schließt unter Mother.Low  → Short
      EMA-Trendfilter optional (EMA_PERIOD=0 deaktiviert ihn).
      Mindest-Range der Mutter-Kerze: MOTHER_ATR_MIN × ATR (filtert Mikro-Bars).
    """
    ema_period    = int(cfg.get("EMA_PERIOD",    50))
    mother_atr    = cfg.get("MOTHER_ATR_MIN",  0.5)
    sl_mult       = cfg.get("SL_ATR_MULT",     1.0)
    tp_r          = cfg.get("TP_R",            2.0)

    candles = _candles(conn, asset, "1h", as_of_ts, max(ema_period + 5, 30))
    if len(candles) < 5:
        return None

    cur    = candles[-1]   # Breakout-Kerze
    inside = candles[-2]   # muss Inside Bar gewesen sein
    mother = candles[-3]   # Mutter-Kerze

    ts = cur["time"]

    # Inside Bar Bedingung prüfen (auf Basis der zwei Kerzen VOR der aktuellen)
    is_inside = (inside["high"] < mother["high"] and inside["low"] > mother["low"])
    if not is_inside:
        return None

    atr = atr_wilder(candles, 14)
    if atr <= 0:
        return None

    mother_range = mother["high"] - mother["low"]
    if mother_range < mother_atr * atr:
        return None   # Mutter-Kerze zu klein → kein sinnvoller Ausbruch

    closes   = [c["close"] for c in candles]
    ema_val  = ema(closes, ema_period) if ema_period > 0 else None
    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cur["close"] > mother["high"]:
        # Long-Breakout
        if ema_val and cur["close"] < ema_val:
            return None   # Trendfilter: kein Long im Downtrend
        sl_dist = atr * sl_mult
        sl      = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="inside_bar_breakout", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cur["close"] < mother["low"]:
        # Short-Breakout
        if ema_val and cur["close"] > ema_val:
            return None   # Trendfilter: kein Short im Uptrend
        sl_dist = atr * sl_mult
        sl      = cur["close"] + sl_dist
        tp2     = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="inside_bar_breakout", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _dual_donchian_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Adaptive Channel-Breakout (Dual-Donchian):
      Entry-Kanal (länger) bestimmt den Breakout.
      Exit-Kanal (kürzer) dient als Trailing-Stop-Referenz.
      Volumen- und ATR-Filter wie beim klassischen Donchian.
    """
    entry_period = int(cfg.get("ENTRY_PERIOD", 20))
    exit_period  = int(cfg.get("EXIT_PERIOD",  10))
    vol_factor   = cfg.get("VOL_FACTOR",   1.5)
    atr_min      = cfg.get("ATR_MIN_MULT", 1.0)
    sl_mult      = cfg.get("SL_ATR_MULT",  1.0)
    tp_r         = cfg.get("TP_R",         2.0)

    limit = entry_period + 30
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < entry_period + 5:
        return None

    cur = candles[-1]
    ts  = cur["time"]

    entry_window = candles[-(entry_period + 1):-1]
    entry_high   = max(c["high"] for c in entry_window)
    entry_low    = min(c["low"]  for c in entry_window)

    atr     = atr_wilder(candles, 14)
    atr_avg = atr_wilder(candles[:-14], 14) if len(candles) > 28 else atr
    vol_avg = vol_sma(candles, 20)

    if atr <= 0 or vol_avg <= 0:
        return None

    atr_ok = atr >= atr_min * atr_avg if atr_avg > 0 else True
    vol_ok = cur["volume"] >= vol_factor * vol_avg
    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cur["close"] > entry_high and vol_ok and atr_ok:
        sl_dist = atr * sl_mult
        sl = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="dual_donchian", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cur["close"] < entry_low and vol_ok and atr_ok:
        sl_dist = atr * sl_mult
        sl  = cur["close"] + sl_dist
        tp2 = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="dual_donchian", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _bb_kc_squeeze_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    BB + Keltner Channel Squeeze (Volatility-Breakout):
      Squeeze erkannt wenn BB-Breite < KC-Breite (Kompressionsphase).
      Signal wenn Squeeze sich auflöst + Momentum-Richtung bestätigt.
      Momentum = Close - SMA(Close, period).
    """
    bb_period  = int(cfg.get("BB_PERIOD",  20))
    bb_mult    = cfg.get("BB_MULT",    2.0)
    kc_mult    = cfg.get("KC_MULT",    1.5)
    sl_mult    = cfg.get("SL_ATR_MULT", 1.0)
    tp_r       = cfg.get("TP_R",       2.5)

    candles = _candles(conn, asset, "1h", as_of_ts, bb_period + 30)
    if len(candles) < bb_period + 5:
        return None

    cur = candles[-1]
    ts  = cur["time"]
    closes = [c["close"] for c in candles]

    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, bb_period, bb_mult)
    atr    = atr_wilder(candles, bb_period)
    kc_mid = sma(closes, bb_period)
    kc_upper = kc_mid + kc_mult * atr
    kc_lower = kc_mid - kc_mult * atr

    if atr <= 0 or kc_mid <= 0:
        return None

    # Squeeze: vorherige Kerze hatte BB innerhalb KC
    closes_prev = [c["close"] for c in candles[:-1]]
    bb_u_prev, bb_m_prev, bb_l_prev = bollinger_bands(closes_prev, bb_period, bb_mult)
    atr_prev    = atr_wilder(candles[:-1], bb_period)
    kc_mid_prev = sma(closes_prev, bb_period)
    kc_u_prev   = kc_mid_prev + kc_mult * atr_prev
    kc_l_prev   = kc_mid_prev - kc_mult * atr_prev

    was_squeeze = (bb_u_prev < kc_u_prev and bb_l_prev > kc_l_prev)
    is_squeeze  = (bb_upper  < kc_upper  and bb_lower  > kc_lower)

    # Squeeze löst sich auf: vorher drin, jetzt raus
    if not was_squeeze or is_squeeze:
        return None

    # Momentum: Close minus SMA
    momentum = cur["close"] - kc_mid
    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    sl_dist  = atr * sl_mult

    if momentum > 0:
        sl = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="bb_kc_squeeze", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if momentum < 0:
        sl  = cur["close"] + sl_dist
        tp2 = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="bb_kc_squeeze", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _supertrend_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Supertrend Trend-Following (ATR-basiert, 3-facher Konsensfilter):
      Drei unabhängige Supertrend-Instanzen mit unterschiedlichen Parametern.
      Long: alle 3 zeigen 'up' (Preis über Upper Band).
      Short: alle 3 zeigen 'down' (Preis unter Lower Band).
      Richtungswechsel-Signal: vorherige Kerze hatte anderen Konsens.
    """
    configs = [
        (int(cfg.get("ST1_PERIOD", 10)), cfg.get("ST1_MULT", 1.0)),
        (int(cfg.get("ST2_PERIOD", 11)), cfg.get("ST2_MULT", 2.0)),
        (int(cfg.get("ST3_PERIOD", 12)), cfg.get("ST3_MULT", 3.0)),
    ]
    sl_mult  = cfg.get("SL_ATR_MULT", 1.0)
    tp_r     = cfg.get("TP_R",        2.5)

    max_period = max(p for p, _ in configs)
    candles = _candles(conn, asset, "1h", as_of_ts, max_period + 50)
    if len(candles) < max_period + 10:
        return None

    def _supertrend_direction(cands: list, period: int, mult: float) -> Optional[str]:
        if len(cands) < period + 2:
            return None
        closes = [c["close"] for c in cands]
        highs  = [c["high"]  for c in cands]
        lows   = [c["low"]   for c in cands]
        # ATR via Wilder
        atr_val = atr_wilder(cands, period)
        if atr_val <= 0:
            return None
        mid = (highs[-1] + lows[-1]) / 2
        upper = mid + mult * atr_val
        lower = mid - mult * atr_val
        price = closes[-1]
        return "up" if price > lower else "down"

    dirs_cur  = [_supertrend_direction(candles,        p, m) for p, m in configs]
    dirs_prev = [_supertrend_direction(candles[:-1],   p, m) for p, m in configs]

    if None in dirs_cur or None in dirs_prev:
        return None

    all_up_cur   = all(d == "up"   for d in dirs_cur)
    all_down_cur = all(d == "down" for d in dirs_cur)
    all_up_prev   = all(d == "up"   for d in dirs_prev)
    all_down_prev = all(d == "down" for d in dirs_prev)

    # Nur bei Richtungswechsel signalisieren (Einstieg, nicht Fortsetzung)
    if not (all_up_cur and not all_up_prev) and not (all_down_cur and not all_down_prev):
        return None

    cur      = candles[-1]
    ts       = cur["time"]
    atr      = atr_wilder(candles, configs[0][0])
    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    sl_dist  = atr * sl_mult

    if all_up_cur:
        sl = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="supertrend", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if all_down_cur:
        sl  = cur["close"] + sl_dist
        tp2 = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="supertrend", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _atr_channel_breakout_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    ATR-Channel-Breakout auf 1h:
      Dynamischer Channel = EMA(N) ± ATR_BAND * ATR(14)
      Long:  Close > EMA + ATR_BAND * ATR  UND  ATR-Expansion  UND  Vol > VOL_FACTOR * VolSMA
      Short: Spiegelbildlich
    Volatilitätsnormiert — Einstiegsschwelle skaliert mit ATR statt fixem Preisniveau.
    Edge: Filtert enge Ranging-Phasen automatisch heraus (ATR-Expansion zwingend).
    """
    ema_period  = int(cfg.get("EMA_PERIOD",   30))
    atr_band    = cfg.get("ATR_BAND",      2.0)
    atr_min     = cfg.get("ATR_MIN_MULT",  1.0)
    vol_factor  = cfg.get("VOL_FACTOR",    1.3)
    sl_mult     = cfg.get("SL_ATR_MULT",   1.0)
    tp_r        = cfg.get("TP_R",          2.0)

    limit = max(ema_period, 50) + 20
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < ema_period + 15:
        return None

    cur     = candles[-1]
    ts      = cur["time"]
    closes  = [c["close"] for c in candles]

    atr     = atr_wilder(candles, 14)
    atr_avg = atr_wilder(candles[:-14], 14) if len(candles) > 28 else atr
    vol_avg = vol_sma(candles, 20)

    if atr <= 0 or vol_avg <= 0:
        return None

    ema_val = ema(closes, ema_period)
    if ema_val is None or ema_val <= 0:
        return None

    channel_upper = ema_val + atr_band * atr
    channel_lower = ema_val - atr_band * atr
    atr_ok = atr >= atr_min * atr_avg if atr_avg > 0 else True
    vol_ok = cur["volume"] >= vol_factor * vol_avg
    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)

    if cur["close"] > channel_upper and atr_ok and vol_ok:
        sl_dist = atr * sl_mult
        sl = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="atr_channel_breakout", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if cur["close"] < channel_lower and atr_ok and vol_ok:
        sl_dist = atr * sl_mult
        sl  = cur["close"] + sl_dist
        tp2 = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="atr_channel_breakout", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


def _funding_momentum_signal(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """
    Funding-Rate-Momentum auf 1h (Perps-spezifischer Edge):
      Long:  Funding < -FUNDING_THRESH  UND  Close > EMA(EMA_PERIOD)  (Shorts zahlen → Short-Squeeze)
      Short: Funding > +FUNDING_THRESH  UND  Close < EMA(EMA_PERIOD)  (Longs zahlen → Long-Unwind)
    Edge: Extremes negatives Funding bei steigendem Preis signalisiert Short-Squeeze-Potenzial.
    Funding-Daten aus funding_rates-Tabelle (Point-in-Time, kein Look-ahead).
    """
    ema_period     = int(cfg.get("EMA_PERIOD",      50))
    funding_thresh = cfg.get("FUNDING_THRESH",  0.0003)
    sl_mult        = cfg.get("SL_ATR_MULT",        1.0)
    tp_r           = cfg.get("TP_R",               2.0)

    limit = ema_period + 20
    candles = _candles(conn, asset, "1h", as_of_ts, limit)
    if len(candles) < ema_period + 5:
        return None

    cur    = candles[-1]
    ts     = cur["time"]
    closes = [c["close"] for c in candles]

    # Funding Point-in-Time: aktuellstes Funding vor as_of_ts
    try:
        row = conn.execute(
            """SELECT funding_rate FROM funding_rates
               WHERE asset=? AND CAST(funding_time AS INTEGER) <= ?
               ORDER BY CAST(funding_time AS INTEGER) DESC LIMIT 1""",
            (asset, as_of_ts),
        ).fetchone()
        funding_rate = row[0] if row else None
    except Exception:
        funding_rate = None

    if funding_rate is None:
        return None

    atr = atr_wilder(candles, 14)
    if atr <= 0:
        return None

    ema_val = ema(closes, ema_period)
    if ema_val is None or ema_val <= 0:
        return None

    risk_usd = cfg.get("CAPITAL", 68.0) * cfg.get("MAX_RISK_PCT", 0.02)
    sl_dist  = atr * sl_mult

    if funding_rate < -funding_thresh and cur["close"] > ema_val:
        sl = cur["close"] - sl_dist
        if sl <= 0 or sl_dist <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="funding_momentum", asset=asset, direction="long",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] + sl_dist, 6),
            take_profit_2=round(cur["close"] + sl_dist * tp_r, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    if funding_rate > funding_thresh and cur["close"] < ema_val:
        sl  = cur["close"] + sl_dist
        tp2 = cur["close"] - sl_dist * tp_r
        if sl_dist <= 0 or tp2 <= 0:
            return None
        return BtSignal(
            ts=ts, strategy="funding_momentum", asset=asset, direction="short",
            entry_price=round(cur["close"], 6), stop_loss=round(sl, 6),
            take_profit_1=round(cur["close"] - sl_dist, 6),
            take_profit_2=round(tp2, 6),
            size=round(risk_usd / sl_dist, 4), risk_usd=round(risk_usd, 4),
        )

    return None


SIGNAL_FNS = {
    "vaa":                _vaa_signal,
    "kdt":            _kdt_signal,
    "weekend_momo":   _weekend_momo_signal,
    "asian_fade":     _asian_fade_signal,
    "squeeze":        _squeeze_signal,
    "mean_reversion":     _mean_reversion_signal,
    "vwap_bounce":        _vwap_bounce_signal,
    "ema_pullback":       _ema_pullback_signal,
    "donchian_breakout":  _donchian_breakout_signal,
    "inside_bar_breakout": _inside_bar_signal,
    "dual_donchian":           _dual_donchian_signal,
    "bb_kc_squeeze":           _bb_kc_squeeze_signal,
    "supertrend":              _supertrend_signal,
    "orb":                     orb_engine_adapter,
    "atr_channel_breakout":    _atr_channel_breakout_signal,
    "funding_momentum":        _funding_momentum_signal,
}

STRATEGY_INTERVAL = {
    "vaa":                     "1h",
    "kdt":                     "1h",
    "weekend_momo":            "1d",
    "asian_fade":              "1h",
    "squeeze":                 "1h",
    "mean_reversion":          "1h",
    "vwap_bounce":             "1h",
    "ema_pullback":            "1h",
    "donchian_breakout":       "1h",
    "inside_bar_breakout":     "1h",
    "dual_donchian":           "1h",
    "bb_kc_squeeze":           "1h",
    "supertrend":              "1h",
    "orb":                     "1h",
    "atr_channel_breakout":    "1h",
    "funding_momentum":        "1h",
}

EXIT_INTERVAL = {
    "vaa":                     "1h",
    "kdt":                     "1h",
    "weekend_momo":            "4h",
    "asian_fade":              "1h",
    "squeeze":                 "1h",
    "mean_reversion":          "1h",
    "vwap_bounce":             "1h",
    "ema_pullback":            "1h",
    "donchian_breakout":       "1h",
    "inside_bar_breakout":     "1h",
    "dual_donchian":           "1h",
    "bb_kc_squeeze":           "1h",
    "supertrend":              "1h",
    "orb":                     "1h",
    "atr_channel_breakout":    "1h",
    "funding_momentum":        "1h",
}


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}


def _lookup_funding_pit(conn, asset: str, start_ts: int, end_ts: int) -> float:
    """
    Point-in-Time Funding aus funding_rates-Tabelle.
    Summiert alle Funding-Zahlungen im Haltezeitraum.
    Fallback: 0.0 (FUNDING_8H wird separat als Konstante angewendet wenn keine Daten).
    """
    rows = conn.execute(
        """SELECT funding_rate FROM funding_rates
           WHERE asset=? AND funding_time >= ? AND funding_time < ?
           ORDER BY funding_time ASC""",
        (asset, str(start_ts), str(end_ts)),
    ).fetchall()
    return sum(r[0] for r in rows) if rows else 0.0


def _apply_trade_costs(trade: BtTrade, conn=None) -> BtTrade:
    """
    Zieht Transaktionskosten vom abgeschlossenen Trade ab.
    Kostenmodell: Round-Trip-Fee+Slippage + Funding (Point-in-Time wenn verfügbar).
    """
    from config.settings import ROUND_TRIP, FUNDING_8H, MAKER_FEE, TAKER_FEE, SLIPPAGE_EST, V7_MAKER_TAKER_SPLIT
    sig      = trade.signal
    notional = sig.entry_price * sig.size

    if V7_MAKER_TAKER_SPLIT:
        # IOC-Limit → Maker-Fee bei Fill; sonst Taker-Fee (konservativ immer Taker Exit)
        order_type = getattr(sig, "order_type_used", "ioc_limit")
        entry_fee  = MAKER_FEE if order_type == "ioc_limit" else TAKER_FEE
        exit_fee   = TAKER_FEE  # Exits via Market/SL → immer Taker
        rt_cost = notional * (entry_fee + SLIPPAGE_EST + exit_fee + SLIPPAGE_EST)
    else:
        rt_cost  = notional * ROUND_TRIP

    # Point-in-Time Funding aus DB; Fallback auf statisches Modell
    if conn is not None and trade.exit_ts:
        pit_funding_rate = _lookup_funding_pit(conn, sig.asset, trade.entry_ts, trade.exit_ts)
        if pit_funding_rate != 0.0:
            funding = notional * abs(pit_funding_rate)
            trade.intrabar_model_used = trade.intrabar_model_used  # unverändertes Flag
        else:
            # Kein PIT-Funding → statisches Modell als Fallback
            periods = (trade.exit_ts - trade.entry_ts) / (8 * 3_600_000)
            funding = notional * FUNDING_8H * periods
    else:
        periods = (trade.exit_ts - trade.entry_ts) / (8 * 3_600_000) if trade.exit_ts else 0
        funding = notional * FUNDING_8H * periods

    total_cost  = rt_cost + funding
    sl_dist     = abs(sig.entry_price - sig.stop_loss)
    denominator = sl_dist * sig.size

    trade.pnl_usd = round(trade.pnl_usd - total_cost, 4)
    trade.pnl_r   = round(trade.pnl_usd / denominator, 3) if denominator > 0 else 0.0
    return trade


def run_backtest(
    strategy: str,
    asset: str,
    start_ts: int,
    end_ts: int,
    cfg: dict = None,
    max_exit_bars: int = 48,
    cooldown_bars: int = 0,
    verbose: bool = False,
    apply_costs: bool = True,
    candles_override: list[dict] | None = None,
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
        apply_costs:   Transaktionskosten (Fees + Slippage + Funding) einrechnen
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

    interval_ms      = INTERVAL_MS.get(interval, 3_600_000)
    cooldown_until_ts: int = 0   # kein Cooldown aktiv am Start

    # Timestamps aus Override oder DB laden
    if candles_override is not None:
        timestamps = sorted(c["time"] for c in candles_override)
        # Override-Kerzen in temporäre Lookup-Struktur einlesen
        _candle_cache: dict[int, dict] = {c["time"]: c for c in candles_override}
    else:
        timestamps = [
            r[0] for r in conn.execute(
                """SELECT DISTINCT ts FROM candles
                   WHERE asset=? AND interval=? AND ts >= ? AND ts <= ?
                   ORDER BY ts ASC""",
                (asset, interval, start_ts, end_ts),
            ).fetchall()
        ]
        _candle_cache = None

    log(f"[BACKTEST] {strategy}/{asset}: {len(timestamps)} Bars von "
        f"{datetime.fromtimestamp(start_ts/1000, tz=timezone.utc).date()} bis "
        f"{datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).date()}"
        + (f" cooldown={cooldown_bars}bars" if cooldown_bars > 0 else ""))

    open_trade: Optional[BtTrade] = None

    for ts in timestamps:
        # Offenen Trade schließen falls Exit-Bedingung erreicht
        if open_trade and not open_trade.closed and ts > open_trade.signal.ts:
            open_trade = _simulate_exit(conn, open_trade, asset, exit_intv, max_exit_bars)
            if open_trade.closed:
                if apply_costs:
                    open_trade = _apply_trade_costs(open_trade, conn=conn)
                result.trades.append(open_trade)
                if verbose:
                    log(f"[BACKTEST]   EXIT {open_trade.exit_reason} "
                        f"pnl={open_trade.pnl_r:+.2f}R @ {open_trade.exit_price}")
                if cooldown_bars > 0 and open_trade.exit_ts:
                    cooldown_until_ts = open_trade.exit_ts + cooldown_bars * interval_ms
                open_trade = None

        if open_trade:
            continue  # kein neuer Trade solange Position offen

        if cooldown_bars > 0 and ts <= cooldown_until_ts:
            continue  # Cooldown nach letztem Exit aktiv

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
            if apply_costs:
                open_trade = _apply_trade_costs(open_trade)
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
    if strategy == "squeeze":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "SQUEEZE_PERIOD": 20, "EMA_PERIOD": 20,
                "SL_ATR_MULT": 1.0, "TP_R": 3.0}
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
    if strategy == "ema_pullback":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "EMA_SLOW": 200, "EMA_FAST": 50, "BODY_FACTOR": 0.3,
                "SL_ATR_MULT": 1.0, "TP_R": 2.5}
    if strategy == "donchian_breakout":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "DC_PERIOD": 20, "VOL_FACTOR": 1.5, "ATR_MIN_MULT": 1.0,
                "SL_ATR_MULT": 1.0, "TP_R": 2.0}
    if strategy == "inside_bar_breakout":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "EMA_PERIOD": 50, "MOTHER_ATR_MIN": 0.5,
                "SL_ATR_MULT": 1.0, "TP_R": 2.0}
    if strategy == "mean_reversion":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "BB_PERIOD": 20, "BB_MULT": 2.0, "RSI_PERIOD": 14,
                "RSI_OS": 35.0, "SL_ATR_MULT": 1.0, "TP_R": 2.0}
    if strategy == "vwap_bounce":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "VWAP_PERIOD": 24, "VWAP_BAND": 0.25, "EMA_PERIOD": 50,
                "RSI_MIN": 50.0, "SL_ATR_MULT": 1.0, "TP_R": 2.5}
    if strategy == "asian_fade":
        from config.settings import (ASIAN_FADE_PUMP_THRESHOLD, ASIAN_FADE_RSI_OB,
                                      ASIAN_FADE_SL_ATR_MULT, ASIAN_FADE_TP_MULT,
                                      ASIAN_FADE_MAX_RISK_PCT)
        return {**base, "MAX_RISK_PCT": ASIAN_FADE_MAX_RISK_PCT,
                "PUMP_THRESHOLD": ASIAN_FADE_PUMP_THRESHOLD,
                "RSI_OB": ASIAN_FADE_RSI_OB,
                "SL_ATR_MULT": ASIAN_FADE_SL_ATR_MULT,
                "TP_MULT": ASIAN_FADE_TP_MULT}
    if strategy == "dual_donchian":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "ENTRY_PERIOD": 20, "EXIT_PERIOD": 10,
                "VOL_FACTOR": 1.5, "ATR_MIN_MULT": 1.0,
                "SL_ATR_MULT": 1.0, "TP_R": 2.0}
    if strategy == "bb_kc_squeeze":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "BB_PERIOD": 20, "BB_MULT": 2.0,
                "KC_MULT": 1.5, "SL_ATR_MULT": 1.0, "TP_R": 2.5}
    if strategy == "supertrend":
        return {**base, "MAX_RISK_PCT": MAX_RISK_PCT,
                "ST1_PERIOD": 10, "ST1_MULT": 1.0,
                "ST2_PERIOD": 11, "ST2_MULT": 2.0,
                "ST3_PERIOD": 12, "ST3_MULT": 3.0,
                "SL_ATR_MULT": 1.0, "TP_R": 2.5}
    return base
