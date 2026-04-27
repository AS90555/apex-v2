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

Live-Mode:
  - BitgetClient.place_market_order() + SL/TP-Orders
  - Erst nach Erfolg → Signal 'executed', Trade geschrieben
"""

import json
import time
from datetime import datetime, timezone
from typing import Optional

from core.db import get_connection
from core.models import Signal, Trade
from core.utils import log, now_iso


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
            # anderer Prozess oder falscher Zustand → überspringen
            log(f"[EXECUTOR] Signal #{signal.id}: Status-Lock fehlgeschlagen (nicht mehr 'approved') → Skip")
            conn.close()
            return None

        log(f"[EXECUTOR] Signal #{signal.id} {signal.strategy}/{signal.asset} {signal.direction.upper()} "
            f"— Modus: {signal.mode} — Lock gesetzt (processing)")

        # ── Schritt 2: Execution je nach Modus ───────────────────────────────
        try:
            if signal.mode == "shadow":
                trade = self._execute_shadow(signal)
            elif signal.mode in ("dry_run", "live"):
                trade = self._execute_live(signal, dry_run=(signal.mode == "dry_run"))
            else:
                raise ValueError(f"Unbekannter Modus: {signal.mode}")
        except Exception as e:
            log(f"[EXECUTOR] Signal #{signal.id}: FEHLER bei Execution — {e}")
            log(f"[EXECUTOR] Signal #{signal.id} bleibt auf 'processing' (manuelles Aufräumen erforderlich)")
            conn.close()
            return None

        if trade is None:
            log(f"[EXECUTOR] Signal #{signal.id}: Execution abgebrochen (kein Trade)")
            conn.execute(
                "UPDATE signals SET status='failed', reject_reason='execution_aborted' WHERE id=?",
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
             json.dumps({"order_id": trade.order_id, "mode": signal.mode})),
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

    # ── Shadow-Execution ──────────────────────────────────────────────────────

    def _execute_shadow(self, signal: Signal) -> Trade:
        """Simuliert die Order rein in DB. Kein Netzwerk-Call."""
        order_id = f"SHADOW-{signal.strategy.upper()}-{int(time.time())}"
        log(f"[EXECUTOR] SHADOW: {signal.asset} {signal.direction.upper()} "
            f"@ {signal.entry_price} SL={signal.stop_loss} TP2={signal.take_profit_2} "
            f"size={signal.size} → {order_id}")
        return Trade(
            signal_id=signal.id,
            strategy=signal.strategy, asset=signal.asset, direction=signal.direction,
            entry_price=signal.entry_price, size=signal.size,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2,
            mode=signal.mode, order_id=order_id,
        )

    # ── Live/Dry-Run-Execution ────────────────────────────────────────────────

    def _execute_live(self, signal: Signal, dry_run: bool) -> Optional[Trade]:
        """Echter API-Call (dry_run=True → BitgetClient simuliert intern)."""
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=dry_run)

        if not client.is_ready() and not dry_run:
            log(f"[EXECUTOR] BitgetClient nicht bereit (fehlende Credentials) → abbrechen")
            return None

        result = client.place_market_order(
            coin=signal.asset,
            is_buy=(signal.direction == "long"),
            size=signal.size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_2,
        )

        if not result.success:
            log(f"[EXECUTOR] place_market_order fehlgeschlagen: {result.error}")
            return None

        entry_price = result.avg_price if result.avg_price > 0 else signal.entry_price
        order_id    = result.order_id or f"{'DRY' if dry_run else 'LIVE'}-{int(time.time())}"

        log(f"[EXECUTOR] {'DRY_RUN' if dry_run else 'LIVE'}: {signal.asset} {signal.direction.upper()} "
            f"@ {entry_price:.4f} | order_id={order_id}")

        return Trade(
            signal_id=signal.id,
            strategy=signal.strategy, asset=signal.asset, direction=signal.direction,
            entry_price=entry_price, size=result.filled_size or signal.size,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2,
            mode=signal.mode, order_id=order_id,
        )
