"""
ORB Signal-Funktion für Backtest-Engine und Auto-Lab.

Stateless — keine DB-Abhängigkeit. Die Opening Range wird inline
aus dem übergebenen DataFrame berechnet.

Sessions (UTC):
  tokyo: 01:00–03:00
  eu:    08:00–10:00
  us:    20:00–22:00
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from backtest.models import BtSignal

# ── Session-Definitionen (UTC-Stunden, halb-offen) ───────────────────────────
_SESSIONS: dict[str, tuple[int, int]] = {
    "tokyo": (1,  3),
    "eu":    (8,  10),
    "us":    (20, 22),
}

# ── Default-Parameter ────────────────────────────────────────────────────────
_DEFAULTS: dict[str, float | int | str] = {
    "breakout_threshold_pct":  0.001,
    "min_box_range_pct":       0.003,
    "max_box_age_bars":        6,
    "volume_ratio_min":        1.5,
    "max_breakout_dist_ratio": 2.0,
    "session":                 "all",   # "all" = alle drei Sessions prüfen
}


def _active_session(hour_utc: int) -> str | None:
    for name, (start, end) in _SESSIONS.items():
        if start <= hour_utc < end:
            return name
    return None


def orb_signal_fn(df: pd.DataFrame, asset: str, params: dict | None = None) -> BtSignal | None:
    """
    Opening-Range-Breakout Signalgenerator.

    Args:
        df:     OHLCV-DataFrame mit Spalten ts (Unix-ms), open, high, low, close, volume.
                Muss chronologisch aufsteigend sortiert sein.
        asset:  Asset-Name (z.B. "SOL").
        params: Optuna-Parameter-Dict (überschreibt Defaults).

    Returns:
        BtSignal oder None wenn kein valider Breakout vorliegt.
    """
    if len(df) < 22:
        return None

    p = {**_DEFAULTS, **(params or {})}

    breakout_threshold_pct  = float(p["breakout_threshold_pct"])
    min_box_range_pct       = float(p["min_box_range_pct"])
    max_box_age_bars        = int(p["max_box_age_bars"])
    volume_ratio_min        = float(p["volume_ratio_min"])
    max_breakout_dist_ratio = float(p["max_breakout_dist_ratio"])
    session_filter          = str(p["session"])  # "all" | "tokyo" | "eu" | "us"

    # Timestamps in UTC-Stunden umrechnen
    ts_utc = pd.to_datetime(df["ts"], unit="ms", utc=True)
    hours  = ts_utc.dt.hour

    # Aktueller (letzter) Bar
    cur      = df.iloc[-1]
    cur_hour = int(hours.iloc[-1])
    cur_ts   = int(cur["ts"])

    # Prüfe welche Session(s) für diesen Bar relevant sind
    # Wir suchen: gibt es eine Session, deren Fenster im df liegt,
    # und der aktuelle Bar liegt nach dem Session-Ende (Breakout-Zone)
    best_signal: BtSignal | None = None

    sessions_to_check = list(_SESSIONS.keys()) if session_filter == "all" else [session_filter]

    for sess_name in sessions_to_check:
        start_h, end_h = _SESSIONS[sess_name]

        # Session-Bars: gleicher Kalendertag (UTC) wie der aktuelle Bar
        cur_date = ts_utc.iloc[-1].date()
        same_day = ts_utc.dt.date == cur_date
        in_session = same_day & (hours >= start_h) & (hours < end_h)

        session_bars = df[in_session]
        if len(session_bars) < 2:
            continue  # Zu wenige Bars für eine valide Box

        box_high = float(session_bars["high"].max())
        box_low  = float(session_bars["low"].min())
        box_range = box_high - box_low

        close = float(cur["close"])

        # Box-Range-Filter
        if box_range < min_box_range_pct * close:
            continue

        # Box-Alter: aktueller Bar muss nach Session-Ende liegen,
        # aber nicht mehr als max_box_age_bars Bars entfernt
        last_session_idx = session_bars.index[-1]
        all_idx = df.index.tolist()
        try:
            last_session_pos = all_idx.index(last_session_idx)
        except ValueError:
            continue
        cur_pos = len(df) - 1
        bars_after_session = cur_pos - last_session_pos
        if bars_after_session <= 0 or bars_after_session > max_box_age_bars:
            continue

        # Breakout-Check
        long_trigger  = box_high * (1.0 + breakout_threshold_pct)
        short_trigger = box_low  * (1.0 - breakout_threshold_pct)

        if close > long_trigger:
            direction = "long"
            breakout_level = box_high
        elif close < short_trigger:
            direction = "short"
            breakout_level = box_low
        else:
            continue

        # Late-Entry-Guard
        breakout_dist = abs(close - breakout_level)
        if breakout_dist > max_breakout_dist_ratio * box_range:
            continue

        # Body-Ratio (kein Doji)
        c_high  = float(cur["high"])
        c_low   = float(cur["low"])
        c_open  = float(cur["open"])
        c_range = c_high - c_low + 1e-9
        c_body  = abs(close - c_open)
        if c_body / c_range < 0.30:
            continue

        # Volume-Filter
        vol_series = df["volume"].astype(float)
        vol_sma20  = vol_series.rolling(20).mean().iloc[-1]
        if pd.notna(vol_sma20) and vol_sma20 > 0:
            if float(cur["volume"]) < volume_ratio_min * vol_sma20:
                continue

        # SL / TP berechnen — klassisches ORB: volle Box als Risiko
        sl_buffer = float(p.get("sl_buffer_pct", 0.001))
        entry     = close

        if direction == "long":
            stop_loss    = box_low  * (1.0 - sl_buffer)
            sl_dist      = entry - stop_loss
            take_profit1 = entry + sl_dist * 1.5
            take_profit2 = entry + sl_dist * 3.0
        else:
            stop_loss    = box_high * (1.0 + sl_buffer)
            sl_dist      = stop_loss - entry
            take_profit1 = entry - sl_dist * 1.5
            take_profit2 = entry - sl_dist * 3.0

        if sl_dist <= 0 or sl_dist / entry < 0.002:
            continue

        risk_usd = 100.0  # Platzhalter; Engine überschreibt mit CAPITAL × MAX_RISK_PCT
        size     = risk_usd / sl_dist

        best_signal = BtSignal(
            ts=cur_ts, strategy="orb", asset=asset, direction=direction,
            entry_price=round(entry, 6), stop_loss=round(stop_loss, 6),
            take_profit_1=round(take_profit1, 6), take_profit_2=round(take_profit2, 6),
            size=round(size, 4), risk_usd=round(risk_usd, 4),
        )
        break  # Erste valide Session gewinnt

    return best_signal


# ── Engine-kompatibler Wrapper ────────────────────────────────────────────────
# Signatur: (conn, asset, as_of_ts, cfg) → Optional[BtSignal]
# Lädt den point-in-time DataFrame aus der DB und delegiert an orb_signal_fn.

def orb_engine_adapter(conn, asset: str, as_of_ts: int, cfg: dict) -> Optional[BtSignal]:
    """Wrapper für backtest/engine.py run_backtest — baut df aus DB und ruft orb_signal_fn."""
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume
           FROM candles
           WHERE asset=? AND interval='1h' AND ts <= ?
           ORDER BY ts DESC LIMIT 500""",
        (asset, as_of_ts),
    ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.sort_values("ts").reset_index(drop=True)
    return orb_signal_fn(df, asset, params=cfg)
