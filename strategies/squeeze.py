"""
Squeeze Breakout Strategy Agent

Volatility-Expansion Strategie. Erkennt TTM-Squeeze-Release auf 1h-Basis.
Entry in Richtung EMA(25): Release über EMA = LONG, darunter = SHORT.

Champion-Parameter (Auto-Lab, 2026-04-27):
  SQUEEZE_PERIOD=20, EMA_PERIOD=25, SL_ATR_MULT=1.5, TP_R=3.0
  OOS: n=1924, PF=1.14, Avg R=+0.095R | Train: PF=1.05, Avg R=+0.037R
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
from features.indicators import is_squeeze, ema, atr_wilder
from config.settings import (
    SQUEEZE_ENABLED, SQUEEZE_ASSETS,
    SQUEEZE_PERIOD, SQUEEZE_EMA_PERIOD,
    SQUEEZE_SL_ATR_MULT, SQUEEZE_TP_R,
    SQUEEZE_MAX_RISK_PCT, CAPITAL,
)


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


class SqueezeStrategy(BaseStrategy):
    """
    TTM Squeeze Breakout — Volatility Expansion Entry.

    Kann als Standard-Instanz (settings.py) ODER als Deploy-Instanz
    mit benutzerdefinierten Parametern laufen:

        SqueezeStrategy()                         → Standard (Champion-Params)
        SqueezeStrategy(deploy_cfg={...}, ...)    → Lab-Discovery Forward-Test

    Deploy-Instanzen bekommen eine eindeutige strategy_key (z.B. "squeeze_42"),
    damit ihre Trades in der DB nie mit Standard-Trades vermischt werden.
    """

    def __init__(self, deploy_cfg: dict | None = None,
                 deploy_assets: list[str] | None = None,
                 strategy_key: str | None = None,
                 mode_override: str | None = None):
        # deploy_cfg=None → Standard-Modus (Champion-Params aus settings.py)
        self._deploy_cfg      = deploy_cfg
        self._deploy_assets   = deploy_assets
        self._strategy_key    = strategy_key    # z.B. "squeeze_42"
        self._mode_override   = mode_override   # überschreibt get_strategy_mode()

    @property
    def name(self) -> str:
        # Entscheidende Stelle: Deploy-Instanzen haben ihre eigene strategy_key.
        # Alle INSERT INTO signals/trades nutzen signal.strategy → strikt getrennt.
        return self._strategy_key if self._strategy_key else "squeeze"

    @property
    def assets(self) -> list[str]:
        return self._deploy_assets if self._deploy_assets else SQUEEZE_ASSETS

    def _param(self, key: str, default):
        """Liest aus deploy_cfg oder fällt auf settings.py zurück."""
        if self._deploy_cfg:
            return self._deploy_cfg.get(key, default)
        return default

    def generate_signals(self) -> list[Signal]:
        if not SQUEEZE_ENABLED and not self._deploy_cfg:
            return []

        # Parameter: deploy_cfg überschreibt settings.py
        sq_period    = self._param("SQUEEZE_PERIOD", SQUEEZE_PERIOD)
        ema_period   = self._param("EMA_PERIOD",     SQUEEZE_EMA_PERIOD)
        sl_atr_mult  = self._param("SL_ATR_MULT",    SQUEEZE_SL_ATR_MULT)
        tp_r         = self._param("TP_R",            SQUEEZE_TP_R)
        max_risk_pct = self._param("MAX_RISK_PCT",    SQUEEZE_MAX_RISK_PCT)

        signals = []
        conn = get_connection()

        for asset in self.assets:
            if self._mode_override:
                mode = self._mode_override
            else:
                mode = get_strategy_mode("squeeze", asset)

            candles = _get_candles(conn, asset, limit=sq_period + 2)
            if len(candles) < sq_period + 2:
                log(f"[{self.name}] {asset}: zu wenig Candles ({len(candles)}<{sq_period+2})")
                continue

            squeeze_prev = is_squeeze(candles[-(sq_period+2):-2], sq_period)
            squeeze_now  = is_squeeze(candles[-(sq_period+1):],   sq_period)

            if squeeze_prev or not squeeze_now:
                continue

            closes        = [c["close"] for c in candles]
            ema_val       = ema(closes, ema_period)
            current_close = candles[-1]["close"]
            direction     = "long" if current_close > ema_val else "short"

            atr = atr_wilder(candles, 14)
            if atr <= 0:
                continue

            entry   = current_close
            sl_dist = atr * sl_atr_mult
            if direction == "long":
                sl = entry - sl_dist
                tp = entry + sl_dist * tp_r
            else:
                sl = entry + sl_dist
                tp = entry - sl_dist * tp_r

            risk_usd = CAPITAL * max_risk_pct
            size     = round(risk_usd / sl_dist, 4) if sl_dist > 0 else 0.0

            from config.settings import SIZE_DECIMALS, PRICE_DECIMALS
            dec_size  = SIZE_DECIMALS.get(asset, 2)
            dec_price = PRICE_DECIMALS.get(asset, 2)

            sig = Signal(
                created_at=now_iso(),
                strategy=self.name,   # ← "squeeze" oder "squeeze_42" — DB-Trennung
                asset=asset,
                direction=direction,
                entry_price=round(entry, dec_price),
                stop_loss=round(sl, dec_price),
                take_profit_1=round(tp, dec_price),
                take_profit_2=round(tp, dec_price),
                size=round(size, dec_size),
                risk_usd=round(risk_usd, 4),
                session=None,
                status="pending",
                mode=mode,
            )
            row_id = _save_signal(conn, sig)
            if row_id == 0:
                log(f"[{self.name}] {asset}: Signal heute bereits vorhanden — überspringe (Dedup)")
                continue
            log(f"[{self.name}] {asset} {direction.upper()} @ {entry:.2f} "
                f"SL={sl:.2f} TP={tp:.2f} mode={mode}")
            signals.append(sig)

        conn.close()
        return signals
