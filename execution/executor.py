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
from core.db import get_connection
from core.models import Signal, Trade
from core.utils import log, now_iso


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

    # Schritt 3b: Balance-Check — Margin-Bedarf = notional_1x (unabhängig von Leverage)
    # Bei Isolated-Futures gilt: Margin = Size × Price / Leverage = notional_1x (konstant)
    from core.state import get_live_balance
    balance = get_live_balance()
    if balance > 0 and notional_1x > balance * 0.95:
        log(
            f"[SIZING] ⛔ Signal #{signal.id} {signal.asset}: Trade abgelehnt — "
            f"Margin-Bedarf ${notional_1x:.2f} > verfügbare Balance ${balance:.2f} "
            f"(SL-Distanz={sl_distance:.4f} zu eng für RISK ${RISK_USDT:.2f})"
        )
        return None

    # Schritt 4: Hebel bestimmen
    if notional_1x >= MIN_NOTIONAL:
        leverage = 1
    else:
        # Minimaler Hebel um MIN_NOTIONAL zu erreichen
        leverage = math.ceil(TARGET_NOTIONAL / notional_1x)

    # Schritt 5: hartes Limit prüfen
    if leverage > MAX_LEVERAGE:
        log(
            f"[SIZING] ⛔ Signal #{signal.id} {signal.asset}: Trade abgelehnt — "
            f"Hebel-Limit überschritten ({leverage}x > {MAX_LEVERAGE}x). "
            f"SL-Distanz={sl_distance:.4f}, Notional@1x=${notional_1x:.3f}"
        )
        return None

    # Effektive Positionsgröße mit Hebel (Coins)
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
            log(f"[EXECUTOR] Signal #{signal.id} bleibt auf 'processing' "
                f"(manuelles Aufräumen erforderlich)")
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
                size, stop_loss, take_profit_1, take_profit_2, mode, session, context_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal.id, signal.strategy, signal.asset, signal.direction,
             trade.entry_price, ts_now,
             trade.size, trade.stop_loss, trade.take_profit_1, trade.take_profit_2,
             signal.mode, signal.session,
             json.dumps({"order_id": trade.order_id, "mode": signal.mode,
                         "leverage": getattr(trade, "_leverage", None)})),
        )
        trade.id = cur2.lastrowid

        conn.execute(
            "UPDATE signals SET status='executed', execution_ts=?, order_id=? WHERE id=?",
            (ts_now, trade.order_id, signal.id),
        )
        conn.commit()
        conn.close()

        log(f"[EXECUTOR] Signal #{signal.id} → Trade #{trade.id} geschrieben "
            f"(order_id={trade.order_id}, entry={trade.entry_price})")
        return trade

    # ── Live/Dry-Run-Execution ────────────────────────────────────────────────

    def _execute_live(self, signal: Signal, dry_run: bool) -> Optional[Trade]:
        """
        Echter API-Call mit dynamischer Positionsgröße und Hebel-Berechnung.

          1. Aktuellen Preis holen (für Notional-Berechnung)
          2. _calc_sizing(): Coins, Hebel, Notional
          3. set_leverage() API-Call (beide Seiten bei isolated)
          4. place_market_order() mit berechneter Size
        """
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=dry_run)

        if not client.is_ready and not dry_run:
            log(f"[EXECUTOR] BitgetClient nicht bereit (fehlende Credentials) → abbrechen")
            return None

        # Schritt 1: aktuellen Preis für Notional-Schätzung
        current_price = client.get_price(signal.asset)
        if current_price <= 0 and not dry_run:
            log(f"[EXECUTOR] Kein gültiger Preis für {signal.asset} → abbrechen")
            return None
        if current_price <= 0:
            current_price = signal.entry_price or 1.0   # Dry-Run Fallback

        # Schritt 2: Positionsgröße und Hebel berechnen
        sizing = _calc_sizing(signal, current_price)
        if sizing is None:
            # _calc_sizing hat bereits den Grund geloggt (Hebel-Limit etc.)
            return None

        size     = sizing["size"]
        leverage = sizing["leverage"]

        # Schritt 3: Hebel setzen (beide Seiten, isolated margin)
        hold_side = "long" if signal.direction == "long" else "short"
        for side in (hold_side,):   # nur aktive Seite setzen
            ok = client.set_leverage(signal.asset, leverage, hold_side=side)
            if not ok and not dry_run:
                log(f"[EXECUTOR] set_leverage {signal.asset}×{leverage} fehlgeschlagen → abbrechen")
                return None

        log(f"[EXECUTOR] {'DRY' if dry_run else 'LIVE'}: {signal.asset} "
            f"{signal.direction.upper()} | "
            f"size={size} | leverage={leverage}x | "
            f"notional=${sizing['notional']:.2f}")

        # Schritt 4: Market Order platzieren
        result = client.place_market_order(
            coin=signal.asset,
            is_buy=(signal.direction == "long"),
            size=size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
        )

        if not result.success:
            log(f"[EXECUTOR] place_market_order fehlgeschlagen: {result.error}")
            return None

        entry_price = result.avg_price if result.avg_price > 0 else signal.entry_price
        order_id    = result.order_id or f"{'DRY' if dry_run else 'LIVE'}-{int(time.time())}"

        # TP2: separater TPSL-Order nach Entry (TP1 war preset im Market-Order)
        if signal.take_profit_2 and signal.take_profit_2 > 0:
            tp2_result = client.place_take_profit(
                coin=signal.asset,
                trigger_price=signal.take_profit_2,
                size=result.filled_size or size,
                hold_side=hold_side,
            )
            if tp2_result.success:
                log(f"[EXECUTOR] TP2 platziert: {signal.asset} @ {signal.take_profit_2}")
            else:
                log(f"[EXECUTOR] TP2 fehlgeschlagen (ignoriert): {tp2_result.error}")

        t = Trade(
            signal_id=signal.id,
            strategy=signal.strategy, asset=signal.asset, direction=signal.direction,
            entry_price=entry_price, size=result.filled_size or size,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2,
            mode=signal.mode, order_id=order_id,
        )
        t._leverage = leverage   # für context_json
        return t
