"""
Governance Checks — einzelne, isolierte Risikoregeln.

Reihenfolge (in gate.py registriert):
  1. check_signal_expiry      — abgelaufene Signale sofort rausfiltern
  2. check_drawdown_kill      — HWM-50% Kill-Switch
  3. check_daily_dd           — Tages-DD-Breaker (-2R)
  4. check_regime             — crash/unknown → NO-TRADE
  5. check_sizing_sanity      — Balance > MIN_BALANCE, SL plausibel
  6. check_position_open      — Asset bereits in offener Position
  7. check_session_traded     — Bereits in dieser Session gehandelt
"""

import json
from datetime import datetime, timezone
from typing import Tuple

from core.db import get_connection
from core.models import Signal
from core.state import get_hwm, get_daily_pnl, get_regime, get_live_balance
from core.utils import log
from config.settings import (
    DRAWDOWN_KILL_PCT, MIN_BALANCE_USD,
    DAILY_DD_KILL_R,
    SIGNAL_EXPIRY_MINUTES,
)
from governance.gate import BaseGovernanceCheck


class SignalExpiryCheck(BaseGovernanceCheck):
    """Signal zu alt → rejected statt pending lassen."""

    @property
    def name(self) -> str:
        return "signal_expiry"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        created = datetime.fromisoformat(signal.created_at.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age_min > SIGNAL_EXPIRY_MINUTES:
            return False, f"expired: {age_min:.0f}min > {SIGNAL_EXPIRY_MINUTES}min"
        return True, f"age={age_min:.1f}min"


class DrawdownKillCheck(BaseGovernanceCheck):
    """HWM-50% Kill-Switch: Live-Balance unter 50% des All-Time-High → Stop."""

    @property
    def name(self) -> str:
        return "drawdown_kill"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        if signal.mode == "shadow":
            return True, "shadow_skip"
        hwm = get_hwm()
        if hwm <= 0:
            return True, "hwm_not_set"
        balance = get_live_balance()
        if balance <= 0:
            return True, "balance_unknown_skip_hwm_check"
        kill_level = hwm * (1.0 - DRAWDOWN_KILL_PCT)
        if balance < kill_level:
            return False, f"drawdown_kill: balance={balance:.2f} < hwm*{1-DRAWDOWN_KILL_PCT:.0%}={kill_level:.2f}"
        return True, f"balance={balance:.2f} hwm={hwm:.2f}"


class DailyDrawdownCheck(BaseGovernanceCheck):
    """Tages-PnL-Breaker: wenn pnl_r <= DAILY_DD_KILL_R → Stop."""

    @property
    def name(self) -> str:
        return "daily_dd"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        daily = get_daily_pnl()   # dict: {date, pnl_r, pnl_usd, trades}
        pnl_r = daily.get("pnl_r", 0.0)
        if pnl_r <= DAILY_DD_KILL_R:
            return False, f"daily_dd_kill: pnl_r={pnl_r:.2f} <= {DAILY_DD_KILL_R}"
        return True, f"daily_pnl_r={pnl_r:.2f}"


class RegimeCheck(BaseGovernanceCheck):
    """crash/unknown Regime → kein Trading."""

    NO_TRADE_REGIMES = {"crash", "unknown"}

    @property
    def name(self) -> str:
        return "regime"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        regime_data = get_regime()   # dict: {regime, risk_modifier, ...}
        regime = regime_data.get("regime", "unknown")
        if regime in self.NO_TRADE_REGIMES:
            return False, f"regime_no_trade: regime={regime}"
        return True, f"regime={regime}"


class SizingSanityCheck(BaseGovernanceCheck):
    """Balance > MIN_BALANCE (live), SL-Distanz plausibel (0.05% – 15%)."""

    @property
    def name(self) -> str:
        return "sizing_sanity"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        if signal.mode != "shadow":
            balance = get_live_balance()
            if balance <= 0:
                return False, "balance_unknown_no_trade"
            if balance < MIN_BALANCE_USD:
                return False, f"balance_too_low: {balance:.2f} < {MIN_BALANCE_USD}"

        if not signal.entry_price or not signal.stop_loss or signal.entry_price <= 0:
            return False, "missing_entry_or_sl"

        sl_dist = abs(signal.entry_price - signal.stop_loss)
        sl_pct  = sl_dist / signal.entry_price

        if sl_pct < 0.0005 or sl_pct > 0.15:
            return False, f"sl_pct_out_of_range: {sl_pct:.4%}"

        balance_str = f"balance={get_live_balance():.2f} " if signal.mode != "shadow" else ""
        return True, f"{balance_str}sl_pct={sl_pct:.3%}"


class PositionOpenCheck(BaseGovernanceCheck):
    """Asset bereits in offener Position → kein neues Signal.
    Fallback auf DB-Query wenn Key fehlt oder älter als 10 Minuten."""

    @property
    def name(self) -> str:
        return "position_open"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        conn = get_connection()
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key='open_positions'",
        ).fetchone()

        use_fallback = False
        if not row:
            use_fallback = True
        else:
            try:
                updated_at = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
                if age_min > 10:
                    use_fallback = True
                    log(f"[PositionOpenCheck] open_positions veraltet ({age_min:.0f}min), nutze DB-Fallback")
            except Exception:
                use_fallback = True

        if use_fallback:
            rows = conn.execute(
                "SELECT DISTINCT asset FROM trades WHERE exit_ts IS NULL AND mode != 'shadow'",
            ).fetchall()
            conn.close()
            open_assets = [r[0] for r in rows]
            if signal.asset in open_assets:
                return False, f"position_already_open: {signal.asset} (db_fallback)"
            return True, f"open_assets_db={open_assets}"

        conn.close()
        open_positions = json.loads(row[0])
        if signal.asset in open_positions:
            return False, f"position_already_open: {signal.asset}"
        return True, f"open_positions={open_positions}"


class SessionTradedCheck(BaseGovernanceCheck):
    """Bereits ein Trade in dieser Session für dieses Asset heute → Skip."""

    @property
    def name(self) -> str:
        return "session_traded"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        conn = get_connection()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT COUNT(*) FROM trades
               WHERE asset=? AND session=? AND date(entry_ts)=? AND mode=?""",
            (signal.asset, signal.session, today, signal.mode),
        ).fetchone()
        conn.close()

        count = row[0] if row else 0
        if count > 0:
            return False, f"session_already_traded: {signal.asset}/{signal.session} count={count}"
        return True, f"session_trades_today={count}"
