"""
ORB Strategy Agent — Opening Range Breakout

Liest: candles (5m, 15m, 4h), features, opening_ranges, system_state aus SQLite.
Schreibt: Signals in die signals-Tabelle.
Sendet: KEINE Orders.

Filter-Gauntlet (identisch zu V1 autonomous_trade.py):
  1. Box-Alter <= MAX_BOX_AGE_MIN
  2. Box-Range >= MIN_BOX_RANGE
  3. Breakout erkannt (Preis > box_high + threshold oder < box_low - threshold)
  4. Late-Entry-Guard (Distanz <= 2x Box-Range)
  5. 5m-Candle-Close-Bestätigung
  6. Candle-Body >= 30% (kein Doji)
  7. H-014 Volume-Ratio >= 2.0x
  8. H-006 EMA-200 (15m) Alignment
  9. H-006 EMA-50 (4h) Alignment (optional)

Fail-safe: fehlende Feature-Daten blockieren NICHT (kein False-Negative).
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
    ORB_ASSETS, ORB_ASSET_PRIORITY,
    BREAKOUT_THRESHOLD, MIN_BOX_RANGE, MAX_BOX_AGE_MIN,
    MAX_BREAKOUT_DISTANCE_RATIO, MAX_SPREAD_PCT,
    H006_EMA_FILTER_ENABLED, H006_REQUIRE_H4_ALIGN,
    H014_VOLUME_FILTER_ENABLED, H014_VOLUME_RATIO_MIN,
    MAX_RISK_PCT, CAPITAL, MIN_RR_RATIO, TELEGRAM_V2_PREFIX,
)


def _get_latest_candle(conn, asset: str, interval: str) -> dict | None:
    """Neueste abgeschlossene Kerze für (asset, interval)."""
    row = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT 1",
        (asset, interval),
    ).fetchone()
    return dict(row) if row else None


def _get_candles(conn, asset: str, interval: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT ?",
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


def _get_opening_range(conn, asset: str, session: str, date: str) -> dict | None:
    row = conn.execute(
        "SELECT high, low, open, close, ts FROM opening_ranges WHERE asset=? AND session=? AND date=?",
        (asset, session, date),
    ).fetchone()
    return dict(row) if row else None


def _get_current_session() -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    h = now.hour
    if 2 <= h < 4:
        return "tokyo"
    if 9 <= h < 11:
        return "eu"
    if 21 <= h < 23:
        return "us"
    return ""


def _check_breakout(asset: str, price: float, box_high: float, box_low: float) -> str | None:
    t = BREAKOUT_THRESHOLD.get(asset, price * 0.002)
    if price > box_high + t:
        return "long"
    if price < box_low - t:
        return "short"
    return None


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


class ORBStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "orb"

    @property
    def assets(self) -> list[str]:
        return ORB_ASSETS

    def generate_signals(self) -> list[Signal]:
        session = _get_current_session()
        if not session:
            log("[ORB] Kein aktives Handelsfenster")
            return []

        today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn      = get_connection()
        signals   = []

        # Bereits geöffnete Positionen aus system_state lesen
        open_assets_raw = conn.execute(
            "SELECT value FROM system_state WHERE key='open_positions'",
        ).fetchone()
        open_assets = json.loads(open_assets_raw[0]) if open_assets_raw else []

        for asset in ORB_ASSET_PRIORITY:
            if asset not in ORB_ASSETS:
                continue

            mode = get_strategy_mode("orb", asset)
            log(f"[ORB] {asset}/{session} — Modus: {mode}")

            # ── 1. Position bereits offen? ────────────────────────────────────
            if asset in open_assets:
                log(f"[ORB] {asset}: Position bereits offen → Skip")
                continue

            # ── 2. Opening-Range-Box holen ───────────────────────────────────
            box = _get_opening_range(conn, asset, session, today)
            if not box:
                log(f"[ORB] {asset}: Keine Box für {session}/{today} → Skip")
                continue

            # ── 3. Box-Alter prüfen ──────────────────────────────────────────
            box_dt  = datetime.fromtimestamp(box["ts"] / 1000, tz=timezone.utc)
            age_min = (datetime.now(timezone.utc) - box_dt).total_seconds() / 60
            if age_min > MAX_BOX_AGE_MIN:
                log(f"[ORB] {asset}: Box {age_min:.0f}min alt > {MAX_BOX_AGE_MIN}min → Skip")
                continue

            box_high  = box["high"]
            box_low   = box["low"]
            box_range = box_high - box_low

            # ── 4. Box-Range prüfen ──────────────────────────────────────────
            min_range = MIN_BOX_RANGE.get(asset, box_high * 0.0005)
            if box_range < min_range:
                log(f"[ORB] {asset}: Box-Range {box_range:.4f} < {min_range:.4f} → Skip")
                continue

            # ── 5. Aktuellen Preis aus neuester 5m-Candle ───────────────────
            c5m_last = _get_latest_candle(conn, asset, "5m")
            if not c5m_last:
                log(f"[ORB] {asset}: Keine 5m-Candle in DB → Skip")
                continue
            current_price = c5m_last["close"]

            # ── 6. Breakout-Erkennung ────────────────────────────────────────
            direction = _check_breakout(asset, current_price, box_high, box_low)
            if not direction:
                log(f"[ORB] {asset}: Kein Breakout (price={current_price:.4f}) → Skip")
                continue

            # ── 7. Late-Entry-Guard ──────────────────────────────────────────
            breakout_level = box_high if direction == "long" else box_low
            breakout_dist  = abs(current_price - breakout_level)
            if breakout_dist > box_range * MAX_BREAKOUT_DISTANCE_RATIO:
                log(f"[ORB] {asset}: Late-Entry ({breakout_dist:.4f} > {MAX_BREAKOUT_DISTANCE_RATIO}x Range) → Skip")
                continue

            # ── 8. 5m-Candle-Close-Bestätigung + Body/Volume ─────────────────
            candles_5m = _get_candles(conn, asset, "5m", 22)
            if len(candles_5m) < 2:
                log(f"[ORB] {asset}: Zu wenige 5m-Candles → Skip")
                continue

            last_closed = candles_5m[-2]  # letzte abgeschlossene Kerze
            candle_close = last_closed["close"]

            if direction == "long" and candle_close <= box_high:
                log(f"[ORB] {asset}: 5m-Close {candle_close:.4f} <= box_high {box_high:.4f} → Nicht bestätigt")
                continue
            if direction == "short" and candle_close >= box_low:
                log(f"[ORB] {asset}: 5m-Close {candle_close:.4f} >= box_low {box_low:.4f} → Nicht bestätigt")
                continue

            # Body-Stärke
            c_range = last_closed["high"] - last_closed["low"]
            c_body  = abs(last_closed["close"] - last_closed["open"])
            body_ratio = c_body / c_range if c_range > 0 else 0.0
            if body_ratio < 0.3:
                log(f"[ORB] {asset}: Schwache Breakout-Kerze body={body_ratio:.0%} < 30% → Skip")
                continue

            # ── 9. H-014 Volume-Filter ───────────────────────────────────────
            if H014_VOLUME_FILTER_ENABLED:
                ts_last = last_closed["time"]
                vol_sma_feat = _get_feature(conn, asset, "5m", ts_last, "vol_sma_20_5m")
                if vol_sma_feat and vol_sma_feat > 0:
                    vol_ratio = last_closed["volume"] / vol_sma_feat
                    if vol_ratio < H014_VOLUME_RATIO_MIN:
                        log(f"[ORB] {asset}: Vol-Ratio {vol_ratio:.2f}x < {H014_VOLUME_RATIO_MIN}x → Skip")
                        continue
                # Fail-safe: kein Feature → kein Block

            # ── 10. H-006 EMA-200 (15m) Alignment ───────────────────────────
            if H006_EMA_FILTER_ENABLED:
                c15m_last = _get_latest_candle(conn, asset, "15m")
                if c15m_last:
                    ema200 = _get_feature(conn, asset, "15m", c15m_last["ts"], "ema_200_15m")
                    if ema200 and ema200 > 0:
                        above = c15m_last["close"] > ema200
                        if direction == "long" and not above:
                            log(f"[ORB] {asset}: LONG gegen EMA-200 (close={c15m_last['close']:.2f} < ema200={ema200:.2f}) → Skip")
                            continue
                        if direction == "short" and above:
                            log(f"[ORB] {asset}: SHORT gegen EMA-200 → Skip")
                            continue
                    # Fail-safe: ema200 None/0 → kein Block

                # H-006 4h-Alignment
                if H006_REQUIRE_H4_ALIGN:
                    c4h_last = _get_latest_candle(conn, asset, "4h")
                    if c4h_last:
                        ema50_4h = _get_feature(conn, asset, "4h", c4h_last["ts"], "ema_50_4h")
                        if ema50_4h and ema50_4h > 0:
                            above4h = c4h_last["close"] > ema50_4h
                            if direction == "long" and not above4h:
                                log(f"[ORB] {asset}: LONG gegen 4H-EMA-50 → Skip")
                                continue
                            if direction == "short" and above4h:
                                log(f"[ORB] {asset}: SHORT gegen 4H-EMA-50 → Skip")
                                continue

            # ── 11. Signal-Sizing ─────────────────────────────────────────────
            entry  = current_price
            sl     = box_low - (box_range * 0.1) if direction == "long" else box_high + (box_range * 0.1)
            sl_dist = abs(entry - sl)
            if sl_dist <= 0:
                continue

            risk_usd = CAPITAL * MAX_RISK_PCT
            size     = risk_usd / sl_dist
            tp1      = entry + sl_dist * 1.0 if direction == "long" else entry - sl_dist * 1.0
            tp2      = entry + sl_dist * 3.0 if direction == "long" else entry - sl_dist * 3.0

            # ── 12. Signal in DB schreiben ────────────────────────────────────
            # UNIQUE-Schutz: nur ein Signal pro (strategy, asset, session, date, mode)
            existing = conn.execute(
                """SELECT id FROM signals
                   WHERE strategy='orb' AND asset=? AND session=? AND date(created_at)=? AND mode=?
                   AND status NOT IN ('rejected', 'expired')""",
                (asset, session, today, mode),
            ).fetchone()
            if existing:
                log(f"[ORB] {asset}: Signal für {session}/{today} bereits vorhanden → Skip")
                continue

            signal = Signal(
                strategy="orb", asset=asset, direction=direction, mode=mode,
                entry_price=round(entry, 4), stop_loss=round(sl, 4),
                take_profit_1=round(tp1, 4), take_profit_2=round(tp2, 4),
                size=round(size, 4), risk_usd=round(risk_usd, 4),
                session=session, created_at=now_iso(), status="pending",
            )
            sig_id = _save_signal(conn, signal)
            signal.id = sig_id
            signals.append(signal)
            log(f"[ORB] {TELEGRAM_V2_PREFIX} Signal: {asset} {direction.upper()} @ {entry:.4f} | SL={sl:.4f} | TP2={tp2:.4f} | mode={mode}")

        conn.close()
        return signals
