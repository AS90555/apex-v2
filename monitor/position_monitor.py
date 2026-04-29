"""
Position-Monitor — überwacht offene Trades, setzt Break-Even-SL, erkennt Exits.

Logik:
  1. Lädt alle offenen Trades (exit_ts IS NULL) aus der trades-Tabelle
  2. Holt aktuelle Positionen von Bitget (shadow-Modus: überspringen)
  3. Erkennt Exits: Trade in DB offen, aber keine Position mehr auf Bitget → Exit schreiben
  4. Break-Even-SL: wenn unrealized_pnl >= 1R → be_applied=1 setzen (nur loggen, kein API-Call)
  5. Aktualisiert system_state['open_positions'] für Governance
"""

import json
from datetime import datetime, timezone
from typing import Optional

from core.db import get_connection, set_state
from core.state import get_daily_pnl, set_daily_pnl, get_hwm, set_hwm
from core.utils import log, now_iso


def _load_open_trades(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, signal_id, strategy, asset, direction, entry_price,
                  size, stop_loss, take_profit_1, take_profit_2, be_applied, mode, session
           FROM trades WHERE exit_ts IS NULL""",
    ).fetchall()
    return [
        {
            "id": r[0], "signal_id": r[1], "strategy": r[2], "asset": r[3],
            "direction": r[4], "entry_price": r[5], "size": r[6],
            "stop_loss": r[7], "take_profit_1": r[8], "take_profit_2": r[9],
            "be_applied": r[10], "mode": r[11], "session": r[12],
        }
        for r in rows
    ]


def _get_live_positions(mode: str) -> dict[str, object]:
    """Gibt dict {asset: Position} zurück. Leer im shadow-Modus."""
    if mode == "shadow":
        return {}
    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=(mode == "dry_run"))
        positions = client.get_positions()
        return {pos.coin: pos for pos in positions}
    except Exception as e:
        log(f"[MONITOR] get_positions Fehler: {e}")
        return {}


def _mark_exit(conn, trade: dict, exit_price: float, reason: str, pnl_usd: float):
    sl_dist = abs(trade["entry_price"] - trade["stop_loss"])
    pnl_r   = (pnl_usd / (sl_dist * trade["size"])) if sl_dist > 0 and trade["size"] > 0 else 0.0
    ts_now  = now_iso()

    conn.execute(
        """UPDATE trades SET exit_price=?, exit_ts=?, exit_reason=?, pnl_usd=?, pnl_r=?
           WHERE id=?""",
        (round(exit_price, 6), ts_now, reason, round(pnl_usd, 4), round(pnl_r, 4), trade["id"]),
    )
    log(f"[MONITOR] Exit erkannt: Trade #{trade['id']} {trade['asset']} "
        f"pnl={pnl_usd:+.2f}$ ({pnl_r:+.2f}R) reason={reason}")

    # Daily-PnL akkumulieren
    daily = get_daily_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if daily.get("date") != today:
        daily = {"date": today, "pnl_r": 0.0, "pnl_usd": 0.0, "trades": 0}
    set_daily_pnl(
        date=today,
        pnl_r=daily["pnl_r"] + pnl_r,
        pnl_usd=daily["pnl_usd"] + pnl_usd,
        trades=daily["trades"] + 1,
    )


def _check_break_even(conn, trade: dict, current_price: float):
    if trade["be_applied"]:
        return
    sl_dist = abs(trade["entry_price"] - trade["stop_loss"])
    if sl_dist <= 0:
        return

    if trade["direction"] == "long":
        pnl_usd = (current_price - trade["entry_price"]) * trade["size"]
    else:
        pnl_usd = (trade["entry_price"] - current_price) * trade["size"]

    pnl_r = pnl_usd / (sl_dist * trade["size"]) if trade["size"] > 0 else 0.0

    if pnl_r >= 1.0:
        # 0.05% Puffer gegen Spike-Out beim BE-SL
        if trade["direction"] == "long":
            new_sl = trade["entry_price"] * 0.9995
        else:
            new_sl = trade["entry_price"] * 1.0005

        # Shadow + Dry-Run: nur DB-Flag, kein API-Call (keine reale Position)
        if trade["mode"] in ("shadow", "dry_run"):
            conn.execute("UPDATE trades SET be_applied=1 WHERE id=?", (trade["id"],))
            log(f"[MONITOR] Break-Even ({trade['mode']} simuliert): Trade #{trade['id']} {trade['asset']} "
                f"pnl_r={pnl_r:.2f}R → be_applied=1 (kein API-Call)")
            return

        # Live: erst API-Call, dann DB-Flag bei Erfolg
        try:
            from execution.bitget_client import BitgetClient
            hold_side = "long" if trade["direction"] == "long" else "short"
            client = BitgetClient(dry_run=False)
            ok = client.modify_sl(
                coin=trade["asset"],
                new_sl=round(new_sl, 6),
                size=trade["size"],
                hold_side=hold_side,
            )
            if ok:
                conn.execute("UPDATE trades SET be_applied=1 WHERE id=?", (trade["id"],))
                log(f"[MONITOR] Break-Even: Trade #{trade['id']} {trade['asset']} "
                    f"pnl_r={pnl_r:.2f}R → SL auf {new_sl:.4f} verschoben, be_applied=1")
            else:
                log(f"[MONITOR] Break-Even API-Call fehlgeschlagen: Trade #{trade['id']} "
                    f"{trade['asset']} — be_applied bleibt 0, nächster Zyklus versucht es erneut")
        except Exception as e:
            log(f"[MONITOR] Break-Even API-Call Fehler: Trade #{trade['id']} {trade['asset']}: {e} "
                f"— be_applied bleibt 0")


def _update_open_positions_state(conn, open_assets: list[str]):
    ts = now_iso()
    val = json.dumps(open_assets)
    conn.execute(
        """INSERT INTO system_state(key, value, updated_at) VALUES('open_positions', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (val, ts),
    )


def _refresh_balance_and_hwm() -> float:
    """Holt aktuelle Balance von Bitget, schreibt balance_usdt + aktualisiert HWM.
    Gibt Balance zurück (0.0 bei Fehler — überschreibt dann NICHT den State)."""
    try:
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=False)
        if not client.is_ready:
            return 0.0
        balance = client.get_balance()
        if balance > 0:
            set_state("balance_usdt", str(balance))
            hwm = get_hwm()
            if hwm <= 0:
                set_hwm(balance)
                log(f"[MONITOR] HWM initialisiert: {balance:.2f} USDT")
            elif balance > hwm:
                set_hwm(balance)
                log(f"[MONITOR] HWM aktualisiert: {hwm:.2f} → {balance:.2f} USDT")
        return balance
    except Exception as e:
        log(f"[MONITOR] Balance-Abruf fehlgeschlagen: {e}")
        return 0.0


def run_position_monitor() -> dict:
    """
    Hauptroutine. Gibt Stats-Dict zurück: {checked, exits, be_applied, open}.
    """
    # Balance einmalig holen und in system_state schreiben (für Governance)
    balance = _refresh_balance_and_hwm()

    conn = get_connection()
    open_trades = _load_open_trades(conn)
    log(f"[MONITOR] {len(open_trades)} offene Trade(s) in DB")

    stats = {"checked": len(open_trades), "exits": 0, "be_applied_new": 0, "open": 0}

    if not open_trades:
        _update_open_positions_state(conn, [])
        conn.commit()
        conn.close()
        return stats

    # Positionen je Modus holen (shadow → leer, live → Bitget)
    # Gruppiere nach Modus
    live_modes = {t["mode"] for t in open_trades if t["mode"] != "shadow"}
    live_positions: dict[str, object] = {}
    for mode in live_modes:
        live_positions.update(_get_live_positions(mode))

    still_open_assets: list[str] = []

    for trade in open_trades:
        asset = trade["asset"]

        if trade["mode"] == "shadow":
            # Aktuellen Marktpreis holen (kein Auth), SL/TP simulieren
            current_price = 0.0
            try:
                from execution.bitget_client import BitgetClient
                current_price = BitgetClient(dry_run=True).get_price(asset)
            except Exception:
                pass

            if current_price > 0:
                sl  = trade["stop_loss"]
                tp1 = trade["take_profit_1"]
                d   = trade["direction"]
                if d == "long" and current_price <= sl:
                    pnl = (sl - trade["entry_price"]) * trade["size"]
                    _mark_exit(conn, trade, sl, "sl_hit_shadow", pnl)
                    stats["exits"] += 1
                elif d == "short" and current_price >= sl:
                    pnl = (trade["entry_price"] - sl) * trade["size"]
                    _mark_exit(conn, trade, sl, "sl_hit_shadow", pnl)
                    stats["exits"] += 1
                elif tp1 and d == "long" and current_price >= tp1:
                    pnl = (tp1 - trade["entry_price"]) * trade["size"]
                    _mark_exit(conn, trade, tp1, "tp1_hit_shadow", pnl)
                    stats["exits"] += 1
                elif tp1 and d == "short" and current_price <= tp1:
                    pnl = (trade["entry_price"] - tp1) * trade["size"]
                    _mark_exit(conn, trade, tp1, "tp1_hit_shadow", pnl)
                    stats["exits"] += 1
                else:
                    still_open_assets.append(asset)
                    stats["open"] += 1
                    log(f"[MONITOR] Trade #{trade['id']} {asset} shadow @ {current_price:.4f} → offen")
            else:
                still_open_assets.append(asset)
                stats["open"] += 1
                log(f"[MONITOR] Trade #{trade['id']} {asset} shadow → kein Preis, als offen markiert")
            continue

        pos = live_positions.get(asset)

        if pos is None:
            # Keine Position mehr auf Bitget → Exit
            _mark_exit(conn, trade, exit_price=trade["entry_price"],
                       reason="position_closed_external", pnl_usd=0.0)
            stats["exits"] += 1
            # HWM nach Exit aktualisieren (balance wurde oben gecacht)
            if balance > 0:
                hwm = get_hwm()
                if balance > hwm:
                    set_hwm(balance)
                    log(f"[MONITOR] HWM nach Exit aktualisiert: {hwm:.2f} → {balance:.2f} USDT")
        else:
            # Position noch offen
            current_price = pos.entry_price  # beste verfügbare Approximation
            be_before = trade["be_applied"]
            _check_break_even(conn, trade, current_price)
            be_after = conn.execute(
                "SELECT be_applied FROM trades WHERE id=?", (trade["id"],)
            ).fetchone()[0]
            if be_after and not be_before:
                stats["be_applied_new"] += 1
            still_open_assets.append(asset)
            stats["open"] += 1

    _update_open_positions_state(conn, list(set(still_open_assets)))
    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    import time as _time
    log("[MONITOR] Daemon-Modus gestartet (Intervall: 30s)")
    while True:
        try:
            stats = run_position_monitor()
            log(f"[MONITOR] Lauf abgeschlossen: {stats}")
        except Exception as e:
            log(f"[MONITOR] Fehler im Daemon-Loop: {e}")
        _time.sleep(30)
