"""
Executor — Einzige Komponente, die Orders an Bitget sendet.

Zweistufige Idempotenz (Locking-Protokoll):
  1. approved → processing  (atomare UPDATE WHERE status='approved')
     → Wenn 0 Rows betroffen: anderer Prozess war schneller → abbrechen
  2. Execution (shadow = nur DB, live = echter API-Call)
  3. processing → executed + Trade INSERT (alles in einer Transaktion)
     → Bei Fehler: bleibt auf 'processing' für manuelles Aufräumen

Shadow-Mode:
  - Order wird in DB simuliert (trades-Eintrag mit order_id='SHADOW-...')
  - BitgetClient wird NICHT instanziiert, kein Netzwerk-Call

Live/Dry-Run-Mode:
  - Position Sizing via _calc_sizing() (RISK_USDT / SL-Distanz)
  - Dynamischer Hebel: falls Notional < MIN_NOTIONAL → Hebel erhöhen
  - Hartes Limit: MAX_LEVERAGE → Trade abgelehnt wenn Hebel nicht reicht
  - set_leverage() API-Call vor place_market_order()
  - Rate-Limit-Schutz: _request_with_retry() im BitgetClient (429 → Backoff)
"""

import json
import math
import time
from datetime import datetime, timezone
from typing import Optional

from config.settings import (
    RISK_USDT, MIN_NOTIONAL, TARGET_NOTIONAL, MAX_LEVERAGE,
    SIZE_DECIMALS, MAX_OPEN_RISK_USDT,
)
from core.db import get_connection, set_state, get_state
from core.models import Signal, Trade
from core.utils import log, now_iso

_CIRCUIT_BREAKER_THRESHOLD = 3   # API-Fehler pro Asset → Soft-Kill


# ── Circuit-Breaker ───────────────────────────────────────────────────────────

def _increment_circuit_breaker(conn_or_none, asset: str) -> None:
    """Erhöht Fehler-Counter und setzt Soft-Kill wenn Schwelle überschritten."""
    key = f"circuit_errors_{asset}"
    current = int(get_state(key, "0"))
    current += 1
    set_state(key, str(current))
    if current >= _CIRCUIT_BREAKER_THRESHOLD:
        kill_key = f"kill_mode_{asset}"
        set_state(kill_key, "soft")
        log(f"[EXECUTOR] ⚡ Circuit-Breaker: {asset} auf Soft-Kill gesetzt "
            f"({current} Fehler ≥ {_CIRCUIT_BREAKER_THRESHOLD})")


def _is_circuit_broken(asset: str) -> bool:
    return get_state(f"kill_mode_{asset}", "ok") != "ok"


# ── clOrdId-Recovery ──────────────────────────────────────────────────────────

# Netzwerkfehler-Marker: Fehlertypen bei denen die Order möglicherweise trotzdem
# angekommen ist. Fachliche Exchange-Rejections (margin, invalid param) sind
# KEINE Netzwerkfehler und lösen keine Recovery aus.
_NETWORK_ERROR_MARKERS = (
    "connectionerror", "timeout", "read timed out", "connect timed out",
    "remotedisconnected", "chunkedencodingerror", "http 502", "http 503",
    "http 504", "http 524", "rate limit",
)


def _is_network_error(error_str: str) -> bool:
    """True wenn der Fehler ein Transport-/Netzwerkproblem ist (kein fachliches Reject)."""
    low = error_str.lower()
    return any(marker in low for marker in _NETWORK_ERROR_MARKERS)


# ── Audit-Log ─────────────────────────────────────────────────────────────────

def _write_audit_log(signal_id: int, cl_ord_id: str,
                     state_from: Optional[str], state_to: str,
                     payload: Optional[dict] = None) -> None:
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO execution_audit_log
               (signal_id, cl_ord_id, state_from, state_to, payload_json)
               VALUES (?,?,?,?,?)""",
            (signal_id, cl_ord_id, state_from, state_to,
             json.dumps(payload) if payload else None),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[EXECUTOR] Audit-Log Fehler (nicht kritisch): {e}")


# ── Position Sizing & Leverage-Berechnung ────────────────────────────────────

def _get_min_size(asset: str) -> float:
    """Liest minTradeNum vom Bitget-Kontrakt (gecacht, kein Auth nötig)."""
    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=True)   # kein Auth für Marktdaten
        info = client.get_contract_info(asset)
        return info.get("min_size", 0.0)
    except Exception as e:
        log(f"[SIZING] get_min_size {asset} fehlgeschlagen: {e}")
        return 0.0


def _calc_sizing(signal: Signal, current_price: float) -> dict | None:
    """
    Berechnet Positionsgröße und nötigen Hebel für ein Signal.

    Logik:
      1. SL-Distanz = |entry - stop_loss|
      2. Position_Size = RISK_USDT / SL_Distanz        (Coins, ohne Hebel)
      3. Notional = Position_Size * entry_price
      4. Wenn Notional >= MIN_NOTIONAL → Hebel = 1 (oder minimal nötig)
      5. Wenn Notional < MIN_NOTIONAL → Hebel = ceil(TARGET_NOTIONAL / Notional)
      6. Wenn Hebel > MAX_LEVERAGE → Trade abgelehnt

    Rückgabe: {"size": float, "leverage": int, "notional": float}
              oder None wenn abgelehnt.
    """
    entry = signal.entry_price or current_price
    sl    = signal.stop_loss

    if not sl or sl <= 0 or not entry or entry <= 0:
        log(f"[SIZING] Signal #{signal.id}: ungültige Entry/SL-Werte ({entry}/{sl})")
        return None

    sl_distance = abs(entry - sl)
    if sl_distance < 1e-8:
        log(f"[SIZING] Signal #{signal.id}: SL-Distanz = 0 → abgelehnt")
        return None

    # Schritt 1 + 2: reine Coin-Menge bei Hebel = 1
    raw_size    = RISK_USDT / sl_distance          # Coins
    notional_1x = raw_size * entry                 # USDT ohne Hebel

    # Schritt 3a: Offenes Risikobudget prüfen (alle nicht-shadow Positionen)
    from core.db import get_connection as _get_conn
    _rconn = _get_conn()
    _open_rows = _rconn.execute(
        """SELECT entry_price, stop_loss, size FROM trades
           WHERE exit_ts IS NULL AND mode != 'shadow'""",
    ).fetchall()
    _rconn.close()
    open_risk = sum(abs(r[0] - r[1]) * r[2] for r in _open_rows)
    if open_risk + RISK_USDT > MAX_OPEN_RISK_USDT:
        log(
            f"[SIZING] ⛔ Signal #{signal.id} {signal.asset}: Trade abgelehnt — "
            f"Risikobudget erschöpft (offen=${open_risk:.2f} + neu=${RISK_USDT:.2f} "
            f"> MAX=${MAX_OPEN_RISK_USDT:.2f})"
        )
        return None

    # Schritt 3b: Balance-Check mit Hebel-Berücksichtigung
    # Isolated-Futures: Margin = Notional / Leverage → auto-Hebel wenn Notional > Balance
    from core.state import get_live_balance
    balance = get_live_balance()

    # Schritt 4: Hebel bestimmen
    if notional_1x >= MIN_NOTIONAL:
        # Großposition: Hebel = 1 wenn Balance reicht, sonst Mindest-Hebel für Margin-Fit
        if balance > 0 and notional_1x > balance * 0.95:
            leverage = math.ceil(notional_1x / (balance * 0.95))
        else:
            leverage = 1
    else:
        # Kleinposition: Hebel inflationiert auf MIN_NOTIONAL (Exchange-Minimum)
        leverage = math.ceil(TARGET_NOTIONAL / notional_1x)

    # Schritt 5: hartes Limit prüfen
    if leverage > MAX_LEVERAGE:
        log(
            f"[SIZING] ⛔ Signal #{signal.id} {signal.asset}: Trade abgelehnt — "
            f"Hebel-Limit überschritten ({leverage}x > {MAX_LEVERAGE}x bei Balance ${balance:.2f}). "
            f"SL-Distanz={sl_distance:.4f}, Notional@1x=${notional_1x:.3f}"
        )
        return None

    # Effektive Positionsgröße:
    # Großposition (notional >= MIN_NOTIONAL): size bleibt raw_size, Leverage = Margin-Reducer
    # Kleinposition (notional < MIN_NOTIONAL):  size ×= leverage um Exchange-Minimum zu erreichen
    if notional_1x >= MIN_NOTIONAL:
        effective_size    = raw_size
    else:
        effective_size    = raw_size * leverage
    effective_notional = effective_size * entry

    s_dec = SIZE_DECIMALS.get(signal.asset, 2)
    effective_size = round(effective_size, s_dec)

    # ── Exchange-Mindestgröße prüfen ──────────────────────────────────────────
    min_size = _get_min_size(signal.asset)
    if min_size > 0 and effective_size < min_size:
        log(
            f"[SIZING] ⛔ Trade Skipped: Calculated position size is below exchange minimum "
            f"({signal.asset}: {effective_size} < {min_size} Kontrakte). "
            f"SL-Distanz={sl_distance:.4f} — SL zu weit für ${RISK_USDT:.2f} Risiko."
        )
        return None

    log(
        f"[SIZING] Signal #{signal.id} {signal.asset}: "
        f"SL-Dist={sl_distance:.4f} | "
        f"Size={effective_size} | "
        f"Leverage={leverage}x | "
        f"Notional=${effective_notional:.2f} | "
        f"Risiko=${RISK_USDT:.2f}"
        + (f" | minSize={min_size}" if min_size > 0 else "")
    )

    return {
        "size":     effective_size,
        "leverage": leverage,
        "notional": effective_notional,
    }


# ── Executor ─────────────────────────────────────────────────────────────────

class Executor:

    def execute(self, signal: Signal) -> Optional[Trade]:
        """
        Führt ein approved Signal aus.
        Gibt Trade zurück oder None wenn übersprungen/fehlgeschlagen.
        """
        conn = get_connection()

        # ── Schritt 1: atomares Locking — approved → processing ───────────────
        cur = conn.execute(
            "UPDATE signals SET status='processing' WHERE id=? AND status='approved'",
            (signal.id,),
        )
        conn.commit()

        if cur.rowcount == 0:
            log(f"[EXECUTOR] Signal #{signal.id}: Status-Lock fehlgeschlagen "
                f"(nicht mehr 'approved') → Skip")
            conn.close()
            return None

        log(f"[EXECUTOR] Signal #{signal.id} {signal.strategy}/{signal.asset} "
            f"{signal.direction.upper()} — Modus: {signal.mode} — Lock (processing)")

        # ── Dedup-Check: bereits heute ausgeführt? ────────────────────────────
        _dup = conn.execute(
            """SELECT id FROM signals
               WHERE strategy=? AND asset=? AND mode=?
                 AND DATE(created_at)=DATE(?)
                 AND status='executed'
                 AND id != ?
               LIMIT 1""",
            (signal.strategy, signal.asset, signal.mode, signal.created_at, signal.id),
        ).fetchone()
        if _dup:
            log(f"[EXECUTOR] Signal #{signal.id} {signal.strategy}/{signal.asset}: "
                f"Dedup — bereits heute ausgeführt (Signal #{_dup[0]}) → rejected")
            conn.execute(
                "UPDATE signals SET status='rejected', "
                "reject_reason='dedup_executor: already_executed_today' WHERE id=?",
                (signal.id,),
            )
            conn.commit()
            conn.close()
            return None

        # ── Schritt 2: Execution je nach Modus ───────────────────────────────
        # Shadow-Signale haben Status 'approved_shadow' und werden nie geladen.
        # Nur 'approved' (dry_run / live) gelangt hierher.
        try:
            if signal.mode in ("dry_run", "live"):
                trade = self._execute_live(signal, dry_run=(signal.mode == "dry_run"))
            else:
                raise ValueError(f"Unbekannter Modus im Executor: {signal.mode} — Shadow-Signale dürfen hier nicht ankommen")
        except Exception as e:
            log(f"[EXECUTOR] Signal #{signal.id}: FEHLER bei Execution — {e}")
            # v6: Status 'error' statt hängenbleiben auf 'processing'
            conn.execute(
                "UPDATE signals SET status='failed', "
                "reject_reason=? WHERE id=?",
                (f"execution_error: {str(e)[:200]}", signal.id),
            )
            conn.commit()
            # Circuit-Breaker: Asset-spezifischer Fehler-Counter
            _increment_circuit_breaker(conn, signal.asset)
            conn.close()
            return None

        if trade is None:
            log(f"[EXECUTOR] Signal #{signal.id}: Execution abgebrochen (kein Trade)")
            conn.execute(
                "UPDATE signals SET status='failed', reject_reason='execution_aborted' "
                "WHERE id=?",
                (signal.id,),
            )
            conn.commit()
            conn.close()
            return None

        # ── Schritt 3: processing → executed + Trade INSERT (eine Transaktion) ─
        ts_now = now_iso()
        cur2 = conn.execute(
            """INSERT INTO trades
               (signal_id, strategy, asset, direction, entry_price, entry_ts,
                size, stop_loss, take_profit_1, take_profit_2, mode, session, context_json,
                signal_price, fill_price, slippage_bps, slippage_measured_at,
                market_impact_check, spread_at_execution_bps, order_type_used,
                ioc_tolerance_used_bps, liquidity_score_at_execution)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal.id, signal.strategy, signal.asset, signal.direction,
             trade.entry_price, ts_now,
             trade.size, trade.stop_loss, trade.take_profit_1, trade.take_profit_2,
             signal.mode, signal.session,
             json.dumps({"order_id": trade.order_id, "mode": signal.mode,
                         "leverage": getattr(trade, "_leverage", None),
                         "cl_ord_id": getattr(trade, "_cl_ord_id", None)}),
             getattr(trade, "_signal_price", None),
             getattr(trade, "_fill_price", None),
             getattr(trade, "_slippage_bps", None),
             ts_now if getattr(trade, "_slippage_bps", None) is not None else None,
             getattr(trade, "_market_impact_check", None),
             getattr(trade, "_spread_at_execution_bps", None),
             getattr(trade, "_order_type_used", None),
             getattr(trade, "_ioc_tolerance_used_bps", None),
             getattr(trade, "_liquidity_score", None)),
        )
        trade.id = cur2.lastrowid

        conn.execute(
            "UPDATE signals SET status='executed', execution_ts=?, order_id=? WHERE id=?",
            (ts_now, trade.order_id, signal.id),
        )
        conn.commit()
        conn.close()

        log(f"[EXECUTOR] Signal #{signal.id} → Trade #{trade.id} geschrieben "
            f"(order_id={trade.order_id}, entry={trade.entry_price}, "
            f"slippage={getattr(trade, '_slippage_bps', 0):.1f}bps)")
        return trade

    # ── Live/Dry-Run-Execution ────────────────────────────────────────────────

    def _execute_live(self, signal: Signal, dry_run: bool) -> Optional[Trade]:
        """
        Echter API-Call mit dynamischer Positionsgröße und Hebel-Berechnung.

          0. Circuit-Breaker prüfen
          1. Deterministische clOrdId VOR API-Call generieren (Idempotenz)
          2. Aktuellen Preis holen (signal_price für Slippage-Tracking)
          3. _calc_sizing(): Coins, Hebel, Notional
          4. set_leverage() API-Call
          5. place_market_order() mit clOrdId
          6. Slippage messen + in Audit-Log schreiben
        """
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=dry_run)

        # Schritt 0: Circuit-Breaker
        if _is_circuit_broken(signal.asset):
            log(f"[EXECUTOR] Circuit-Breaker aktiv für {signal.asset} → abbrechen")
            return None

        if not client.is_ready and not dry_run:
            log(f"[EXECUTOR] BitgetClient nicht bereit (fehlende Credentials) → abbrechen")
            return None

        # Schritt 1: clOrdId deterministisch VOR API-Call
        cl_ord_id = f"APEX-V2-SIG-{signal.id}-E1"
        _write_audit_log(signal.id, cl_ord_id, None, "created")

        # Schritt 2: aktuellen Preis für Notional-Schätzung + Slippage-Baseline
        current_price = client.get_price(signal.asset)
        signal_price  = current_price   # vor Order = Signal-Preis
        if current_price <= 0 and not dry_run:
            log(f"[EXECUTOR] Kein gültiger Preis für {signal.asset} → abbrechen")
            return None
        if current_price <= 0:
            current_price = signal.entry_price or 1.0

        # Schritt 3: Positionsgröße und Hebel berechnen
        sizing = _calc_sizing(signal, current_price)
        if sizing is None:
            return None

        size     = sizing["size"]
        leverage = sizing["leverage"]

        # Daily-DD Half-Size
        _conn = get_connection()
        _row = _conn.execute(
            "SELECT reason FROM governance_log WHERE signal_id=? ORDER BY ts DESC LIMIT 1",
            (signal.id,),
        ).fetchone()
        _conn.close()
        if _row and _row[0] and "HALF_SIZE" in _row[0]:
            s_dec = SIZE_DECIMALS.get(signal.asset, 2)
            size  = round(size * 0.5, s_dec)
            log(f"[EXECUTOR] Half-size wegen Daily-DD -1.5R: neue Größe {size}")
        if _row and _row[0] and "REGIME_HALF" in _row[0]:
            s_dec = SIZE_DECIMALS.get(signal.asset, 2)
            size  = round(size * 0.5, s_dec)
            regime_tag = "SIDEWAYS" if "SIDEWAYS" in _row[0] else "HIGH_VOL"
            log(f"[EXECUTOR] Regime-Half-Size: {size} (Regime: {regime_tag})")

        # Schritt 4: Hebel setzen
        hold_side = "long" if signal.direction == "long" else "short"
        ok = client.set_leverage(signal.asset, leverage, hold_side=hold_side)
        if not ok and not dry_run:
            log(f"[EXECUTOR] set_leverage {signal.asset}×{leverage} fehlgeschlagen → abbrechen")
            return None

        # Schritt 4b: Market-Impact-Guard (v6 Phase 7)
        from execution.market_impact_guard import evaluate as _mig_evaluate
        _mig = _mig_evaluate(
            asset=signal.asset,
            order_size_usd=sizing["notional"],
            client=client if not dry_run else None,
        )

        log(f"[EXECUTOR] {'DRY' if dry_run else 'LIVE'}: {signal.asset} "
            f"{signal.direction.upper()} | size={size} | leverage={leverage}x | "
            f"notional=${sizing['notional']:.2f} | clOrdId={cl_ord_id} | "
            f"order_type={_mig.order_type} tol={_mig.ioc_tolerance_bps:.1f}bps")

        # Schritt 5: Market Order mit clOrdId
        _write_audit_log(signal.id, cl_ord_id, None, "sent")
        result = client.place_market_order(
            coin=signal.asset,
            is_buy=(signal.direction == "long"),
            size=size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
            client_order_id=cl_ord_id,
        )

        if not result.success:
            # P2.1 — clOrdId-Recovery: nur bei Netzwerk-/Transportfehlern prüfen,
            # ob Order trotz Exception bei Bitget angekommen ist.
            if _is_network_error(result.error or ""):
                log(f"[EXECUTOR] Netzwerkfehler erkannt — Recovery-Query für {cl_ord_id}")
                recovered = client.get_order_by_client_id(signal.asset, cl_ord_id)
                if recovered.success:
                    # Order existiert bei Bitget — als gefüllt behandeln
                    log(f"[EXECUTOR] clOrdId-Recovery erfolgreich: {cl_ord_id} "
                        f"order_id={recovered.order_id}")
                    _write_audit_log(signal.id, cl_ord_id, "sent", "recovered",
                                     payload={"order_id": recovered.order_id,
                                              "original_error": result.error})
                    result = recovered
                elif not recovered.error or not recovered.error.startswith("query_failed:"):
                    # Query erfolgreich, Order nicht gefunden → einmaliger Retry mit -R1
                    cl_ord_id_r1 = cl_ord_id + "-R1"
                    log(f"[EXECUTOR] Order nicht gefunden → Retry mit {cl_ord_id_r1}")
                    _write_audit_log(signal.id, cl_ord_id, "sent", "retry_r1",
                                     payload={"reason": result.error, "retry_id": cl_ord_id_r1})
                    result = client.place_market_order(
                        coin=signal.asset,
                        is_buy=(signal.direction == "long"),
                        size=size,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit_1,
                        client_order_id=cl_ord_id_r1,
                    )
                    if result.success:
                        cl_ord_id = cl_ord_id_r1
                else:
                    # Recovery-Query selbst fehlgeschlagen → Originalfehler beibehalten
                    log(f"[EXECUTOR] Recovery-Query fehlgeschlagen ({recovered.error}) "
                        f"— Originalfehler behalten")

            if not result.success:
                log(f"[EXECUTOR] place_market_order fehlgeschlagen: {result.error}")
                _write_audit_log(signal.id, cl_ord_id, "sent", "error",
                                 payload={"error": result.error})
                _increment_circuit_breaker(None, signal.asset)
                return None

        _write_audit_log(signal.id, cl_ord_id, "sent", "acked",
                         payload={"order_id": result.order_id})

        entry_price = result.avg_price if result.avg_price > 0 else signal.entry_price
        order_id    = result.order_id or cl_ord_id

        # Schritt 6: Slippage messen
        fill_price   = entry_price
        slippage_bps = 0.0
        if signal_price and signal_price > 0 and fill_price and fill_price > 0:
            slippage_bps = abs(fill_price - signal_price) / signal_price * 10000

        # TP2: separater TPSL-Order (P2.2 — atomare Variante nicht möglich)
        # Bitget place-order (v2) unterstützt nur einen Preset-TP-Slot (presetStopSurplusPrice).
        # Ein zweiter atomarer TP existiert in der API nicht → TP2 wird als separater
        # place-tpsl-order platziert. Scheitert er nach Retry: Hard-Kill-Fallback (verbindlich).
        if signal.take_profit_2 and signal.take_profit_2 > 0:
            filled = result.filled_size or size
            tp2_result = client.place_take_profit(
                coin=signal.asset,
                trigger_price=signal.take_profit_2,
                size=filled,
                hold_side=hold_side,
            )
            if not tp2_result.success:
                # Einmaliger Retry nach 2s (transiente API-Fehler)
                time.sleep(2.0)
                tp2_result = client.place_take_profit(
                    coin=signal.asset,
                    trigger_price=signal.take_profit_2,
                    size=filled,
                    hold_side=hold_side,
                )
            if tp2_result.success:
                log(f"[EXECUTOR] TP2 platziert: {signal.asset} @ {signal.take_profit_2}")
            else:
                # TP2 nach Retry nicht setzbar — Position schließen + Hard-Kill auf Asset
                log(f"[EXECUTOR] KRITISCH: TP2 fehlgeschlagen nach Retry "
                    f"({signal.asset}): {tp2_result.error} — schließe Position")
                is_close_buy = (signal.direction == "short")
                client.place_market_order(
                    coin=signal.asset,
                    is_buy=is_close_buy,
                    size=filled,
                    reduce_only=True,
                )
                from governance.kill_switch import set_kill_mode
                set_kill_mode("hard", reason=f"TP2-Platzierung fehlgeschlagen: {tp2_result.error}",
                              asset=signal.asset)
                _write_audit_log(signal.id, cl_ord_id, "acked", "error",
                                 payload={"tp2_error": tp2_result.error, "action": "position_closed"})
                return None

        t = Trade(
            signal_id=signal.id,
            strategy=signal.strategy, asset=signal.asset, direction=signal.direction,
            entry_price=entry_price, size=result.filled_size or size,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2,
            mode=signal.mode, order_id=order_id,
        )
        t._leverage              = leverage
        t._signal_price          = signal_price
        t._fill_price            = fill_price
        t._slippage_bps          = slippage_bps
        t._cl_ord_id             = cl_ord_id
        t._market_impact_check   = _mig.market_impact_check
        t._spread_at_execution_bps = _mig.spread_at_snapshot_bps
        t._order_type_used       = _mig.order_type
        t._ioc_tolerance_used_bps = _mig.ioc_tolerance_bps
        t._liquidity_score       = _mig.liquidity_score
        return t
