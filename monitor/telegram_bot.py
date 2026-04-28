#!/usr/bin/env python3
"""
APEX V2 Telegram Command & Control Bot

Läuft als eigenständiger Daemon (Polling).
Interaktives Inline-Menü + proaktive Push-Benachrichtigungen.

Start:  python3 monitor/telegram_bot.py
Cron:   nicht als Cron — als Daemon via systemd oder tmux/screen.

Push-Events (automatisch):
  • Neuer Trade executed (Dry-Run oder Live)
  • Kritischer Heartbeat-Ausfall (>10 min)
  • Tagesstatus (täglich 08:00 UTC)

Interaktive Commands:
  /start  /menu  → Haupt-Inline-Menü
  /status        → System-Status direkt
  /pnl           → Dashboard direkt
"""

import sys
import os

# .env laden BEVOR eigene Module (config.settings) importiert werden
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

import asyncio
import logging
import math
import concurrent.futures
import time as _time
from datetime import datetime, timezone, timedelta

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes,
)
from telegram.constants import ParseMode

from core.db import get_connection
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logging.basicConfig(
    format="%(asctime)s [BOT] %(levelname)s %(message)s",
    level=logging.WARNING,
)
log = logging.getLogger(__name__)

# ── Lab-Referenzwerte (Auto-Lab 2026-04-27) ──────────────────────────────────
# wr_test: beste OOS-Win-Rate aus lab_discoveries (live abgefragt falls verfügbar)
LAB_REF = {
    "ETH": {"avg_r": 0.095, "pf": 1.14, "wr": 40.4},
    "BTC": {"avg_r": 0.068, "pf": 1.10, "wr": 36.8},
    "SOL": {"avg_r": 0.053, "pf": 1.07, "wr": 40.0},  # Fallback 40% wenn keine DB-Daten
}

HEARTBEAT_MAX_AGE_MIN = {
    "intake": 10, "features": 10,
    "strategies": 30, "governance": 30,
    "executor": 30, "monitor": 30,
}


def _canary_target(asset: str) -> int:
    """
    Dynamisches Trade-Ziel für den Standard-Canary pro Asset.
    Formel identisch zu Deployments: max(30, ceil(15 / WR)).
    WR kommt aus LAB_REF (gefüllt aus lab_discoveries-Daten).
    """
    wr_pct = LAB_REF.get(asset, {}).get("wr", 40.0)
    return max(30, int(math.ceil(15.0 / (wr_pct / 100.0))))


# Alle Assets für den Markt-Wetterbericht (Reihenfolge = Anzeige-Reihenfolge)
WEATHER_ASSETS = ["BTC", "ETH", "SOL", "XRP", "AVAX"]

_REGIME_WEATHER = {
    "TREND_UP":   ("🟢", "UP"),
    "TREND_DOWN": ("🔴", "DOWN"),
    "SIDEWAYS":   ("🟡", "SIDE"),
    "UNKNOWN":    ("⚪", "?"),
}


def _db_market_weather() -> str:
    """Liest aktuelle Regime aus system_state und gibt eine kompakte Zeile zurück."""
    from core.db import get_state
    parts = []
    for asset in WEATHER_ASSETS:
        regime = get_state(f"regime_{asset}", "UNKNOWN")
        icon, label = _REGIME_WEATHER.get(regime, ("⚪", "?"))
        parts.append(f"`{asset}` {icon}{label}")
    return "  ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# DB-Abfragen
# ══════════════════════════════════════════════════════════════════════════════

def _db_pnl_summary() -> dict:
    conn = get_connection()
    rows = conn.execute(
        """SELECT strategy, asset, COUNT(*) n,
                  SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) wins,
                  SUM(pnl_r) total_r, AVG(pnl_r) avg_r
           FROM trades
           WHERE exit_ts IS NOT NULL
           GROUP BY strategy, asset
           ORDER BY total_r DESC"""
    ).fetchall()
    overall = conn.execute(
        "SELECT COUNT(*), SUM(pnl_r), AVG(pnl_r) FROM trades WHERE exit_ts IS NOT NULL"
    ).fetchone()
    today_r = conn.execute(
        "SELECT SUM(pnl_r) FROM trades WHERE exit_ts IS NOT NULL AND exit_ts >= ?",
        ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
    ).fetchone()[0] or 0.0
    conn.close()
    return {
        "rows":     [dict(r) for r in rows],
        "total_n":  overall[0] or 0,
        "total_r":  round(overall[1] or 0, 2),
        "avg_r":    round(overall[2] or 0, 4),
        "today_r":  round(today_r, 2),
    }


def _db_open_signals() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, strategy, asset, direction, entry_price, stop_loss,
                  take_profit_1, status, mode, created_at
           FROM signals
           WHERE status IN ('approved','processing','pending')
           ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_heartbeats() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT component, status, message, ts, latency_ms
           FROM heartbeats h1
           WHERE ts = (SELECT MAX(ts) FROM heartbeats h2 WHERE h2.component=h1.component)
           ORDER BY component"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_active_deployments() -> list[dict]:
    """Alle aktiven Deploy-Instanzen inkl. ihrer Trade-Performance."""
    conn = get_connection()
    deps = conn.execute(
        """SELECT discovery_id, strategy_key, base_strategy, asset, market_regime,
                  mode, deployed_at, target_trades, go_live_notified
           FROM active_deployments WHERE active=1 ORDER BY deployed_at"""
    ).fetchall()

    result = []
    for dep in deps:
        sk = dep["strategy_key"]
        stats = conn.execute(
            """SELECT COUNT(*) n,
                      COALESCE(SUM(pnl_r), 0.0)  total_r,
                      COALESCE(AVG(pnl_r), 0.0)  avg_r,
                      COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) wins
               FROM trades WHERE strategy=? AND exit_ts IS NOT NULL""",
            (sk,),
        ).fetchone()
        open_n = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy=? AND status IN ('pending','approved','processing')",
            (sk,),
        ).fetchone()[0]
        result.append({
            "strategy_key":     sk,
            "discovery_id":     dep["discovery_id"],
            "asset":            dep["asset"],
            "regime":           dep["market_regime"] or "?",
            "mode":             dep["mode"],
            "target_trades":    dep["target_trades"],
            "go_live_notified": dep["go_live_notified"],
            "n":                stats["n"],
            "total_r":          stats["total_r"],
            "avg_r":            stats["avg_r"],
            "wins":             stats["wins"],
            "open_signals":     open_n,
        })
    conn.close()
    return result


def _db_canary() -> dict:
    conn = get_connection()
    rows = conn.execute(
        """SELECT asset,
                  COUNT(*)                                          n,
                  SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END)      wins,
                  COALESCE(SUM(pnl_r),  0.0)                       total_r,
                  COALESCE(AVG(pnl_r),  0.0)                       avg_r
           FROM trades
           WHERE strategy='squeeze' AND mode='dry_run' AND exit_ts IS NOT NULL
           GROUP BY asset"""
    ).fetchall()
    conn.close()
    return {r["asset"]: dict(r) for r in rows}


def _db_last_trades(limit: int = 5) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT strategy, asset, direction, entry_price, exit_price,
                  exit_reason, pnl_r, mode, exit_ts
           FROM trades WHERE exit_ts IS NOT NULL
           ORDER BY exit_ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_new_executed_since(since_iso: str) -> list[dict]:
    """Frisch ausgeführte Trades seit `since_iso` — für Push-Benachrichtigungen."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT strategy, asset, direction, entry_price, exit_price,
                  exit_reason, pnl_r, mode, exit_ts, id
           FROM trades WHERE exit_ts >= ? ORDER BY exit_ts ASC""",
        (since_iso,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_alpha_setups() -> list[dict]:
    """Bestes Setup pro (asset, market_regime) nach Micro-Score sortiert."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT d.id, d.strategy, d.asset, d.market_regime,
                      d.pf_test, d.avg_r_test, d.wr_test, d.n_test,
                      d.fitness_score, d.params_json,
                      COALESCE(d.max_dd_r, 0.0)   AS max_dd_r,
                      COALESCE(d.micro_score, d.pf_test * 10.0) AS micro_score
               FROM lab_discoveries d
               INNER JOIN (
                   SELECT asset, market_regime,
                          MAX(COALESCE(micro_score, pf_test * 10.0)) AS best_score
                   FROM lab_discoveries
                   WHERE market_regime != 'UNKNOWN'
                   GROUP BY asset, market_regime
               ) best ON d.asset = best.asset
                      AND d.market_regime = best.market_regime
                      AND COALESCE(d.micro_score, d.pf_test * 10.0) = best.best_score
               ORDER BY micro_score DESC"""
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


from core.autopilot import (
    deploy_discovery as _db_deploy,
    calc_target_trades as _calc_target_trades,
    deactivate_asset_deployments as _deactivate_asset,
)


# ── CIO-Logik ─────────────────────────────────────────────────────────────────

_REGIME_ICON_CIO = {
    "TREND_UP":   "🟢",
    "TREND_DOWN": "🔴",
    "SIDEWAYS":   "🟡",
    "UNKNOWN":    "⚪",
}

RISK_PER_TRADE_CIO = 1.50   # USDT


def _cio_best_setup(asset: str, regime: str) -> dict | None:
    """
    Findet das Setup mit dem höchsten micro_score für (asset, regime).
    Fallback auf pf_test wenn micro_score noch nicht berechnet (ältere Einträge).
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT id, strategy, pf_test, avg_r_test, wr_test, n_test,
                  COALESCE(max_dd_r, 0.0)              AS max_dd_r,
                  COALESCE(micro_score, pf_test * 10.0) AS micro_score
           FROM lab_discoveries
           WHERE asset=? AND market_regime=?
             AND market_regime != 'UNKNOWN'
           ORDER BY COALESCE(micro_score, pf_test * 10.0) DESC
           LIMIT 1""",
        (asset, regime),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _cio_portfolio() -> list[dict]:
    """
    Kernfunktion des CIO-Modus.
    Liest für jedes Live-Asset das aktuelle Regime und findet das beste Setup.

    Rückgabe: Liste von Dicts, eines pro Asset:
      {"asset", "regime", "setup": {...} | None}
    """
    from config.settings import LIVE_ASSETS
    from core.db import get_state

    results = []
    for asset in LIVE_ASSETS:
        regime = get_state(f"regime_{asset}") or "UNKNOWN"
        setup  = _cio_best_setup(asset, regime) if regime != "UNKNOWN" else None
        results.append({"asset": asset, "regime": regime, "setup": setup})
    return results


def _build_portfolio_text(portfolio: list[dict]) -> str:
    """Formatiert die CIO-Empfehlung als Telegram-Markdown-Text."""
    lines = ["💼 *CIO Portfolio\\-Empfehlung \\(Live\\)*\n"]
    any_setup = False

    for p in portfolio:
        asset  = p["asset"]
        regime = p["regime"]
        setup  = p["setup"]
        icon   = _REGIME_ICON_CIO.get(regime, "⚪")

        if setup:
            any_setup   = True
            dd_usdt     = setup["max_dd_r"] * RISK_PER_TRADE_CIO
            score       = setup["micro_score"]
            lines.append(
                f"*{asset}* \\({icon} {regime}\\)\n"
                f"  Setup \\#{setup['id']} \\| `{setup['strategy']}`\n"
                f"  Score: *{score:.1f}*  PF: *{setup['pf_test']:.2f}*  "
                f"WR: *{setup['wr_test']:.1f}%*\n"
                f"  Max DD: *\\-${dd_usdt:.2f}*  n={setup['n_test']}\n"
            )
        else:
            reason = "kein Setup im Lab" if regime != "UNKNOWN" else "Regime unbekannt"
            lines.append(f"*{asset}* \\({icon} {regime}\\)\n  _\\({reason}\\)_\n")

    if not any_setup:
        lines.append(
            "\n⚠️ _Kein passendes Setup für das aktuelle Markt\\-Regime\\._\n"
            "_Lab\\-Daemon läuft weiter — check später\\._"
        )
    return "\n".join(lines)


def _portfolio_keyboard(portfolio: list[dict]) -> InlineKeyboardMarkup:
    """
    Granulares Inline-Keyboard unter der CIO-Empfehlung.

    Pro Asset mit vorhandenem Setup eine eigene Zeile:
      [🚀 BTC Live]  [🧪 BTC Dry-Run]

    Darunter (nur wenn ≥2 Setups vorhanden):
      [🔥 ALLE LIVE (Risiko!)]

    Callback-Format:
      cio_single_live:<disc_id>:<asset>
      cio_single_dry:<disc_id>:<asset>
      cio_all_live_confirm:<id1,id2,...>
    """
    rows  = []
    valid = [(p["asset"], p["setup"]["id"]) for p in portfolio if p["setup"]]

    for asset, disc_id in valid:
        rows.append([
            InlineKeyboardButton(
                f"🚀 {asset} Live",
                callback_data=f"cio_single_live:{disc_id}:{asset}",
            ),
            InlineKeyboardButton(
                f"🧪 {asset} Dry-Run",
                callback_data=f"cio_single_dry:{disc_id}:{asset}",
            ),
        ])

    if len(valid) >= 2:
        all_ids = ",".join(str(i) for _, i in valid)
        rows.append([
            InlineKeyboardButton(
                "🔥 ALLE LIVE (Risiko!)",
                callback_data=f"cio_all_live_confirm:{all_ids}",
            ),
        ])

    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="portfolio"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _server_health() -> dict:
    """CPU- und RAM-Auslastung via psutil (blockiert kurz für cpu_percent)."""
    cpu  = psutil.cpu_percent(interval=1)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    uptime_h = (datetime.now(timezone.utc) - boot).total_seconds() / 3600
    return {
        "cpu_pct":    cpu,
        "ram_pct":    ram.percent,
        "ram_used_gb": ram.used / 1024**3,
        "ram_total_gb": ram.total / 1024**3,
        "disk_pct":   disk.percent,
        "disk_free_gb": disk.free / 1024**3,
        "uptime_h":   uptime_h,
    }


def _fetch_binance_candles(asset: str, days: int) -> dict:
    """
    Lädt historische 1h-Kerzen via ccxt (Binance Futures, kein API-Key nötig).
    Speichert mit source='binance' — immun gegen 30-Tage-Cleanup.
    Gibt {"inserted": N, "existing": M, "error": None} zurück.
    """
    import ccxt as _ccxt
    from datetime import datetime, timezone

    SYMBOL_MAP = {
        "BTC":  "BTC/USDT:USDT",  "ETH":  "ETH/USDT:USDT",
        "SOL":  "SOL/USDT:USDT",  "XRP":  "XRP/USDT:USDT",
        "ADA":  "ADA/USDT:USDT",  "LINK": "LINK/USDT:USDT",
        "AVAX": "AVAX/USDT:USDT", "BNB":  "BNB/USDT:USDT",
        "DOGE": "DOGE/USDT:USDT",
    }
    symbol = SYMBOL_MAP.get(asset.upper())
    if not symbol:
        return {"inserted": 0, "existing": 0, "error": f"Kein Binance-Symbol für {asset}"}

    try:
        ex       = _ccxt.binance({"options": {"defaultType": "future"}})
        now_ms   = int(_time.time() * 1000)
        start_ms = now_ms - days * 86_400_000
        chunk    = 1000
        since    = start_ms
        inserted = 0
        existing = 0

        conn        = get_connection()
        fetched_at  = datetime.now(timezone.utc).isoformat()

        # Vorhandene Timestamps für schnelles Deduplizieren
        known = set(
            r[0] for r in conn.execute(
                "SELECT ts FROM candles WHERE asset=? AND interval='1h'", (asset.upper(),)
            ).fetchall()
        )

        while since < now_ms:
            ohlcv = ex.fetch_ohlcv(symbol, "1h", since=since, limit=chunk)
            if not ohlcv:
                break
            for bar in ohlcv:
                ts = bar[0]
                if ts in known:
                    existing += 1
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO candles
                       (asset, interval, ts, open, high, low, close, volume, fetched_at, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (asset.upper(), "1h", ts,
                     bar[1], bar[2], bar[3], bar[4], bar[5],
                     fetched_at, "binance"),
                )
                known.add(ts)
                inserted += 1
            conn.commit()
            since = ohlcv[-1][0] + 3_600_000
            _time.sleep(0.25)

        conn.close()
        return {"inserted": inserted, "existing": existing, "error": None}

    except Exception as e:
        return {"inserted": 0, "existing": 0, "error": str(e)}


def _run_lab_backtest(asset: str, days: int = 365) -> dict:
    """
    Führt einen schnellen Squeeze-Backtest für `asset` (synchron, im Thread-Pool).
    Nutzt Champion-Parameter + 3 Varianten als Mini-Grid.
    Gibt das Ergebnis-Dict zurück.
    """
    import time
    from backtest.engine import run_backtest

    VARIANTS = [
        {"SQUEEZE_PERIOD": 20, "EMA_PERIOD": 25, "SL_ATR_MULT": 1.5, "TP_R": 3.0},  # Champion
        {"SQUEEZE_PERIOD": 20, "EMA_PERIOD": 20, "SL_ATR_MULT": 1.0, "TP_R": 4.0},
        {"SQUEEZE_PERIOD": 15, "EMA_PERIOD": 20, "SL_ATR_MULT": 1.5, "TP_R": 4.0},
    ]

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000

    best = None
    for cfg in VARIANTS:
        try:
            r = run_backtest("squeeze", asset, start_ms, now_ms, cfg=cfg)
            s = r.summary()
            n = s["trades"]
            if n == 0:
                continue
            wins  = [t.pnl_r for t in r.trades if t.pnl_r > 0]
            losses= [t.pnl_r for t in r.trades if t.pnl_r < 0]
            gw    = sum(wins)
            gl    = abs(sum(losses))
            pf    = round(gw / gl, 3) if gl > 0 else 999.0
            row   = {
                "n": n, "total_r": round(s["total_r"], 2),
                "avg_r": round(s["avg_r"], 4),
                "wr": round(s["winrate"], 1),
                "pf": pf, "cfg": cfg,
            }
            if best is None or row["pf"] > best["pf"]:
                best = row
        except Exception as e:
            best = best or {"error": str(e)}

    return best or {"n": 0, "total_r": 0, "avg_r": 0, "wr": 0, "pf": 0, "cfg": {}}


# ══════════════════════════════════════════════════════════════════════════════
# Formatter
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_r(r: float) -> str:
    return f"{r:+.2f}R"


def _fmt_disc(live: float, lab: float) -> str:
    if not lab:
        return ""
    d = (live - lab) / abs(lab)
    icon = "✅" if abs(d) <= 0.3 else ("⚠️" if abs(d) <= 0.5 else "🛑")
    return f"{icon} {d:+.0%}"


def _r_bar(r: float, scale: float = 3.0, width: int = 10) -> str:
    filled = min(int(abs(r) / scale * width), width)
    if r >= 0:
        return "▓" * filled + "·" * (width - filled)
    return "░" * filled + "·" * (width - filled)


def build_dashboard_text() -> str:
    d   = _db_pnl_summary()
    can = _db_canary()

    lines = ["📊 *APEX V2 — Dashboard*\n"]
    lines.append(f"🌤 *Markt\\-Wetter:*  {_db_market_weather()}\n")
    lines.append(f"Trades gesamt: *{d['total_n']}*  |  Gesamt: *{_fmt_r(d['total_r'])}*")
    lines.append(f"Ø Avg R: *{_fmt_r(d['avg_r'])}*  |  Heute: *{_fmt_r(d['today_r'])}*\n")

    if d["rows"]:
        lines.append("*Nach Strategie/Asset:*")
        for r in d["rows"]:
            wr = round(r["wins"] / r["n"] * 100) if r["n"] else 0
            lines.append(
                f"  `{r['strategy']}/{r['asset']}`  "
                f"n={r['n']}  AvgR={_fmt_r(r['avg_r'] or 0)}  WR={wr}%"
            )

    lines.append("\n*🐦 Squeeze Canary \\(Dry-Run\\):*")
    total_can   = 0
    all_ready   = True
    for asset, ref in LAB_REF.items():
        target  = _canary_target(asset)
        c       = can.get(asset)
        n       = c["n"]     if c else 0
        total_r = c["total_r"] if c else 0.0
        avg     = c["avg_r"]   if c else 0.0
        wins    = c["wins"]    if c else 0
        total_can += n

        filled  = min(int(n / target * 10), 10) if target else 0
        prog    = "▓" * filled + "░" * (10 - filled)
        pct     = min(int(n / target * 100), 100) if target else 0
        disc    = _fmt_disc(avg, ref["avg_r"]) if n > 0 else ""

        if n == 0:
            all_ready = False
            lines.append(f"  `{asset}` 0/{target} \\[{prog}\\] 0%")
        elif n < target:
            all_ready = False
            wr = round(wins / n * 100) if n else 0
            lines.append(
                f"  `{asset}` {n}/{target} \\[{prog}\\] {pct}%  "
                f"· {_fmt_r(total_r)}  AvgR={_fmt_r(avg)} {disc}  WR={wr}%"
            )
        else:
            wr    = round(wins / n * 100) if n else 0
            badge = "🟢" if total_r > 0 else "🔴"
            lines.append(
                f"  `{asset}` {n}/{target} \\[{prog}\\] {pct}%  {badge}\n"
                f"  {_fmt_r(total_r)}  AvgR={_fmt_r(avg)} {disc}  WR={wr}%"
            )

    if all_ready and total_can > 0:
        lines.append("\n🎯 *Alle Assets bereit — Go/No\\-Go Entscheidung fällig\\!*")
    else:
        remaining = sum(max(0, _canary_target(a) - (can.get(a, {}).get("n") or 0)) for a in LAB_REF)
        lines.append(f"\n⏳ Noch ca\\. *{remaining}* Trades bis Go\\-Live\\-Entscheidung")

    # ── Aktive Deployments ────────────────────────────────────────────────────
    deployments = _db_active_deployments()
    if deployments:
        lines.append("\n🚀 *Active Deployments:*")
        _regime_icon = {"TREND_UP": "📈", "TREND_DOWN": "📉", "SIDEWAYS": "↔️"}
        for dep in deployments:
            n       = dep["n"]
            total_r = dep["total_r"]
            avg_r   = dep["avg_r"]
            wr      = round(dep["wins"] / n * 100) if n > 0 else 0
            r_icon  = _regime_icon.get(dep["regime"], "❓")
            open_s  = dep["open_signals"]

            target  = dep["target_trades"]
            filled  = min(int(n / target * 10), 10) if target else 0
            prog    = "▓" * filled + "░" * (10 - filled)
            pct     = min(int(n / target * 100), 100) if target else 0

            if n == 0:
                trade_line = f"0/{target} Trades \\[{prog}\\] 0%"
                if open_s:
                    trade_line += f"  · {open_s} Signal offen"
            else:
                trade_line = (
                    f"{n}/{target} Trades \\[{prog}\\] {pct}%  "
                    f"· {_fmt_r(total_r)}  AvgR={_fmt_r(avg_r)}  WR={wr}%"
                )

            # Go-Live Badge wenn Ziel erreicht
            badge = ""
            if n >= target:
                badge = "  🟢 *BEREIT*" if dep["total_r"] > 0 else "  🔴 *FAILED*"

            lines.append(
                f"  `{dep['strategy_key']}` \\(`{dep['asset']}`\\) {r_icon}{dep['regime']}{badge}\n"
                f"  {trade_line}  \\[`{dep['mode']}`\\]"
            )
    else:
        lines.append("\n_Keine aktiven Deployments — nutze `/deploy <ID>` aus dem Alpha\\-Dashboard\\._")

    return "\n".join(lines)


def build_signals_text() -> str:
    sigs = _db_open_signals()
    if not sigs:
        return "📂 *Offene Signale*\n\nKeine pending/approved/processing Signale."

    lines = [f"📂 *Offene Signale* ({len(sigs)})\n"]
    for s in sigs:
        dt  = s["created_at"][:16].replace("T", " ")
        dir_icon = "📈" if s["direction"] == "long" else "📉"
        status_icon = {"pending": "⏳", "approved": "✅", "processing": "⚙️"}.get(s["status"], "❓")
        lines.append(
            f"{status_icon} `{s['strategy']}/{s['asset']}` {dir_icon} "
            f"@ {s['entry_price']}  [{s['mode']}]\n"
            f"   SL {s['stop_loss']} → TP {s['take_profit_1']}  `{dt}`"
        )
    return "\n".join(lines)


def build_status_text() -> str:
    hbs  = _db_heartbeats()
    hw   = _server_health()
    now  = datetime.now(timezone.utc)

    # ── Server-Ressourcen ─────────────────────────────────────────────────────
    def _res_icon(pct: float, warn: float = 70, crit: float = 90) -> str:
        return "🔴" if pct >= crit else ("⚠️" if pct >= warn else "✅")

    lines = ["⚙️ *System Status*\n"]
    lines.append("*Server-Ressourcen:*")
    lines.append(
        f"  {_res_icon(hw['cpu_pct'])} CPU: *{hw['cpu_pct']:.1f}%*"
    )
    lines.append(
        f"  {_res_icon(hw['ram_pct'])} RAM: *{hw['ram_pct']:.1f}%*"
        f"  ({hw['ram_used_gb']:.1f}/{hw['ram_total_gb']:.1f} GB)"
    )
    lines.append(
        f"  {_res_icon(hw['disk_pct'], 80, 95)} Disk: *{hw['disk_pct']:.1f}%*"
        f"  ({hw['disk_free_gb']:.1f} GB frei)"
    )
    lines.append(f"  🕐 Uptime: {hw['uptime_h']:.1f}h\n")

    # ── Pipeline-Heartbeats ───────────────────────────────────────────────────
    lines.append("*Pipeline-Heartbeats:*")
    if not hbs:
        lines.append("  ⚠️ Keine Heartbeats — läuft master\\_run.py?")
    else:
        all_ok = True
        for hb in hbs:
            comp    = hb["component"]
            max_min = HEARTBEAT_MAX_AGE_MIN.get(comp, 30)
            try:
                ts  = datetime.fromisoformat(hb["ts"])
                age = (now - ts).total_seconds() / 60
            except Exception:
                age = 999.0

            if age <= max_min:
                icon = "✅"
            elif age <= max_min * 2:
                icon = "⚠️"
                all_ok = False
            else:
                icon = "🔴"
                all_ok = False

            age_str = f"{int(age)}m" if age < 120 else f"{age/60:.1f}h"
            lat_str = f" {hb['latency_ms']:.0f}ms" if hb["latency_ms"] else ""
            lines.append(f"  {icon} `{comp:<12}` {age_str} alt{lat_str}")

        lines.append("")
        lines.append("  ✅ Pipeline OK" if all_ok else "  ⚠️ Mindestens eine Komponente auffällig")

    # ── letzte 5 Trades ───────────────────────────────────────────────────────
    recent = _db_last_trades(5)
    if recent:
        lines.append("\n*Letzte Trades:*")
        for t in recent:
            r_str = _fmt_r(t["pnl_r"] or 0)
            icon  = "🟢" if (t["pnl_r"] or 0) > 0 else "🔴"
            lines.append(f"  {icon} `{t['strategy']}/{t['asset']}` {r_str}  [{t['exit_reason']}]")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Tastatur
# ══════════════════════════════════════════════════════════════════════════════

def persistent_keyboard() -> ReplyKeyboardMarkup:
    """Dauerhaftes Tastenfeld — bleibt im Chat-Eingabebereich angedockt."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Dashboard"),          KeyboardButton("🏆 Alpha Setups")],
            [KeyboardButton("💼 Portfolio Empfehlung"), KeyboardButton("⚙️ Status")],
            [KeyboardButton("🔌 API Test"),             KeyboardButton("📖 Hilfe")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Dashboard",      callback_data="dashboard"),
            InlineKeyboardButton("📂 Offene Trades",  callback_data="signals"),
        ],
        [
            InlineKeyboardButton("⚙️ System Status",  callback_data="status"),
            InlineKeyboardButton("🔄 Aktualisieren",  callback_data="refresh_menu"),
        ],
        [
            InlineKeyboardButton("🏆 Top Alpha Setups", callback_data="alpha"),
            InlineKeyboardButton("📖 Hilfe",            callback_data="help"),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════
# Handler
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *APEX V2 — Command & Control*\n\n"
        "Das Tastenfeld ist jetzt dauerhaft angedockt\\.\n"
        "Tippe auf eine Schaltfläche oder nutze Befehle wie `/lab ETH`\\.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=persistent_keyboard(),
    )
    # Inline-Menü als zweite Nachricht
    await update.message.reply_text(
        "📋 *Schnellzugriff:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Hauptmenü*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = build_status_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aktualisieren", callback_data="status"),
            InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
        ]]),
    )


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = build_dashboard_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aktualisieren", callback_data="dashboard"),
            InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
        ]]),
    )


async def cmd_lab(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /lab <ASSET> [DAYS]
    Startet einen On-Demand Squeeze-Backtest im Thread-Pool.
    Antwort kommt als neue Nachricht sobald der Test fertig ist.
    """
    args  = (ctx.args or [])
    asset = args[0].upper() if args else None
    days  = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 365

    if not asset:
        await update.message.reply_text(
            "❌ Nutzung: `/lab ETH` oder `/lab XRP 180`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Sofort bestätigen — Backtest dauert einige Sekunden
    wait_msg = await update.message.reply_text(
        f"🔬 Starte Squeeze-Backtest für *{asset}* über {days} Tage\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Backtest im Thread-Pool (blockiert nicht den Event-Loop)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, _run_lab_backtest, asset, days
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Backtest-Fehler: {e}")
        return

    if result.get("error"):
        await wait_msg.edit_text(f"❌ Fehler: {result['error']}")
        return

    n      = result["n"]
    if n == 0:
        await wait_msg.edit_text(
            f"⚠️ *{asset}* — Keine Trades in {days} Tagen\\.\n"
            f"Möglicherweise fehlen 1h\\-Candles in der DB\\.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pf      = result["pf"]
    avg_r   = result["avg_r"]
    wr      = result["wr"]
    total_r = result["total_r"]
    cfg     = result["cfg"]

    verdict_icon = "✅" if pf >= 1.3 else ("⚠️" if pf >= 1.1 else "❌")
    verdict_text = "Starker Edge" if pf >= 1.3 else ("Schwacher Edge" if pf >= 1.1 else "Kein Edge")

    param_lines = "\n".join(f"  `{k}` = `{v}`" for k, v in sorted(cfg.items()))
    text = (
        f"🔬 *Lab-Test: squeeze/{asset}* \\({days} Tage\\)\n\n"
        f"{verdict_icon} *{verdict_text}*\n\n"
        f"Trades:  *{n}*\n"
        f"Total R: *{total_r:+.2f}R*\n"
        f"Avg R:   *{avg_r:+.3f}R*\n"
        f"Win\\-Rate: *{wr:.1f}%*\n"
        f"PF:      *{pf:.2f}*\n\n"
        f"*Beste Parameter:*\n{param_lines}"
    )
    await wait_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)


_HELP_TEXT = (
    "📖 *APEX V2 — Befehlsübersicht*\n\n"
    "`/menu` — Haupt\\-Menü öffnen\n"
    "`/status` — System\\-Status \\(Heartbeats, Server\\)\n"
    "`/pnl` — Dashboard \\(P&L, Canary\\)\n"
    "`/lab <ASSET> [TAGE]` — On\\-Demand Squeeze\\-Backtest\n"
    "    Beispiel: `/lab ETH 365`\n"
    "`/fetch <ASSET> <TAGE>` — Historische Kerzen via Binance laden\n"
    "    Beispiel: `/fetch XRP 180`\n"
    "`/help` — Diese Übersicht\n\n"
    "*Assets:* ETH, BTC, SOL, XRP, AVAX, DOGE, ADA, BNB"
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
        ]]),
    )


async def cmd_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /fetch <ASSET> <TAGE>
    Lädt historische Kerzen via ccxt/Binance in die DB.
    Läuft im Thread-Pool damit der Bot nicht blockiert.
    """
    args  = ctx.args or []
    asset = args[0].upper() if len(args) >= 1 else None
    days  = int(args[1]) if len(args) >= 2 and args[1].isdigit() else None

    if not asset or not days:
        await update.message.reply_text(
            "❌ Nutzung: `/fetch ETH 365` oder `/fetch XRP 180`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    wait_msg = await update.message.reply_text(
        f"⏳ Lade Daten für *{asset}* \\({days} Tage\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_binance_candles, asset, days)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Fehler beim Download: {e}")
        return

    if result.get("error"):
        await wait_msg.edit_text(f"❌ Fehler: {result['error']}")
        return

    inserted = result["inserted"]
    existing = result["existing"]
    total    = inserted + existing
    await wait_msg.edit_text(
        f"✅ *{asset}* — {days} Tage geladen\n"
        f"Neue Kerzen: *{inserted}*  |  Bereits vorhanden: *{existing}*  |  Gesamt: *{total}*",
        parse_mode=ParseMode.MARKDOWN,
    )


_REGIME_ICON = {"TREND_UP": "📈", "TREND_DOWN": "📉", "SIDEWAYS": "↔️", "UNKNOWN": "❓"}


def _build_alpha_text() -> str:
    setups = _db_alpha_setups()
    if not setups:
        return (
            "🏆 *Top Alpha Setups*\n\n"
            "Noch keine kategorisierten Funde in der Alpha\\-Library\\.\n"
            "Der Lab\\-Daemon muss mindestens eine Iteration abgeschlossen haben\\."
        )

    lines = ["🏆 *Top Alpha Setups — Sortiert nach Micro\\-Score*\n"]
    import json as _json
    RISK_PER_TRADE = 1.50
    for s in setups:
        icon      = _REGIME_ICON.get(s["market_regime"], "❓")
        params    = _json.loads(s["params_json"])
        p_str     = "  ".join(f"`{k}`={v}" for k, v in sorted(params.items()))
        max_dd_r  = s.get("max_dd_r") or 0.0
        max_dd_usd = max_dd_r * RISK_PER_TRADE
        score     = s.get("micro_score") or s["pf_test"] * 10.0
        lines.append(
            f"*\\[ID {s['id']}\\]* `{s['asset']}` {icon} {s['market_regime']}\n"
            f"  PF: *{s['pf_test']:.2f}*  WR: *{s['wr_test']:.1f}%*  "
            f"Max DD: *\\-${max_dd_usd:.2f}*  Score: *{score:.1f}*\n"
            f"  {p_str}\n"
            f"  → `/deploy {s['id']}`"
        )

    lines.append("\n_Micro\\-Score = PF / \\(MaxDD\\$/Kapital\\) — höher = besser für Micro\\-Account\\._")
    lines.append("_Benutze `/deploy <ID>` um ein Setup als parallelen Dry\\-Run zu starten\\._")
    return "\n".join(lines)


async def cmd_alpha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = _build_alpha_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aktualisieren", callback_data="alpha"),
            InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
        ]]),
    )


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/portfolio — CIO-Modus: beste Setup-Empfehlung für aktuelle Marktlage."""
    import asyncio
    portfolio = await asyncio.get_event_loop().run_in_executor(None, _cio_portfolio)
    text = _build_portfolio_text(portfolio)
    kb   = _portfolio_keyboard(portfolio)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cmd_api_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /api_test — prüft API-Verbindung und gibt Futures-Balance zurück.
    Diagnostiziert häufige Fehler: falscher Key, IP-Whitelist, Futures inaktiv.
    """
    await update.message.reply_text(
        "🔌 Teste API\\-Verbindung \\.\\.\\.", parse_mode=ParseMode.MARKDOWN
    )

    def _run_test() -> dict:
        from execution.bitget_client import BitgetClient
        client = BitgetClient(dry_run=False)

        if not client.is_ready:
            return {"ok": False, "error": "no_credentials",
                    "msg": "Keine API-Credentials in config/.env gefunden."}
        try:
            balance = client.get_balance()
            # Kontrakt-Limits für alle Live-Assets holen (Diagnose)
            from config.settings import LIVE_ASSETS
            contract_info = {}
            for asset in LIVE_ASSETS:
                info = client.get_contract_info(asset)
                contract_info[asset] = info
            return {"ok": True, "balance": balance, "contracts": contract_info}
        except Exception as e:
            err = str(e)
            if "40037" in err or "invalid api key" in err.lower():
                msg = "❌ Ungültiger API-Key — bitte in Bitget prüfen."
            elif "40039" in err or "ip" in err.lower():
                msg = "❌ IP nicht auf der Whitelist — bitte Server-IP in Bitget freischalten."
            elif "40034" in err or "permission" in err.lower():
                msg = "❌ Futures-Handel nicht freigeschaltet — bitte in Bitget unter 'Futures' aktivieren."
            elif "429" in err:
                msg = "❌ Rate-Limit getroffen — bitte in 60 Sekunden erneut versuchen."
            else:
                msg = f"❌ API-Fehler: {err[:200]}"
            return {"ok": False, "error": err, "msg": msg}

    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, _run_test)

    if not result["ok"]:
        await update.message.reply_text(
            f"*API\\-Diagnose*\n\n{result['msg']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=persistent_keyboard(),
        )
        return

    bal = result["balance"]
    contracts = result.get("contracts", {})
    contract_lines = []
    for asset, info in contracts.items():
        ms = info.get("min_size", "?")
        contract_lines.append(f"  `{asset}USDT`: minSize={ms}")

    text = (
        f"*API\\-Diagnose*\n\n"
        f"✅ *Verbunden\\!* Futures\\-Balance: *{bal:.4f} USDT*\n\n"
        f"*Kontrakt\\-Limits \\(Live\\-Assets\\):*\n"
        + "\n".join(contract_lines)
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=persistent_keyboard(),
    )


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /deploy <ID>
    Aktiviert ein Lab-Discovery als parallele Dry-Run-Instanz.
    Berührt NICHT die laufende squeeze/canary-Konfiguration.
    """
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "❌ Nutzung: `/deploy 42`\n"
            "Die ID findest du im `/alpha`\\-Dashboard\\.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    disc_id = int(args[0])
    result  = _db_deploy(disc_id)

    if result.get("error"):
        await update.message.reply_text(
            f"❌ {result['error']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    key    = result["strategy_key"]
    asset  = result["asset"]
    target = result["target_trades"]
    wr     = result["wr_test"]
    wr_str = f"{wr:.1f}%" if wr else "n/a"
    await update.message.reply_text(
        f"✅ *Setup \\#{disc_id} deployed\\!*\n\n"
        f"Instanz: `{key}` \\| Asset: `{asset}`\n"
        f"Modus: `dry_run` \\(parallel zum Canary\\-Test\\)\n\n"
        f"📐 *Dynamisches Ziel:* *{target} Trades*\n"
        f"Backtest WR: {wr_str} → Ziel = max\\(30, ⌈15÷{wr/100:.2f}⌉\\) = {target}\n\n"
        f"Trades erscheinen ab dem nächsten Cron\\-Zyklus unter `strategy='{key}'`\\.\n"
        f"Bei Erreichen von {target} Trades \\+ positivem R\\-Total → Telegram\\-Push\\.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Alpha Dashboard", callback_data="alpha"),
            InlineKeyboardButton("◀️ Menü",            callback_data="back_menu"),
        ]]),
    )


async def handle_keyboard_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet Klicks auf das persistente ReplyKeyboard.

    Sendet den Inhalt mit dem persistenten Keyboard als reply_markup —
    so bleibt das Keyboard dauerhaft sichtbar, auch nach App-Neustarts.
    """
    text = update.message.text
    kb   = persistent_keyboard()   # Keyboard bei jedem Reply mitschicken → niemals verschwinden

    if text == "📊 Dashboard":
        await update.message.reply_text(
            build_dashboard_text(), parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    elif text == "🏆 Alpha Setups":
        await update.message.reply_text(
            _build_alpha_text(), parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    elif text == "⚙️ Status":
        await update.message.reply_text(
            build_status_text(), parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    elif text == "📖 Hilfe":
        await update.message.reply_text(
            _HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    elif text == "💼 Portfolio Empfehlung":
        await cmd_portfolio(update, ctx)
    elif text == "🔌 API Test":
        await cmd_api_test(update, ctx)


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Aktualisieren", callback_data=action),
        InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
    ]])

    if action == "back_menu" or action == "refresh_menu":
        await query.edit_message_text(
            "📋 *Hauptmenü*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )

    elif action == "dashboard":
        await query.edit_message_text(
            build_dashboard_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_btn,
        )

    elif action == "signals":
        await query.edit_message_text(
            build_signals_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_btn,
        )

    elif action == "status":
        await query.edit_message_text(
            build_status_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_btn,
        )

    elif action == "help":
        await query.edit_message_text(
            _HELP_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
            ]]),
        )

    elif action == "alpha":
        await query.edit_message_text(
            _build_alpha_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="alpha"),
                InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
            ]]),
        )

    elif action == "portfolio":
        import asyncio
        portfolio = await asyncio.get_event_loop().run_in_executor(None, _cio_portfolio)
        await query.edit_message_text(
            _build_portfolio_text(portfolio),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_portfolio_keyboard(portfolio),
        )

    elif action.startswith("cio_single_dry:") or action.startswith("cio_single_live:"):
        parts   = action.split(":")
        mode    = "dry_run" if "dry" in parts[0] else "live"
        disc_id = int(parts[1])
        asset   = parts[2]

        if mode == "live":
            # Bestätigung für Live-Einzeldeploy
            await query.edit_message_text(
                f"⚠️ *{asset} live schalten?*\n\n"
                f"Setup \\#{disc_id} wird mit echtem Kapital gehandelt\\.\n"
                f"Bestehende {asset}\\-Instanzen werden gestoppt\\.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"✅ Ja, {asset} LIVE",
                        callback_data=f"cio_confirmed_live:{disc_id}:{asset}",
                    )],
                    [InlineKeyboardButton("❌ Abbrechen", callback_data="portfolio")],
                ]),
            )
        else:
            r = _db_deploy(disc_id, mode="dry_run", replace_asset=True)
            if r.get("ok"):
                msg = (
                    f"🧪 *{asset} Dry\\-Run gestartet*\n\n"
                    f"Instanz: `{r['strategy_key']}`\n"
                    f"Ziel: {r['target_trades']} Trades\n"
                    f"_Vorherige {asset}\\-Instanzen gestoppt\\._"
                )
            else:
                msg = f"⚠️ {r.get('error','Deploy fehlgeschlagen')}"
            await query.edit_message_text(
                msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Portfolio", callback_data="portfolio"),
                    InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
                ]]),
            )

    elif action.startswith("cio_confirmed_live:"):
        parts   = action.split(":")
        disc_id = int(parts[1])
        asset   = parts[2]
        r = _db_deploy(disc_id, mode="live", replace_asset=True)
        if r.get("ok"):
            msg = (
                f"🔴 *{asset} LIVE aktiv*\n\n"
                f"Instanz: `{r['strategy_key']}`\n"
                f"Modus: *LIVE* \\| Ziel: {r['target_trades']} Trades\n"
                f"_Executor handelt ab dem nächsten Signal\\._"
            )
        else:
            msg = f"⚠️ {r.get('error','Deploy fehlgeschlagen')}"
        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("cio_all_live_confirm:"):
        ids_str = action.split(":", 1)[1]
        n       = len(ids_str.split(","))
        await query.edit_message_text(
            f"⚠️ *ALLE {n} Setups live schalten?*\n\n"
            f"Echtes Kapital \\(${56}\\) wird eingesetzt\\.\n"
            f"Alle bestehenden Deployments werden ersetzt\\.\n\n"
            f"_Diese Aktion kann nicht automatisch rückgängig gemacht werden\\._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🔥 Ja, alle {n} LIVE",
                    callback_data=f"cio_all_live_execute:{ids_str}",
                )],
                [InlineKeyboardButton("❌ Abbrechen", callback_data="portfolio")],
            ]),
        )

    elif action.startswith("cio_all_live_execute:"):
        ids     = [int(x) for x in action.split(":", 1)[1].split(",") if x]
        results = [_db_deploy(i, mode="live", replace_asset=True) for i in ids]
        ok      = sum(1 for r in results if r.get("ok"))
        lines   = [f"🔴 *CIO All\\-Live Deploy*\n"]
        for r in results:
            if r.get("ok"):
                lines.append(
                    f"✅ `{r['asset']}` → `{r['strategy_key']}` "
                    f"\\| Ziel: {r['target_trades']} Trades"
                )
            else:
                lines.append(f"⚠️ {r.get('error','?')}")
        lines.append(f"\n_{ok}/{len(ids)} Setups live\\. Executor wartet auf Signale\\._")
        await query.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Push-Jobs (job_queue)
# ══════════════════════════════════════════════════════════════════════════════

async def push_new_trades(ctx: ContextTypes.DEFAULT_TYPE):
    """Alle 2 Minuten: prüfe auf neue abgeschlossene Trades → Push."""
    since_key = "push_last_trade_ts"
    last_ts   = ctx.bot_data.get(since_key)
    now_iso   = datetime.now(timezone.utc).isoformat()

    if last_ts is None:
        ctx.bot_data[since_key] = now_iso
        return

    new = _db_new_executed_since(last_ts)
    if new:
        ctx.bot_data[since_key] = now_iso
        for t in new:
            r_val = t["pnl_r"] or 0
            icon  = "🟢" if r_val > 0 else "🔴"
            dir_i = "📈" if t["direction"] == "long" else "📉"
            mode  = f"[{t['mode']}]" if t["mode"] != "live" else ""
            msg   = (
                f"{icon} *Trade geschlossen* {mode}\n"
                f"`{t['strategy']}/{t['asset']}` {dir_i} "
                f"@ {t['entry_price']} → {t['exit_price']}\n"
                f"PnL: *{_fmt_r(r_val)}*  |  Exit: `{t['exit_reason']}`"
            )
            await ctx.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg,
                parse_mode=ParseMode.MARKDOWN,
            )


async def push_heartbeat_alert(ctx: ContextTypes.DEFAULT_TYPE):
    """Alle 5 Minuten: prüfe auf kritische Heartbeat-Ausfälle."""
    hbs  = _db_heartbeats()
    now  = datetime.now(timezone.utc)
    dead = []

    for hb in hbs:
        comp    = hb["component"]
        max_min = HEARTBEAT_MAX_AGE_MIN.get(comp, 30)
        try:
            ts  = datetime.fromisoformat(hb["ts"])
            age = (now - ts).total_seconds() / 60
        except Exception:
            age = 999.0
        if age > max_min * 2:
            dead.append(f"`{comp}` seit {int(age)}min tot")

    if not dead:
        return

    alerted_key = "hb_alert_sent"
    last_alert  = ctx.bot_data.get(alerted_key)
    # Nur einmal pro 30 Minuten warnen
    if last_alert and (now - last_alert).total_seconds() < 1800:
        return

    ctx.bot_data[alerted_key] = now
    msg = "🚨 *SYSTEM-ALARM* — Heartbeat-Ausfall\n\n" + "\n".join(dead)
    await ctx.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN,
    )


async def push_daily_status(ctx: ContextTypes.DEFAULT_TYPE):
    """Täglicher Status-Report (wird vom job_queue um 08:00 UTC getriggert)."""
    d    = _db_pnl_summary()
    can  = _db_canary()
    now  = datetime.now(timezone.utc)

    lines = [f"📅 *Tagesstatus — {now.strftime('%Y-%m-%d')}*\n"]
    lines.append(f"Trades: *{d['total_n']}*  |  Gesamt: *{_fmt_r(d['total_r'])}*")
    lines.append(f"Heute: *{_fmt_r(d['today_r'])}*  |  Ø Avg R: *{_fmt_r(d['avg_r'])}*\n")

    lines.append("*🐦 Squeeze Canary:*")
    for asset, ref in LAB_REF.items():
        target = _canary_target(asset)
        c      = can.get(asset)
        n      = c["n"]      if c else 0
        avg    = c["avg_r"]  if c else 0.0
        t_r    = c["total_r"] if c else 0.0
        disc   = _fmt_disc(avg, ref["avg_r"]) if n > 0 else ""
        pct    = min(int(n / target * 100), 100) if target else 0
        badge  = ("🟢" if t_r > 0 else "🔴") if n >= target else "⏳"
        lines.append(f"  {badge} `{asset}` {n}/{target} Trades \\({pct}%\\)  {_fmt_r(t_r)} {disc}")

    await ctx.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


async def push_go_live_check(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Alle 15 Minuten: prüft ob ein Deployment seinen Forward-Test bestanden hat.
    Kriterium: n >= target_trades UND total_r > 0
    Push wird pro Deployment nur EINMAL gesendet (go_live_notified-Flag).
    """
    from core.db import get_state, set_state

    async def _send_verdict(sk: str, asset: str, regime: str,
                            n: int, target: int, total_r: float,
                            wins: int) -> None:
        losses = n - wins
        pf     = (wins / losses) if losses > 0 else 999.0
        wr     = round(wins / n * 100, 1) if n > 0 else 0
        if total_r > 0:
            msg = (
                f"🟢 *Go\\-Live Freigabe\\!*\n\n"
                f"Instanz `{sk}` hat den Forward\\-Test bestanden\\!\n\n"
                f"*Asset:* `{asset}`  \\|  *Regime:* `{regime}`\n"
                f"*Trades:* {n}/{target}  \\|  *Total R:* *{_fmt_r(total_r)}*\n"
                f"*PF:* *{pf:.2f}*  \\|  *WR:* *{wr}%*\n\n"
                f"✅ Bereit für Live\\-Modus\\!"
            )
        else:
            msg = (
                f"🔴 *Forward\\-Test nicht bestanden*\n\n"
                f"`{sk}` — {target} Trades, aber kein positives R\\.\n\n"
                f"*Total R:* *{_fmt_r(total_r)}*  \\|  *PF:* *{pf:.2f}*  \\|  *WR:* *{wr}%*\n\n"
                f"Empfehlung: Deployment deaktivieren oder Regime abwarten\\."
            )
        await ctx.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN,
        )

    # ── 1. Deployments prüfen ────────────────────────────────────────────────
    deps = _db_active_deployments()
    conn = get_connection()
    for dep in deps:
        if dep["go_live_notified"]:
            continue
        n, target, total_r = dep["n"], dep["target_trades"], dep["total_r"]
        if n < target:
            continue
        await _send_verdict(dep["strategy_key"], dep["asset"], dep["regime"],
                            n, target, total_r, dep["wins"])
        conn.execute(
            "UPDATE active_deployments SET go_live_notified=1 WHERE strategy_key=?",
            (dep["strategy_key"],),
        )
        conn.commit()
    conn.close()

    # ── 2. Standard-Canary pro Asset prüfen ─────────────────────────────────
    # Notified-Flag via system_state: "canary_go_live_notified_ETH" = "1"
    can = _db_canary()
    for asset in LAB_REF:
        state_key = f"canary_go_live_notified_{asset}"
        if get_state(state_key) == "1":
            continue
        target  = _canary_target(asset)
        c       = can.get(asset)
        n       = c["n"]       if c else 0
        total_r = c["total_r"] if c else 0.0
        wins    = c["wins"]    if c else 0
        if n < target:
            continue
        await _send_verdict(f"squeeze/{asset}", asset, "Canary",
                            n, target, total_r, wins)
        set_state(state_key, "1")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("FEHLER: TELEGRAM_BOT_TOKEN nicht gesetzt.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        print("FEHLER: TELEGRAM_CHAT_ID nicht gesetzt.")
        sys.exit(1)

    print(f"[BOT] Starte APEX V2 Telegram-Bot (Token: ...{TELEGRAM_BOT_TOKEN[-6:]})")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pnl",    cmd_pnl))
    app.add_handler(CommandHandler("lab",    cmd_lab))
    app.add_handler(CommandHandler("fetch",  cmd_fetch))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("alpha",    cmd_alpha))
    app.add_handler(CommandHandler("deploy",   cmd_deploy))
    app.add_handler(CommandHandler("api_test",  cmd_api_test))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CallbackQueryHandler(button_callback))

    # Persistentes ReplyKeyboard — Textnachrichten der Buttons abfangen
    _kb_buttons = {
        "📊 Dashboard", "🏆 Alpha Setups", "💼 Portfolio Empfehlung",
        "⚙️ Status", "🔌 API Test", "📖 Hilfe",
    }
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(" + "|".join(_kb_buttons) + ")$"),
        handle_keyboard_button,
    ))

    # Job Queue
    jq = app.job_queue

    # Neue Trades: alle 2 Minuten prüfen
    jq.run_repeating(push_new_trades, interval=120, first=10)

    # Heartbeat-Check: alle 5 Minuten
    jq.run_repeating(push_heartbeat_alert, interval=300, first=30)

    # Tagesstatus: täglich 08:00 UTC
    jq.run_daily(push_daily_status, time=datetime.strptime("08:00", "%H:%M").time())

    # Go-Live Check: alle 15 Minuten
    jq.run_repeating(push_go_live_check, interval=900, first=60)

    print("[BOT] Polling gestartet — Ctrl+C zum Beenden")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
