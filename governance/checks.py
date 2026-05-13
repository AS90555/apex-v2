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
  8. check_btc_cross_asset    — Soft-Filter: BTC-Regime als Markt-Kontext (Phase 1: nur logging)
  9. stale_candle             — Kerze älter als STALE_CANDLE_TOLERANCE_SECONDS → rejected (v6)
 10. funding_rate             — Hohe Funding-Rate gegen Signal-Richtung → warn/block (v6)
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
    DAILY_DD_HALF_R, DAILY_DD_KILL_R,
    SIGNAL_EXPIRY_MINUTES,
    STALE_CANDLE_TOLERANCE_SECONDS,
    FUNDING_RATE_WARN_THRESHOLD,
    FUNDING_RATE_BLOCK_THRESHOLD,
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
    """Tages-PnL-Breaker: zweistufig.
    pnl_r <= DAILY_DD_KILL_R (-2.0R)  → kein Trade.
    pnl_r <= DAILY_DD_HALF_R (-1.5R)  → Trade erlaubt, aber halbe Größe (HALF_SIZE-Flag).
    """

    @property
    def name(self) -> str:
        return "daily_dd"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        daily = get_daily_pnl()   # dict: {date, pnl_r, pnl_usd, trades}
        pnl_r = daily.get("pnl_r", 0.0)
        if pnl_r <= DAILY_DD_KILL_R:
            return False, f"daily_dd_kill: pnl_r={pnl_r:.2f} <= {DAILY_DD_KILL_R}"
        if pnl_r <= DAILY_DD_HALF_R:
            return True, f"daily_dd_half: pnl_r={pnl_r:.2f} — HALF_SIZE"
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
        if signal.session is None:
            return True, "session_trades_today=skip(no_session)"
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


class CrossAssetRegimeCheck(BaseGovernanceCheck):
    """
    Phase 1 (Soft-Filter): BTC als Markt-Führungsindikator.

    Prüft ob das Signal-Asset gegen das BTC-Regime handelt:
      - Long-Signal + BTC TREND_DOWN → btc_contra=True
      - Short-Signal + BTC TREND_UP  → btc_contra=True
      - BTC SIDEWAYS oder Asset=BTC  → neutral, immer approved

    Phase 1: blockiert NIE — gibt immer True zurück, schreibt btc_contra
    in den reason-String für spätere Auswertung im governance_log.

    Aktivierung als Hard-Filter: CROSS_ASSET_HARD_REJECT = True setzen
    sobald Datenbasis aus Phase 1 vorliegt (empfohlen: nach 30+ Trades).
    """

    CROSS_ASSET_HARD_REJECT = False   # Phase 1: nur loggen
    REGIME_MAX_AGE_MIN      = 15      # BTC-Regime älter als 15 Min → skip

    @property
    def name(self) -> str:
        return "btc_cross_asset"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        # BTC-Signale prüfen sich nicht selbst
        if signal.asset == "BTC":
            return True, "btc_cross_asset=skip(self)"

        # BTC-Regime aus system_state lesen
        conn = get_connection()
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key='regime_BTC'",
        ).fetchone()
        conn.close()

        if not row:
            return True, "btc_cross_asset=skip(no_btc_regime)"

        # Alter prüfen — veraltetes Regime nicht als Signal werten
        try:
            ts = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            if age_min > self.REGIME_MAX_AGE_MIN:
                return True, f"btc_cross_asset=skip(regime_stale_{age_min:.0f}min)"
        except Exception:
            return True, "btc_cross_asset=skip(ts_parse_error)"

        btc_regime = row[0]

        # Nur bei klarem BTC-Trend prüfen — SIDEWAYS/UNKNOWN ist neutral
        if btc_regime not in ("TREND_UP", "TREND_DOWN"):
            return True, f"btc_cross_asset=neutral(btc={btc_regime})"

        direction  = signal.direction.lower()
        contra     = (
            (direction == "long"  and btc_regime == "TREND_DOWN") or
            (direction == "short" and btc_regime == "TREND_UP")
        )

        if contra:
            reason = f"btc_cross_asset=CONTRA(btc={btc_regime} signal={direction})"
            log(f"[CrossAsset] {signal.asset} {direction.upper()} gegen BTC {btc_regime} "
                f"— {'BLOCKED' if self.CROSS_ASSET_HARD_REJECT else 'soft_flag'}")
            if self.CROSS_ASSET_HARD_REJECT:
                return False, reason
            return True, reason   # Phase 1: durchlassen, aber markieren

        return True, f"btc_cross_asset=aligned(btc={btc_regime} signal={direction})"


class HMMRegimeCheck(BaseGovernanceCheck):
    """
    3-State HMM Regime-Filter (P-02).

    Soft-Warning Modus bis HMM Live-Track-Record nach 30 Trades
    validiert ist. Dann auf Hard-Block umstellen via:
    STRATEGY_ALLOWED_REGIMES + return False statt True bei HMM_WARN.
    Kein Modell vorhanden → durchlassen (fail-open).
    """

    @property
    def name(self) -> str:
        return "hmm_regime"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        from config.settings import STRATEGY_ALLOWED_REGIMES
        allowed = STRATEGY_ALLOWED_REGIMES.get(
            signal.strategy, ["TREND", "SIDEWAYS", "HIGH_VOL"]
        )
        try:
            from research.train_hmm import get_current_regime
            conn = get_connection()
            regime = get_current_regime(signal.asset, conn)
            conn.close()
        except FileNotFoundError:
            return True, f"hmm_regime=skip(kein_modell_{signal.asset})"
        except Exception as exc:
            log(f"[HMMRegimeCheck] Fehler bei {signal.asset}: {exc}")
            return True, "hmm_regime=skip(error)"

        if regime not in allowed:
            return True, f"HMM_WARN: regime={regime} not in {allowed}"

        if regime == "SIDEWAYS" and signal.strategy in STRATEGY_ALLOWED_REGIMES:
            return True, f"HMM_WARN: regime=SIDEWAYS — REGIME_HALF"
        if regime == "HIGH_VOL":
            return True, f"hmm_regime=HIGH_VOL — REGIME_HALF"
        return True, f"hmm_regime={regime} OK"


class StaleCandleCheck(BaseGovernanceCheck):
    """
    Prüft ob die jüngste Kerze für das Asset zu alt ist (v6 Phase 6).
    Stale Marktdaten → Signal ist auf falscher Basis erzeugt worden.
    """

    @property
    def name(self) -> str:
        return "stale_candle"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        from datetime import datetime, timezone
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT ts FROM candles
                   WHERE asset=? ORDER BY ts DESC LIMIT 1""",
                (signal.asset,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return True, f"stale_candle=skip(keine_candle_{signal.asset})"

        try:
            ts_ms  = int(row["ts"])
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            age_s  = (now_ms - ts_ms) / 1000
        except Exception:
            return True, "stale_candle=skip(parse_error)"

        if age_s > STALE_CANDLE_TOLERANCE_SECONDS:
            return False, (
                f"stale_market_data: {signal.asset} letzte Kerze "
                f"{age_s:.0f}s alt > {STALE_CANDLE_TOLERANCE_SECONDS}s"
            )
        return True, f"stale_candle=ok({age_s:.0f}s)"


class FundingRateCheck(BaseGovernanceCheck):
    """
    Blockiert/warnt bei hoher Funding-Rate gegen Signal-Richtung (v6 Phase 6).

    |rate| > WARN  → durchlassen, aber in reason markieren
    |rate| > BLOCK und rate-Vorzeichen gegen Signal → rejected
    """

    @property
    def name(self) -> str:
        return "funding_rate"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT funding_rate FROM funding_rates
                   WHERE asset=? ORDER BY funding_time DESC LIMIT 1""",
                (signal.asset,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return True, f"funding_rate=skip(keine_daten_{signal.asset})"

        rate = float(row["funding_rate"])
        direction = signal.direction  # 'long' oder 'short'

        # Funding positiv → Long zahlt → nachteilig für Longs, vorteilhaft für Shorts
        funding_hurts_long  = rate > 0 and direction == "long"
        funding_hurts_short = rate < 0 and direction == "short"
        funding_against = funding_hurts_long or funding_hurts_short

        abs_rate = abs(rate)
        if abs_rate > FUNDING_RATE_BLOCK_THRESHOLD and funding_against:
            return False, (
                f"funding_rate: rate={rate:.5f} gegen {direction} "
                f"> block_threshold={FUNDING_RATE_BLOCK_THRESHOLD:.4f}"
            )
        if abs_rate > FUNDING_RATE_WARN_THRESHOLD:
            return True, (
                f"FUNDING_WARN: rate={rate:.5f} direction={direction} "
                f"(warn_threshold={FUNDING_RATE_WARN_THRESHOLD:.4f})"
            )
        return True, f"funding_rate=ok({rate:.5f})"
