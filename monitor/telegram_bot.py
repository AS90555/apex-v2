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

from core.db import get_connection, DB_PATH
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RISK_USDT


def _is_authorized(update) -> bool:
    """Nur der konfigurierte Chat darf Commands ausführen.
    Fail-CLOSED: leere/fehlende CHAT_ID blockiert ALLE Zugriffe.
    """
    allowed = str(TELEGRAM_CHAT_ID).strip()
    if not allowed:
        return False  # fail-CLOSED — kein Zugriff ohne konfigurierte CHAT_ID
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == allowed or user_id == allowed


def _escape_md(text: str) -> str:
    for ch in r'_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text


def _p(text: str) -> str:
    """Post-Escape für MarkdownV2: escapt vergessene Sonderzeichen, lässt bereits
    escapte Sequenzen (\\X) und Markdown-Marker (* _ ` [ ]) unberührt."""
    _DANGER = frozenset('.()+!|{}~>#=-')
    result = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '\\' and i + 1 < len(text):
            result.append(c)
            result.append(text[i + 1])
            i += 2
        elif c in _DANGER:
            result.append('\\')
            result.append(c)
            i += 1
        else:
            result.append(c)
            i += 1
    return ''.join(result)




# ── Portfolio-Manager DB helpers ──────────────────────────────────────────────

def _pm_summary() -> dict:
    conn = get_connection()
    total  = conn.execute("SELECT COUNT(*) FROM lab_discoveries").fetchone()[0]
    live   = conn.execute("SELECT COUNT(*) FROM lab_discoveries WHERE deployment_status='live'").fetchone()[0]
    dry    = conn.execute("SELECT COUNT(*) FROM lab_discoveries WHERE deployment_status='dry'").fetchone()[0]
    top    = conn.execute(
        "SELECT id, strategy, asset, micro_score FROM lab_discoveries ORDER BY micro_score DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {"total": total, "live": live, "dry": dry, "top": dict(top) if top else None}


def _pm_list_by(field: str) -> list[dict]:
    assert field in ("asset", "strategy", "market_regime")
    conn = get_connection()
    rows = conn.execute(
        f"""SELECT {field} AS key,
               COUNT(*) AS n,
               AVG(micro_score) AS avg_ms,
               SUM(CASE WHEN deployment_status='live' THEN 1 ELSE 0 END) AS n_live,
               SUM(CASE WHEN deployment_status='dry'  THEN 1 ELSE 0 END) AS n_dry,
               MAX(n_test) AS best_n_test
            FROM lab_discoveries
            GROUP BY {field}
            ORDER BY avg_ms DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _pm_top(n: int = 10) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, strategy, asset, market_regime, micro_score, deployment_status, n_test
           FROM lab_discoveries
           ORDER BY micro_score DESC LIMIT ?""", (n,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _pm_active() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT d.id, d.strategy, d.asset, d.market_regime, d.micro_score,
                  d.deployment_status, d.n_test, a.mode, a.active
           FROM lab_discoveries d
           JOIN active_deployments a ON a.discovery_id = d.id
           WHERE a.active = 1
           ORDER BY d.asset, d.strategy"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _pm_detail(disc_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        """SELECT id, strategy, asset, market_regime, micro_score,
                  deployment_status, deployed_at, deployed_by,
                  n_test, avg_r_test, wr_test, pf_test, max_dd_r,
                  n_train, pf_train, avg_r_train, params_json
           FROM lab_discoveries WHERE id=?""", (disc_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _pm_list_for(field: str, value: str) -> list[dict]:
    assert field in ("asset", "strategy", "market_regime")
    conn = get_connection()
    rows = conn.execute(
        f"""SELECT id, strategy, asset, market_regime, micro_score, deployment_status, n_test
            FROM lab_discoveries WHERE {field}=?
            ORDER BY micro_score DESC LIMIT 20""", (value,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Portfolio-Manager Hilfsfunktionen ────────────────────────────────────────

def _pm_freq_label(n_test: int) -> str:
    """Menschlich lesbare Trade-Frequenz aus n_test (OOS-Fenster = 60 Tage).
    Gibt unescapten Plain-Text zurück — Caller rufen _escape_md() drauf."""
    if not n_test or n_test <= 0:
        return "Frequenz unbekannt"
    per_month = n_test / 2.0
    if per_month > 60:
        return f"täglich aktiv (~{per_month:.0f}×/Monat)"
    if per_month > 8:
        per_week = per_month / 4.33
        return f"~{per_week:.1f}× pro Woche"
    if per_month > 2:
        return f"~{per_month:.0f}× pro Monat"
    per_week = per_month / 4.33
    if per_week >= 0.5:
        return "~alle 2 Wochen"
    weeks = 4.33 / per_month
    return f"~alle {weeks:.0f} Wochen"


def _pm_score_label(score: float) -> str:
    """Qualitäts-Label für Composite-Score (Skala ~2–33, Median ~5.7, P90 ~13)."""
    if score >= 22:
        return "🌟 Exzellent \\(Top\\-3%\\)"
    if score >= 13:
        return "✅ Gut \\(Top\\-10%\\)"
    if score >= 6:
        return "⚠️ Brauchbar \\(Mittelfeld\\)"
    return "🔻 Schwach"


# ── Display-Helpers (reine Übersetzung, keine Logik) ─────────────────────────

_REGIME_DE = {
    "TREND_UP":   "Aufwärts-Trend",
    "TREND_DOWN": "Abwärts-Trend",
    "SIDEWAYS":   "Seitwärts",
    "UNKNOWN":    "Unbekannt",
}

_REGIME_SHORT = {
    "TREND_UP":   "↑ Aufwärts",
    "TREND_DOWN": "↓ Abwärts",
    "SIDEWAYS":   "↔ Seitwärts",
    "UNKNOWN":    "?",
}


def _regime_label(regime: str) -> str:
    return _REGIME_DE.get(regime, regime)


def _regime_short(regime: str) -> str:
    return _REGIME_SHORT.get(regime, regime)


def _deployment_label(dep: dict) -> str:
    """Lesbarer Name aus asset + regime — niemals strategy_key parsen."""
    asset  = dep.get("asset", "?")
    regime = _regime_label(dep.get("regime") or dep.get("market_regime") or "")
    return f"{asset} · {regime}"


_EXIT_REASON_DE = {
    "sl":         "Stop-Loss",
    "sl_hit":     "Stop-Loss",
    "tp1":        "Ziel 1 erreicht",
    "tp1_hit":    "Ziel 1 erreicht",
    "tp2":        "Ziel 2 erreicht",
    "tp2_hit":    "Ziel 2 erreicht",
    "timeout":    "Zeit abgelaufen",
    "invalid_sl": "Ungültiger Stop",
    "manual":     "Manuell",
}


def _exit_label(reason: str) -> str:
    return _escape_md(_EXIT_REASON_DE.get(reason, reason or "?"))


_COMPONENT_DE = {
    "intake":     "Marktdaten",
    "intake_ws":  "Marktdaten WS",
    "features":   "Analyse",
    "strategies": "Strategien",
    "governance": "Steuerung",
    "executor":   "Ausführung",
    "monitor":    "Überwachung",
}


def _component_label(comp: str) -> str:
    return _COMPONENT_DE.get(comp, comp)


def _score_bar(score: float, max_score: float = 33.0, width: int = 8) -> str:
    filled = round(min(score / max_score, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _score_10(score: float, max_score: float = 33.0) -> str:
    return f"{min(score / max_score * 10, 10.0):.1f}/10"


def _r_to_usd(r: float) -> str:
    usd = r * RISK_USDT
    if usd >= 0:
        return _escape_md(f"+${usd:.2f}")
    return _escape_md(f"-${abs(usd):.2f}")


def _mode_label(mode: str) -> str:
    return "LIVE" if mode == "live" else "TEST-LAUF"


# ── Ende Display-Helpers ──────────────────────────────────────────────────────


def _pm_best_alternative(disc_id: int, strategy: str, asset: str,
                          regime: str, current_score: float,
                          current_pf: float = 0.0, current_wr: float = 0.0,
                          current_avgr: float = 0.0, current_n: int = 0) -> dict | None:
    """
    Bestes alternatives Setup in derselben (strategy, asset, regime)-Gruppe.
    Nutzt Hurdle-Filter: Kandidat muss in ALLEN praktischen Metriken mindestens
    so gut sein wie das aktuelle Setup (Toleranz: PF/AvgR -5%, WR -3pp, n -20%).
    Holt Top-5 und iteriert — verhindert LIMIT-1-Blindheit.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, micro_score, pf_test, avg_r_test, wr_test, n_test, max_dd_r, params_json
           FROM lab_discoveries
           WHERE strategy=? AND asset=? AND market_regime=?
             AND id != ? AND micro_score > ?
           ORDER BY micro_score DESC LIMIT 5""",
        (strategy, asset, regime, disc_id, current_score),
    ).fetchall()
    conn.close()

    # Hurdle-Schwellen (Toleranz gegen kleine Unterschiede)
    min_pf   = current_pf   * 0.95 if current_pf   > 0 else 0.0
    min_avgr = current_avgr * 0.95 if current_avgr > 0 else 0.0
    min_wr   = current_wr   - 3.0  if current_wr   > 0 else 0.0
    min_n    = int(current_n * 0.80) if current_n  > 0 else 0

    for row in rows:
        r = dict(row)
        if (r.get("pf_test", 0)    >= min_pf   and
                r.get("avg_r_test", 0) >= min_avgr and
                r.get("wr_test", 0)    >= min_wr   and
                r.get("n_test", 0)     >= min_n):
            return r
    return None


# ── Portfolio-Manager Keyboards & Views ───────────────────────────────────────

def _pm_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Nach Asset",      callback_data="pm_by_asset"),
            InlineKeyboardButton("🧠 Nach Strategie",  callback_data="pm_by_strategy"),
        ],
        [
            InlineKeyboardButton("🌍 Nach Regime",     callback_data="pm_by_regime"),
            InlineKeyboardButton("🏆 Top-Gesamt",      callback_data="pm_top"),
        ],
        [
            InlineKeyboardButton("📈 Aktiv deployed",  callback_data="pm_active"),
            InlineKeyboardButton("🌡️ Regime-Fit",      callback_data="pm_regimefit"),
        ],
        [InlineKeyboardButton("◀️ Menü",               callback_data="back_menu")],
    ])


def _build_pm_main_text() -> str:
    s = _pm_summary()
    top = s["top"]
    if top:
        top_strat = _escape_md(top["strategy"])
        top_asset = _escape_md(top["asset"])
        top_ms    = _escape_md(f"{top['micro_score']:.3f}")
        top_line  = f"\n🥇 Bester: `{top_strat}/{top_asset}` \\(MS {top_ms}\\)"
    else:
        top_line = ""
    return (
        f"📊 *Portfolio Manager*\n\n"
        f"Discoveries: {s['total']} \\| 💰 Live: {s['live']} \\| ⚙️ Dry: {s['dry']}{top_line}\n\n"
        f"Wähle eine Ansicht:"
    )


def _build_pm_group_list(field: str, rows: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    label = {"asset": "Asset", "strategy": "Strategie", "market_regime": "Regime"}[field]
    cb_prefix = {"asset": "pm_asset_", "strategy": "pm_strat_", "market_regime": "pm_regime_"}[field]
    lines = [f"📋 *Nach {_escape_md(label)}*\n"]
    buttons = []
    for r in rows:
        key = r["key"] or "–"
        status = ""
        if r["n_live"]: status += f" 💰{r['n_live']}"
        if r["n_dry"]:  status += f" ⚙️{r['n_dry']}"
        avg_ms_str = _escape_md(f"{r['avg_ms']:.1f}")
        freq = _escape_md(_pm_freq_label(r.get("best_n_test") or 0))
        lines.append(
            f"`{_escape_md(key)}` — {r['n']} Setups, Score {avg_ms_str}{_escape_md(status)}\n"
            f"  🕐 Bestes Setup: {freq}"
        )
        buttons.append([InlineKeyboardButton(
            f"{key} ({r['n']})", callback_data=f"{cb_prefix}{key}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")])
    return _p("\n".join(lines)), InlineKeyboardMarkup(buttons)


def _build_pm_item_list(field: str, value: str, rows: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"🔍 *{_escape_md(field.capitalize())}: {_escape_md(value)}*\n"]
    buttons = []
    for r in rows:
        icon = {"live": "💰", "dry": "⚙️"}.get(r["deployment_status"], "🔬")
        ms_str = _escape_md(f"{r['micro_score']:.1f}")
        freq   = _escape_md(_pm_freq_label(r.get("n_test") or 0))
        lines.append(
            f"{icon} \\#{r['id']} `{_escape_md(r['strategy'])}/{_escape_md(r['asset'])}` "
            f"Score {ms_str} \\| 🕐 {freq}"
        )
        buttons.append([InlineKeyboardButton(
            f"#{r['id']} {r['strategy']}/{r['asset']}", callback_data=f"pm_detail_{r['id']}"
        )])
    back_cb = {"asset": "pm_by_asset", "strategy": "pm_by_strategy", "market_regime": "pm_by_regime"}[field]
    buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data=back_cb)])
    return _p("\n".join(lines)), InlineKeyboardMarkup(buttons)


def _build_pm_top_text(rows: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🏆 *Top\\-10 Discoveries*\n"]
    buttons = []
    for i, r in enumerate(rows, 1):
        icon = {"live": "💰", "dry": "⚙️"}.get(r["deployment_status"], "🔬")
        ms_str = _escape_md(f"{r['micro_score']:.1f}")
        freq   = _escape_md(_pm_freq_label(r.get("n_test") or 0))
        lines.append(
            f"{i}\\. {icon} \\#{r['id']} `{_escape_md(r['strategy'])}/{_escape_md(r['asset'])}` "
            f"Score {ms_str} \\| 🕐 {freq}"
        )
        buttons.append([InlineKeyboardButton(
            f"#{r['id']} {r['strategy']}/{r['asset']}", callback_data=f"pm_detail_{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")])
    return _p("\n".join(lines)), InlineKeyboardMarkup(buttons)


def _regime_fit_data() -> list[dict]:
    """
    Liest pro aktivem Deployment: aktuelles Regime, Training-Regime, Fit-Status,
    Regime-Alter und bestes verfügbares Setup für das aktuelle Regime.
    """
    from core.db import get_state
    from datetime import datetime, timezone

    conn = get_connection()
    rows = conn.execute(
        """SELECT a.asset, a.mode, a.discovery_id, a.strategy_key,
                  d.market_regime AS training_regime,
                  COALESCE(d.micro_score, 0) AS micro_score,
                  d.strategy, d.n_test
           FROM active_deployments a
           JOIN lab_discoveries d ON d.id = a.discovery_id
           WHERE a.active = 1
           ORDER BY a.asset"""
    ).fetchall()
    # Regime-Alter aus system_state
    age_rows = conn.execute(
        "SELECT key, updated_at FROM system_state WHERE key LIKE 'regime_%'"
    ).fetchall()
    conn.close()

    age_map: dict[str, float] = {}
    now_utc = datetime.now(timezone.utc)
    for key, updated_at in age_rows:
        asset = key[len("regime_"):]
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_map[asset] = (now_utc - ts).total_seconds() / 60
        except Exception:
            age_map[asset] = 999.0

    result = []
    for r in rows:
        asset           = r["asset"]
        current_regime  = get_state(f"regime_{asset}") or "UNKNOWN"
        training_regime = r["training_regime"] or "UNKNOWN"
        fit             = (current_regime == training_regime and current_regime != "UNKNOWN")
        regime_age_min  = age_map.get(asset, 999.0)
        best_alt        = None
        if not fit and current_regime not in ("UNKNOWN",):
            best_alt = _cio_best_setup(asset, current_regime)
        result.append({
            "asset":           asset,
            "mode":            r["mode"],
            "discovery_id":    r["discovery_id"],
            "strategy_key":    r["strategy_key"],
            "strategy":        r["strategy"],
            "n_test":          r["n_test"],
            "micro_score":     r["micro_score"],
            "current_regime":  current_regime,
            "training_regime": training_regime,
            "fit":             fit,
            "regime_age_min":  regime_age_min,
            "best_alt":        best_alt,
        })
    return result


def _build_pm_active_text(rows: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    if not rows:
        return "📈 *Aktiv deployed*\n\nKein aktives Deployment\\.", InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")]
        ])

    fit_data = {f["discovery_id"]: f for f in _regime_fit_data()}

    lines = ["📈 *Aktiv deployed*\n"]
    buttons = []
    for r in rows:
        mode_icon = "💰" if r["mode"] == "live" else "⚙️"
        ms_str = _escape_md(f"{r['micro_score']:.1f}")
        freq   = _escape_md(_pm_freq_label(r.get("n_test") or 0))
        lines.append(
            f"{mode_icon} \\#{r['id']} `{_escape_md(r['strategy'])}/{_escape_md(r['asset'])}` "
            f"Score {ms_str} \\| 🕐 {freq}"
        )
        fd = fit_data.get(r["id"])
        if fd:
            fit_icon = "🟢" if fd["fit"] else "🔴"
            age_str  = _escape_md(f"{fd['regime_age_min']:.0f}")
            stale    = " ⚠️" if fd["regime_age_min"] > 15 else ""
            cr_short = _escape_md(_regime_short(fd["current_regime"]))
            tr_short = _escape_md(_regime_short(fd["training_regime"]))
            lines.append(
                f"   {fit_icon} Markt: `{cr_short}`{_escape_md(stale)} \\| Trainiert: `{tr_short}` \\| vor {age_str} Min"
            )
            if not fd["fit"] and fd["best_alt"]:
                alt = fd["best_alt"]
                alt_ms = _escape_md(f"{alt['micro_score']:.1f}")
                alt_id = alt["id"]
                lines.append(
                    f"   💡 Besser: \\#{alt_id} `{_escape_md(alt['strategy'])}` \\(MS {alt_ms}\\)"
                )
        buttons.append([InlineKeyboardButton(
            f"#{r['id']} {r['strategy']}/{r['asset']}", callback_data=f"pm_detail_{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("🌡️ Regime-Fit", callback_data="pm_regimefit")])
    buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")])
    return _p("\n".join(lines)), InlineKeyboardMarkup(buttons)


def _build_pm_regimefit_text() -> tuple[str, InlineKeyboardMarkup]:
    """Kompaktübersicht: aktuelles Regime vs. Training-Regime je Deployment."""
    fit_list = _regime_fit_data()

    if not fit_list:
        return (
            "🌡️ *Regime\\-Fit Übersicht*\n\nKein aktives Deployment\\.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")]]),
        )

    lines = ["🌡️ *Regime\\-Fit Übersicht*\n"]
    buttons = []
    n_match   = sum(1 for f in fit_list if f["fit"])
    n_total   = len(fit_list)
    n_mismatch = n_total - n_match

    for f in fit_list:
        fit_icon  = "🟢" if f["fit"] else "🔴"
        mode_icon = "💰" if f["mode"] == "live" else "⚙️"
        age_str   = _escape_md(f"{f['regime_age_min']:.0f}")
        stale_sfx = _escape_md(" ⚠️ veraltet") if f["regime_age_min"] > 15 else ""
        cr_short  = _escape_md(_regime_short(f["current_regime"]))
        tr_short  = _escape_md(_regime_short(f["training_regime"]))
        asset_s   = _escape_md(f["asset"])

        lines.append(
            f"{fit_icon} {mode_icon} *{asset_s}*  "
            f"Markt: `{cr_short}`{stale_sfx}  ·  Trainiert: `{tr_short}`  \\(vor {age_str} Min\\)"
        )

        if not f["fit"] and f["best_alt"]:
            alt    = f["best_alt"]
            alt_ms = _escape_md(f"{alt['micro_score']:.1f}")
            alt_s  = _escape_md(alt["strategy"])
            lines.append(
                f"   💡 Besser jetzt: \\#{alt['id']} `{alt_s}` MS {alt_ms}"
            )
            buttons.append([InlineKeyboardButton(
                f"🔄 {f['asset']}: #{alt['id']} wechseln",
                callback_data=f"pm_detail_{alt['id']}",
            )])
        elif not f["fit"]:
            lines.append(f"   ⚪ _Kein validiertes Setup für aktuelles Regime im Lab_")

    lines.append("")
    if n_mismatch == 0:
        lines.append(f"✅ *{n_match}/{n_total} Strategien im Regime\\-Match* — kein Handlungsbedarf\\.")
    else:
        lines.append(
            f"⚠️ *{n_mismatch}/{n_total} Mismatch* — "
            f"{_escape_md(str(n_match))} passend, {_escape_md(str(n_mismatch))} nicht\\."
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n_\\[\\.\\.\\.\\]_"

    buttons.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="pm_regimefit")])
    buttons.append([InlineKeyboardButton("◀️ Zurück",        callback_data="pm_main")])
    return text, InlineKeyboardMarkup(buttons)


def _pm_metric_label(value, good_threshold, ok_threshold, higher_is_better=True) -> str:
    """Gibt ✅ / ⚠️ / ❌ zurück je nach Schwellenwert."""
    if value is None:
        return "❓"
    if higher_is_better:
        if value >= good_threshold:
            return "✅"
        if value >= ok_threshold:
            return "⚠️"
        return "❌"
    else:
        if value <= good_threshold:
            return "✅"
        if value <= ok_threshold:
            return "⚠️"
        return "❌"


def _build_pm_detail_text(r: dict) -> tuple[str, InlineKeyboardMarkup]:
    import json as _json

    status      = r.get("deployment_status", "lab")
    status_icon = {"live": "💰 LIVE", "dry": "⚙️ DRY\\-RUN", "lab": "🔬 Lab"}.get(status, status)
    disc_id     = r["id"]

    # ── Rohwerte sicher lesen ────────────────────────────────────────────────
    pf_test   = r.get("pf_test")   or 0.0
    avg_r     = r.get("avg_r_test") or 0.0
    wr        = r.get("wr_test")   or 0.0
    n_test    = r.get("n_test")    or 0
    max_dd    = r.get("max_dd_r")  or 0.0
    micro     = r.get("micro_score") or 0.0
    pf_train  = r.get("pf_train")  or 0.0
    n_train   = r.get("n_train")   or 0

    # ── Dollar-Beträge (basierend auf aktuellem RISK_USDT) ───────────────────
    avg_r_usd = avg_r * RISK_USDT
    dd_usd    = max_dd * RISK_USDT

    # ── Parameter kompakt (max 4 Schlüssel anzeigen) ─────────────────────────
    params_line = ""
    try:
        params = _json.loads(r.get("params_json") or "{}")
        if params:
            parts = [f"{k}={v}" for k, v in list(params.items())[:4]]
            params_line = _escape_md(" | ".join(parts))
    except Exception:
        pass

    # ── Bewertungs-Labels ────────────────────────────────────────────────────
    lbl_pf  = _pm_metric_label(pf_test, 1.5,  1.2,  higher_is_better=True)
    lbl_avgr= _pm_metric_label(avg_r,   0.10,  0.05, higher_is_better=True)
    lbl_wr  = _pm_metric_label(wr,      50.0,  45.0, higher_is_better=True)
    lbl_n   = _pm_metric_label(n_test,  30,    20,   higher_is_better=True)
    lbl_dd  = _pm_metric_label(max_dd,  3.0,   6.0,  higher_is_better=False)

    # ── Alle float-Werte vorab escapen (Python 3.12: kein \\ in f-strings) ──
    ms_s      = _escape_md(f"{micro:.1f}")
    score_lbl = _pm_score_label(micro)
    pf_s      = _escape_md(f"{pf_test:.2f}")
    pf_tr_s   = _escape_md(f"{pf_train:.2f}")
    avgr_s    = _escape_md(f"{avg_r:+.2f}")
    avgr_usd_s= _escape_md(f"{avg_r_usd:+.2f}")
    wr_s      = _escape_md(f"{wr:.0f}")
    dd_s      = _escape_md(f"{max_dd:.1f}")
    dd_usd_s  = _escape_md(f"{dd_usd:.2f}")
    risk_s    = _escape_md(f"{RISK_USDT:.2f}")

    deployed_line = ""
    if r.get("deployed_at"):
        deployed_line = f"\nDeployed: `{_escape_md(r['deployed_at'][:10])}`"

    freq_s = _escape_md(_pm_freq_label(n_test))

    text = (
        f"*Setup \\#{disc_id}* — {status_icon}{deployed_line}\n"
        f"`{_escape_md(r['strategy'])}` / `{_escape_md(r['asset'])}` / "
        f"`{_escape_md(str(r.get('market_regime') or '–'))}`\n"
        f"Gesamt\\-Score: *{ms_s}* — {score_lbl}\n"
        f"\n"
        f"*📊 Profit\\-Faktor: {pf_s}* {lbl_pf}\n"
        f"Für jeden \\$ Verlust kommen {pf_s}\\$ zurück\n"
        f"Ziel: \\>1\\.5 \\| Training: {pf_tr_s} \\({n_train} Trades\\)\n"
        f"\n"
        f"*📈 Ø Gewinn pro Trade: {avgr_s}R* {lbl_avgr}\n"
        f"Entspricht ca\\. \\${avgr_usd_s} bei \\${risk_s} Risiko\n"
        f"Ziel: \\>\\+0\\.10R\n"
        f"\n"
        f"*🎯 Trefferquote: {wr_s}%* {lbl_wr}\n"
        f"{wr_s} von 100 Trades waren gewinnend\n"
        f"Ziel: \\>50%\n"
        f"\n"
        f"*📉 Max\\. Einbruch: \\-{dd_s}R* {lbl_dd}\n"
        f"Größter Rückgang: ca\\. \\-\\${dd_usd_s} bei \\${risk_s} Risiko\n"
        f"Ziel: \\<3R\n"
        f"\n"
        f"*🔬 Getestet an: {n_test} Trades* {lbl_n}\n"
        f"Ziel: \\≥30 für statistische Aussagekraft\n"
        f"\n"
        f"*🕐 Wann kommt der nächste Trade?*\n"
        f"{freq_s}"
    )
    if params_line:
        text += f"\n\n*Parameter:* `{params_line}`"

    # ── Vergleich: gibt es ein besseres Setup in derselben Gruppe? ───────────
    best_alt = _pm_best_alternative(
        disc_id, r["strategy"], r.get("asset", ""),
        r.get("market_regime", ""), micro,
        current_pf=pf_test, current_wr=wr, current_avgr=avg_r, current_n=n_test,
    )
    if best_alt is not None:
        try:
            alt_ms_s   = _escape_md(f"{best_alt['micro_score']:.1f}")
            alt_pf_s   = _escape_md(f"{best_alt.get('pf_test', 0):.2f}")
            alt_avgr_s = _escape_md(f"{best_alt.get('avg_r_test', 0):+.2f}")
            alt_wr_s   = _escape_md(f"{best_alt.get('wr_test', 0):.0f}")
            alt_id     = best_alt["id"]
            # Parameter-Diff: geänderte Keys anzeigen
            diff_parts = []
            try:
                import json as _json2
                cur_p = _json2.loads(r.get("params_json") or "{}")
                alt_p = _json2.loads(best_alt.get("params_json") or "{}")
                for k in cur_p:
                    if k in alt_p and cur_p[k] != alt_p[k]:
                        diff_parts.append(f"{k}: {cur_p[k]}→{alt_p[k]}")
                diff_line = _escape_md(", ".join(diff_parts[:3])) if diff_parts else "andere Parameter"
            except Exception:
                diff_line = "andere Parameter"
            text += (
                f"\n\n*⚠️ Besseres Setup verfügbar:* \\#{alt_id}\n"
                f"Score: {ms_s} → *{alt_ms_s}* ▲\n"
                f"PF: {pf_s} → {alt_pf_s} \\| "
                f"AvgR: {avgr_s} → {alt_avgr_s} \\| "
                f"WR: {wr_s}% → {alt_wr_s}%\n"
                f"Unterschied: _{diff_line}_"
            )
            buttons = []
            if status != "live":
                buttons.append([InlineKeyboardButton("💰 Als LIVE deployen", callback_data=f"deploy_live_{disc_id}")])
            if status == "live":
                buttons.append([InlineKeyboardButton("⬇️ Zu Dry-Run herabstufen", callback_data=f"deploy_dry_confirm_{disc_id}")])
            elif status != "dry":
                buttons.append([InlineKeyboardButton("⚙️ Als Dry-Run starten", callback_data=f"deploy_dry_confirm_{disc_id}")])
            if status in ("live", "dry"):
                buttons.append([InlineKeyboardButton("⏸ Pausieren", callback_data=f"deploy_pause_{disc_id}")])
            buttons.append([InlineKeyboardButton(f"🔄 Setup #{alt_id} ansehen", callback_data=f"pm_detail_{alt_id}")])
            buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")])
        except Exception:
            # Fallback: Vergleichsblock überspringen, normale Buttons
            best_alt = None

    if best_alt is None:
        text += f"\n\n*✅ Aktuell bestes Setup*\nKein besseres in `{_escape_md(r['strategy'])}/{_escape_md(r.get('asset',''))}/{_escape_md(str(r.get('market_regime') or ''))}`"
        buttons = []
        if status != "live":
            buttons.append([InlineKeyboardButton("💰 Als LIVE deployen", callback_data=f"deploy_live_{disc_id}")])
        if status == "live":
            buttons.append([InlineKeyboardButton("⬇️ Zu Dry-Run herabstufen", callback_data=f"deploy_dry_confirm_{disc_id}")])
        elif status != "dry":
            buttons.append([InlineKeyboardButton("⚙️ Als Dry-Run starten", callback_data=f"deploy_dry_confirm_{disc_id}")])
        if status in ("live", "dry"):
            buttons.append([InlineKeyboardButton("⏸ Pausieren", callback_data=f"deploy_pause_{disc_id}")])
        buttons.append([InlineKeyboardButton("◀️ Zurück", callback_data="pm_main")])

    return text, InlineKeyboardMarkup(buttons)


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
    "drift_check":    1500,   # Daily-Job (Cron 06:00) — 25h Puffer für Cron-Verzögerungen
    "hmm_retrain":   10080,   # Wöchentlicher Job (So 05:00) — 7 Tage Puffer
    "regime_monitor":  250,   # 4h-Timer — 10 Min Puffer über 4h (=240 Min)
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


def _db_open_trades() -> list[dict]:
    """Laufende Positionen: Trades ohne exit_ts (Position noch offen)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT t.id, t.strategy, t.asset, t.direction,
                  t.entry_price, t.stop_loss, t.take_profit_1,
                  t.size, t.mode, t.entry_ts, t.be_applied
           FROM trades t
           WHERE t.exit_ts IS NULL
           ORDER BY t.entry_ts DESC LIMIT 20"""
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
    """Bestes Setup pro (asset, market_regime) nach Micro-Score — nur validierte Einträge."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT d.id, d.strategy, d.asset, d.market_regime,
                      d.pf_test, d.avg_r_test, d.wr_test, d.n_test,
                      d.fitness_score, d.params_json,
                      COALESCE(d.max_dd_r, 0.0) AS max_dd_r,
                      d.micro_score
               FROM lab_discoveries d
               INNER JOIN (
                   SELECT asset, market_regime, MAX(micro_score) AS best_score
                   FROM lab_discoveries
                   WHERE market_regime != 'UNKNOWN'
                     AND micro_score > 0
                     AND wr_test >= 48.0
                     AND n_test  >= 40
                   GROUP BY asset, market_regime
               ) best ON d.asset          = best.asset
                      AND d.market_regime = best.market_regime
                      AND d.micro_score   = best.best_score
               ORDER BY d.micro_score DESC"""
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
    Nur Setups mit micro_score > 0, WR ≥ 48% und n ≥ 40 (selbe Hürden wie Lab).
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT id, strategy, pf_test, avg_r_test, wr_test, n_test,
                  fitness_score,
                  COALESCE(max_dd_r, 0.0) AS max_dd_r,
                  micro_score
           FROM lab_discoveries
           WHERE asset=? AND market_regime=?
             AND market_regime != 'UNKNOWN'
             AND micro_score > 0
             AND wr_test >= 48.0
             AND n_test  >= 40
           ORDER BY micro_score DESC
           LIMIT 1""",
        (asset, regime),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _active_deployment_for(asset: str) -> dict | None:
    """Gibt das erste aktive Deployment für ein Asset zurück (mode + strategy_key)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT strategy_key, mode, discovery_id FROM active_deployments WHERE asset=? AND active=1 LIMIT 1",
        (asset,),
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
    lines = ["💼 *CIO Portfolio\\-Empfehlung*\n"]
    any_setup = False

    # Einmalig alle aktiven Deployments laden (statt N einzelne DB-Calls)
    conn = get_connection()
    dep_map: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT asset, mode, strategy_key FROM active_deployments WHERE active=1"
    ).fetchall():
        dep_map[row["asset"]] = {"mode": row["mode"], "strategy_key": row["strategy_key"]}
    conn.close()

    for p in portfolio:
        asset    = p["asset"]
        regime   = p["regime"]
        setup    = p["setup"]
        icon     = _REGIME_ICON_CIO.get(regime, "⚪")
        deployed = dep_map.get(asset)

        if setup:
            any_setup = True
            dd_usdt   = setup["max_dd_r"] * RISK_PER_TRADE_CIO
            score     = setup["micro_score"]
            fitness   = setup.get("fitness_score") or 0.0

            deploy_badge = ""
            if deployed:
                mode_label   = "LIVE" if deployed["mode"] == "live" else "DRY"
                deploy_badge = f"  ✅ _{mode_label} aktiv_\n"

            lines.append(
                f"*{asset}* {icon} `{regime}`\n"
                f"  \\#{setup['id']} `{setup['strategy']}`  "
                f"🎯 *{score:.1f}*  💰 PF *{setup['pf_test']:.2f}*\n"
                f"  🎰 *{setup['wr_test']:.1f}%*  "
                f"📈 *{setup['avg_r_test']:+.3f}R*  "
                f"n=*{setup['n_test']}*  "
                f"📉 *\\-${dd_usdt:.2f}*\n"
                + deploy_badge
            )
        else:
            reason = "kein Setup im Lab" if regime != "UNKNOWN" else "Regime unbekannt"
            lines.append(f"*{asset}* {icon} `{regime}`  _\\({reason}\\)_\n")

    if not any_setup:
        lines.append(
            "\n⚠️ _Kein passendes Setup für das aktuelle Markt\\-Regime\\._\n"
            "_Lab\\-Daemon läuft weiter — check später\\._"
        )
    text = "\n".join(lines)
    # Telegram-Limit: 4096 Zeichen
    if len(text) > 4000:
        text = text[:3990] + "\n_\\[\\.\\.\\.\\]_"
    return _p(text)


def _portfolio_keyboard(portfolio: list[dict]) -> InlineKeyboardMarkup:
    """
    Granulares Inline-Keyboard unter der CIO-Empfehlung.
    Callback-Format: cio_live:<disc_id> | cio_dry:<disc_id>
    (kurz, asset wird im Handler per discovery_id nachgeschlagen)
    """
    # Einmalig alle aktiven Deployments laden
    conn = get_connection()
    dep_assets: set[str] = {
        row["asset"] for row in
        conn.execute("SELECT asset FROM active_deployments WHERE active=1").fetchall()
    }
    conn.close()

    rows  = []
    valid = [(p["asset"], p["setup"]["id"]) for p in portfolio if p["setup"]]

    for asset, disc_id in valid:
        if asset in dep_assets:
            rows.append([
                InlineKeyboardButton(
                    f"✅ {asset} läuft",
                    callback_data=f"dep_info:{asset}",
                ),
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"🚀 {asset} Live",
                    callback_data=f"cio_live:{disc_id}",
                ),
                InlineKeyboardButton(
                    f"⚙️ {asset} Dry",
                    callback_data=f"cio_dry:{disc_id}",
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

    # Glossar-Buttons
    rows.append([
        InlineKeyboardButton("❓ Score",   callback_data="info_score"),
        InlineKeyboardButton("❓ PF",      callback_data="info_pf"),
        InlineKeyboardButton("❓ WR",      callback_data="info_wr"),
        InlineKeyboardButton("❓ Avg R",   callback_data="info_avgr"),
    ])
    rows.append([
        InlineKeyboardButton("❓ Fitness", callback_data="info_fitness"),
        InlineKeyboardButton("❓ Max DD",  callback_data="info_maxdd"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="portfolio"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _server_health() -> dict:
    """CPU- und RAM-Auslastung via psutil."""
    cpu  = psutil.cpu_percent(interval=None)
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
    s = f"{r:+.2f}R"
    return _escape_md(s)


def _ef(val, fmt: str = "") -> str:
    """Escaped float/string für MarkdownV2."""
    if fmt:
        s = format(val, fmt)
    else:
        s = str(val)
    return _escape_md(s)


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


def _db_market_weather_de() -> str:
    """Markt-Wetter mit deutschen Regime-Labels."""
    from core.db import get_state
    parts = []
    for asset in WEATHER_ASSETS:
        regime = get_state(f"regime_{asset}", "UNKNOWN")
        icon, _ = _REGIME_WEATHER.get(regime, ("⚪", "?"))
        parts.append(f"`{asset}` {icon}{_regime_short(regime)}")
    return "  ".join(parts)


def build_dashboard_text() -> str:
    from datetime import datetime, timezone
    d    = _db_pnl_summary()
    can  = _db_canary()
    now  = datetime.now(timezone.utc).strftime("%d. %b  %H:%M UTC")

    # ── Overall-Status ────────────────────────────────────────────────────────
    deployments = _db_active_deployments()
    live_deps = [dep for dep in deployments if dep["mode"] == "live"]
    dry_deps  = [dep for dep in deployments if dep["mode"] != "live"]
    n_active  = len(deployments)
    status_line = (
        f"🟢 SYSTEM OK  ·  {n_active} Strategie{'n' if n_active != 1 else ''} aktiv"
        if n_active else "⚪ Keine aktiven Strategien"
    )

    lines = [
        f"📊 *APEX V2  ·  {now}*",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        status_line,
    ]

    # ── Performance ──────────────────────────────────────────────────────────
    if d["total_n"] > 0:
        today_usd = _r_to_usd(d["today_r"])
        total_usd = _r_to_usd(d["total_r"])
        avg_usd   = _r_to_usd(d["avg_r"])
        avg_icon  = "✅" if d["avg_r"] >= 0.08 else ("⚠️" if d["avg_r"] >= 0 else "❌")
        lines += [
            "",
            "*══ PERFORMANCE ══════════════════*",
            f"Gesamt seit Start:  *{total_usd}*  ({_fmt_r(d['total_r'])})",
            f"Heute:              *{today_usd}*  ({d['total_n']} Trades)",
            f"Ø pro Trade:        *{avg_usd}*  {avg_icon}",
        ]

    # ── Aktive Strategien ─────────────────────────────────────────────────────
    if deployments:
        lines += ["", "*══ AKTIVE STRATEGIEN ════════════*"]

        for dep in live_deps:
            name    = _deployment_label(dep)
            n       = dep["n"]
            wins    = dep["wins"]
            losses  = n - wins
            total_r = dep["total_r"]
            avg_r   = dep["avg_r"]
            wr      = round(wins / n * 100) if n > 0 else 0
            open_s  = dep["open_signals"]
            pnl_usd = round((wins - losses) * RISK_USDT, 2)
            pnl_sign = "\\+" if pnl_usd >= 0 else ""
            pnl_icon = "🟢" if pnl_usd >= 0 else "🔴"
            lines.append(f"\n💰 *LIVE — {name}*")
            if n == 0:
                sig_hint = f"  · {open_s} Signal offen" if open_s else ""
                lines.append(f"  Noch kein Trade{sig_hint}")
            else:
                wr_icon = "✅" if wr >= 50 else "⚠️"
                lines.append(
                    f"  {n} Trades: {wins} Gewinner · {losses} Verlierer\n"
                    f"  PnL: *{pnl_icon} {pnl_sign}${pnl_usd:.2f}*  ·  "
                    f"Trefferquote: {wr}% {wr_icon}"
                )

        for dep in dry_deps:
            name   = _deployment_label(dep)
            n      = dep["n"]
            wins   = dep["wins"]
            losses = n - wins
            target = dep["target_trades"]
            total_r= dep["total_r"]
            avg_r  = dep["avg_r"]
            wr     = round(wins / n * 100) if n > 0 else 0
            open_s = dep["open_signals"]
            filled = min(int(n / target * 10), 10) if target else 0
            prog   = "▓" * filled + "░" * (10 - filled)
            pct    = min(int(n / target * 100), 100) if target else 0
            pnl_usd = round((wins - losses) * RISK_USDT, 2)
            lines.append(f"\n⚙️ *TEST — {name}*")
            if n == 0:
                sig_hint = f"  · {open_s} Signal offen" if open_s else ""
                lines.append(
                    f"  Fortschritt: 0/{target} Trades  \\[{prog}\\] 0%\n"
                    f"  ⏳ Wartet auf erstes Signal{sig_hint}"
                )
            elif n < target:
                remaining = target - n
                pnl_sign  = "\\+" if pnl_usd >= 0 else ""
                lines.append(
                    f"  Fortschritt: {n}/{target} Trades  \\[{prog}\\] {pct}%\n"
                    f"  ⏳ Noch {remaining} Trades bis Freischaltung\n"
                    f"  Bisher: {pnl_sign}${pnl_usd:.2f}  ·  {wr}% Trefferquote"
                )
            else:
                badge = "🟢 *BEREIT*" if total_r > 0 else "🔴 *NICHT BESTANDEN*"
                pnl_sign = "\\+" if pnl_usd >= 0 else ""
                lines.append(
                    f"  {n}/{target} Trades  \\[{prog}\\]  {badge}\n"
                    f"  PnL: {pnl_sign}${pnl_usd:.2f}  ·  {wr}% Trefferquote"
                )
    else:
        lines += [
            "",
            "_Keine aktiven Strategien — wähle ein Setup unter 🏆 Top Setups_",
        ]

    # ── Canary-Basis-Test (nur wenn noch keine Deployments laufen) ────────────
    total_can = sum((can.get(a) or {}).get("n", 0) for a in LAB_REF)
    if total_can > 0 and not deployments:
        all_ready = True
        lines += ["", "*══ BASIS-TEST (Canary) ═══════════*"]
        for asset, ref in LAB_REF.items():
            target  = _canary_target(asset)
            c       = can.get(asset)
            n       = c["n"]      if c else 0
            total_r = c["total_r"] if c else 0.0
            wins    = c["wins"]    if c else 0
            avg     = c["avg_r"]   if c else 0.0
            filled  = min(int(n / target * 10), 10) if target else 0
            prog    = "▓" * filled + "░" * (10 - filled)
            pct     = min(int(n / target * 100), 100) if target else 0
            if n < target:
                all_ready = False
                wr = round(wins / n * 100) if n else 0
                lines.append(
                    f"  `{asset}` {n}/{target}  \\[{prog}\\] {pct}%"
                    + (f"  · PnL {_r_to_usd(total_r)}  WR {wr}%" if n > 0 else "")
                )
            else:
                wr    = round(wins / n * 100) if n else 0
                badge = "🟢" if total_r > 0 else "🔴"
                lines.append(f"  `{asset}` {n}/{target}  \\[{prog}\\]  {badge}  PnL {_r_to_usd(total_r)}  WR {wr}%")
        if all_ready:
            lines.append("\n🎯 *Alle Assets bereit — Go/No\\-Go Entscheidung fällig\\!*")
        else:
            remaining = sum(max(0, _canary_target(a) - (can.get(a, {}).get("n") or 0)) for a in LAB_REF)
            lines.append(f"\n⏳ Noch ca\\. *{remaining}* Test-Trades bis zur Entscheidung")

    # ── Markt heute ───────────────────────────────────────────────────────────
    lines += ["", "*══ MARKT HEUTE ══════════════════*", _db_market_weather_de()]

    return _p("\n".join(lines))


def build_signals_text() -> str:
    open_trades = _db_open_trades()
    pending     = _db_open_signals()
    _mode_de    = {"live": "Live", "dry_run": "Test-Lauf"}

    if not open_trades and not pending:
        return "📂 *Offene Trades*\n\nKeine offenen Positionen oder wartenden Signale\\."

    lines = [f"📂 *OFFENE TRADES*\n"]

    # ── Laufende Positionen (bereits eingestiegen) ────────────────────────────
    if open_trades:
        lines.append(f"*💹 LAUFENDE POSITIONEN  ({len(open_trades)})*")

        # Live-Preise einmalig pro Asset holen
        live_prices: dict[str, float] = {}
        try:
            from execution.bitget_client import BitgetClient
            client = BitgetClient(dry_run=False)
            if client.is_ready:
                for t in open_trades:
                    asset = t["asset"]
                    if asset not in live_prices:
                        try:
                            live_prices[asset] = client.get_price(asset)
                        except Exception:
                            pass
        except Exception:
            pass

        for t in open_trades:
            dir_icon   = "📈 Long" if t["direction"] == "long" else "📉 Short"
            is_long    = t["direction"] == "long"
            mode       = _mode_de.get(t["mode"], t["mode"])
            dt         = t["entry_ts"][:16].replace("T", " ") if t["entry_ts"] else "?"
            be_hint    = "  🔒 BE" if t["be_applied"] else ""
            entry      = t["entry_price"] or 0.0
            sl         = t["stop_loss"]   or 0.0
            tp         = t["take_profit_1"] or 0.0
            risk_pts   = abs(entry - sl) if sl else 0.0
            live_price = live_prices.get(t["asset"])

            if live_price and risk_pts > 0:
                raw_pnl_r = (live_price - entry) / risk_pts if is_long else (entry - live_price) / risk_pts
                pnl_usd   = raw_pnl_r * RISK_USDT
                pnl_sign  = "\\+" if pnl_usd >= 0 else ""
                if pnl_usd > 0.05:
                    pnl_icon = "🟢"
                elif pnl_usd < -0.05:
                    pnl_icon = "🔴"
                else:
                    pnl_icon = "🟡"
                pnl_line = (
                    f"  Live: `{live_price}`  P&L: *{pnl_icon} {pnl_sign}${pnl_usd:.2f}*"
                    f"  \\({raw_pnl_r:+.2f}R\\){be_hint}"
                )
            elif live_price:
                pnl_line = f"  Live: `{live_price}`  P&L: — (kein SL gesetzt){be_hint}"
            else:
                pnl_line = f"  Live-Preis nicht verfügbar{be_hint}"

            lines.append(
                f"\n  {dir_icon}  *{t['asset']}*  \\[{mode}\\]\n"
                f"  Einstieg: `{entry}`  Stop: `{sl}`  Ziel: `{tp}`\n"
                f"{pnl_line}\n"
                f"  Seit: {dt}"
            )

    # ── Wartende Signale (noch nicht ausgeführt) ──────────────────────────────
    if pending:
        lines.append(f"\n*⏳ WARTENDE SIGNALE  ({len(pending)})*")
        _status_de = {"pending": "⏳ Wartet", "approved": "✅ Freigegeben", "processing": "⚙️ In Ausführung"}
        for s in pending:
            dir_icon = "📈 Long" if s["direction"] == "long" else "📉 Short"
            mode     = _mode_de.get(s["mode"], s["mode"])
            dt       = s["created_at"][:16].replace("T", " ")
            status   = _status_de.get(s["status"], s["status"])
            lines.append(
                f"  {status}  ·  {dir_icon}  *{s['asset']}*  \\[{mode}\\]\n"
                f"  Einstieg: `{s['entry_price']}`  Stop: `{s['stop_loss']}`  Ziel: `{s['take_profit_1']}`\n"
                f"  {dt}"
            )

    return _p("\n".join(lines))


def build_status_text() -> str:
    hbs  = _db_heartbeats()
    hw   = _server_health()
    now  = datetime.now(timezone.utc)

    def _res_icon(pct: float, warn: float = 70, crit: float = 90) -> str:
        return "🔴" if pct >= crit else ("⚠️" if pct >= warn else "✅")

    # ── Overall-Status vorab bestimmen ────────────────────────────────────────
    overall_ok = True
    hb_issues  = []
    for hb in hbs:
        comp    = hb["component"]
        max_min = HEARTBEAT_MAX_AGE_MIN.get(comp, 30)
        try:
            ts  = datetime.fromisoformat(hb["ts"])
            age = (now - ts).total_seconds() / 60
        except Exception:
            age = 999.0
        if age > max_min:
            overall_ok = False
            hb_issues.append(_component_label(comp))

    server_warn = (
        hw["cpu_pct"] >= 90 or hw["ram_pct"] >= 90 or hw["disk_pct"] >= 95
    )
    if server_warn:
        overall_ok = False

    status_line = "🟢 Alles läuft normal" if overall_ok else f"⚠️ Prüfung nötig: {', '.join(hb_issues)}"
    now_str     = now.strftime("%H:%M UTC")

    lines = [
        f"⚙️ *SYSTEM STATUS  ·  {now_str}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        status_line,
        "",
        "*══ SERVER ════════════════════════*",
        f"  {_res_icon(hw['cpu_pct'])} CPU:   *{hw['cpu_pct']:.0f}%*",
        (f"  {_res_icon(hw['ram_pct'])} RAM:   *{hw['ram_pct']:.0f}%*"
         f"  ({hw['ram_used_gb']:.1f} / {hw['ram_total_gb']:.1f} GB)"),
        (f"  {_res_icon(hw['disk_pct'], 80, 95)} Disk:  *{hw['disk_pct']:.0f}%*"
         f"  ({hw['disk_free_gb']:.0f} GB frei)"),
        f"  🕐 Läuft seit: {hw['uptime_h']:.0f} Stunden",
    ]

    # ── Pipeline ─────────────────────────────────────────────────────────────
    lines += ["", "*══ PIPELINE ══════════════════════*"]
    if not hbs:
        lines.append("  ⚠️ Keine Heartbeats empfangen — läuft master\\_run.py?")
    else:
        for hb in hbs:
            comp    = hb["component"]
            name    = _component_label(comp)
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
            else:
                icon = "🔴"

            age_s = (now - ts).total_seconds() if age < 999 else 999 * 60
            if age_s < 60:
                age_str = f"vor {int(age_s)}s"
            elif age_s < 7200:
                age_str = f"vor {int(age_s // 60)} Min"
            else:
                age_str = f"vor {age_s / 3600:.1f}h"
            limit_str = f"{max_min} Min"
            lines.append(f"  {icon} {name:<14}  {age_str}  (OK bis {limit_str})")

    # ── Letzte 5 Trades ───────────────────────────────────────────────────────
    recent = _db_last_trades(5)
    if recent:
        lines += ["", "*══ LETZTE TRADES ═════════════════*"]
        for t in recent:
            pnl_r   = t["pnl_r"] or 0
            pnl_usd = _r_to_usd(pnl_r)
            icon    = "✅" if pnl_r > 0 else "❌"
            reason  = _exit_label(t["exit_reason"] or "")
            regime  = _regime_short(t.get("market_regime") or "")
            lines.append(
                f"  {icon} {t['asset']} {t['direction'].capitalize()}"
                f"  *{pnl_usd}*  ·  {reason}"
            )

    # ── Drift-Warnungen ───────────────────────────────────────────────────────
    try:
        _conn = get_connection()
        drift_rows = _conn.execute("""
            SELECT strategy_key, asset, mode, n_live, pf_live, pf_oos, drift_pct, status, action_taken
            FROM live_vs_backtest_drift
            WHERE status IN ('warning', 'critical')
              AND checked_at = (
                  SELECT MAX(checked_at) FROM live_vs_backtest_drift d2
                  WHERE d2.deployment_id = live_vs_backtest_drift.deployment_id
              )
            ORDER BY drift_pct ASC
        """).fetchall()
        _conn.close()
        if drift_rows:
            lines += ["", "*══ DRIFT MONITOR ═════════════════*"]
            for dr in drift_rows:
                icon = "🔴" if dr["status"] == "critical" else "⚠️"
                pf_live_str = f"{dr['pf_live']:.2f}" if dr["pf_live"] else "n/a"
                drift_str   = f"{dr['drift_pct']:.1f}%" if dr["drift_pct"] else "n/a"
                action_str  = " → shadow" if dr["action_taken"] == "shadow_downgrade" else ""
                lines.append(
                    f"  {icon} {dr['strategy_key']} ({dr['asset']})"
                    f"  PF-live={pf_live_str} OOS={dr['pf_oos']:.2f}"
                    f"  Drift={drift_str} n={dr['n_live']}{action_str}"
                )
    except Exception:
        pass   # Drift-Tabelle fehlt oder leer — kein Fehler im Status-Screen

    return _p("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# Labor-Screen
# ══════════════════════════════════════════════════════════════════════════════

_LAB_ASSETS   = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "AVAX"]
_LAB_REGIMES  = ["TREND_UP", "TREND_DOWN", "SIDEWAYS"]
_LAB_STRATS   = ["squeeze", "vaa", "mean_reversion", "vwap_bounce",
                 "ema_pullback", "donchian_breakout", "inside_bar_breakout"]


def _lab_daemon_active() -> bool:
    """True wenn auto_lab_daemon als Prozess läuft (PID-Datei ODER direkte Prozesssuche)."""
    import signal, subprocess
    # 1. PID-Datei prüfen
    try:
        with open("/tmp/apex_lab_daemon.pid") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        pass
    # 2. Prozessliste durchsuchen (robuster Fallback)
    try:
        out = subprocess.check_output(["pgrep", "-f", "auto_lab_daemon"], text=True)
        return bool(out.strip())
    except Exception:
        return False


def _lab_db_stats() -> dict:
    """Alle Labor-Kennzahlen aus DB — vollständig abgesichert."""
    try:
        from research.auto_lab_daemon import get_lab_stats
        stats = get_lab_stats()
    except Exception:
        stats = {"total_tests": 0, "total_pass": 0, "total_disc": 0,
                 "hit_rate": 0.0, "top_rejection": [], "blind_spots": []}

    conn = get_connection()

    # Bestes freies Setup
    free_row = conn.execute(
        """SELECT id, strategy, asset, market_regime, micro_score, pf_test, wr_test, n_test
           FROM lab_discoveries
           WHERE deployment_status='lab'
             AND micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           ORDER BY micro_score DESC LIMIT 1"""
    ).fetchone()
    best_free = dict(free_row) if free_row else None

    # Queue (asset_requests)
    queue_rows = conn.execute(
        "SELECT asset, status, requested_at FROM asset_requests ORDER BY requested_at"
    ).fetchall()
    queue = [dict(r) for r in queue_rows]

    # Letzter Fund
    last_found = conn.execute(
        "SELECT MAX(discovered_at) FROM lab_discoveries"
    ).fetchone()[0]

    # Getestete (strategy, asset) Paare
    tested_pairs = conn.execute(
        "SELECT COUNT(DISTINCT strategy || '/' || asset) FROM lab_discoveries"
    ).fetchone()[0] or 0

    conn.close()

    return {
        **stats,
        "best_free":    best_free,
        "queue":        queue,
        "last_found":   last_found,
        "tested_pairs": tested_pairs,
        "daemon_active": _lab_daemon_active(),
    }


def _lab_since(iso: str) -> str:
    """ISO-Timestamp → 'vor Xh' / 'vor Xm' / 'vor X Tagen'."""
    if not iso:
        return "?"
    try:
        ts  = datetime.fromisoformat(iso)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 3600:
            return f"vor {int(age // 60)} Min"
        if age < 86400:
            return f"vor {age / 3600:.1f}h"
        return f"vor {int(age // 86400)} Tag{'en' if age // 86400 > 1 else ''}"
    except Exception:
        return "?"


def build_lab_main_text() -> str:
    s   = _lab_db_stats()
    now = datetime.now(timezone.utc).strftime("%d. %b  %H:%M UTC")

    daemon_line = "🟢 Daemon aktiv" if s["daemon_active"] else "🔴 Daemon offline"
    last_line   = f"Letzter Fund:  {_lab_since(s['last_found'])}" if s["last_found"] else "Noch kein Fund"

    # Suchraum
    n_queue   = len([q for q in s["queue"] if q["status"] == "pending"])
    q_names   = ", ".join(q["asset"] for q in s["queue"] if q["status"] == "pending")
    q_line    = f"Queue:   {n_queue} Asset{'s' if n_queue != 1 else ''}  ({q_names})" if n_queue else "Queue:   leer"

    lines = [
        f"🔬 *LABOR  ·  {now}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "*══ GESAMTBILANZ ══════════════*",
        f"Tests gesamt:    *{int(s['total_tests']):,}*".replace(",", "."),
        f"Bestanden:       *{int(s['total_pass']):,}*  ({s['hit_rate']:.2f}%)".replace(",", "."),
        f"Discoveries:     *{s['total_disc']}*",
        f"Strategien:      *{len(_LAB_STRATS)}*",
        f"Assets im Test:  *{len(_LAB_ASSETS)}*",
        "",
        "*══ SUCHRAUM ══════════════════*",
        f"Fest:    {len(_LAB_ASSETS) * len(_LAB_STRATS)} Kombis  ({len(_LAB_ASSETS)}×{len(_LAB_STRATS)})",
        q_line,
        "",
        "*══ LABOR STATUS ══════════════*",
        f"{daemon_line}  ·  {int(s['total_tests']):,} Tests".replace(",", "."),
        last_line,
    ]

    # Bestes freies Setup
    if s["best_free"]:
        b = s["best_free"]
        lines += [
            "",
            "*══ BESTES FREIES SETUP ═══════*",
            f"💎 *{b['strategy']}/{b['asset']}  ·  {_regime_label(b['market_regime'])}*",
            f"Score *{b['micro_score']:.1f}*  ·  PF *{b['pf_test']:.2f}*  ·  WR *{b['wr_test']:.0f}%*",
            f"→ Noch nicht deployed\\!",
        ]
    else:
        lines += ["", "_Alle validen Setups sind bereits deployed._"]

    return _p("\n".join(lines))


def _lab_main_keyboard() -> InlineKeyboardMarkup:
    s = _lab_db_stats()
    rows = [
        [
            InlineKeyboardButton("📋 Diagnose",   callback_data="lab_diagnose"),
            InlineKeyboardButton("📡 Signal-Radar", callback_data="lab_radar"),
        ],
        [
            InlineKeyboardButton("🌍 Suchraum",   callback_data="lab_suchraum"),
            InlineKeyboardButton("📈 Lernkurve",  callback_data="lab_lernkurve"),
        ],
        [
            InlineKeyboardButton("🎯 Heatmap",    callback_data="lab_heatmap"),
            InlineKeyboardButton("📊 Strategien", callback_data="lab_strategien"),
        ],
        [
            InlineKeyboardButton("📜 Funde",      callback_data="lab_funde"),
            InlineKeyboardButton("➕ Asset anfragen", callback_data="asset_req_list:0"),
        ],
    ]
    if s.get("best_free"):
        disc_id = s["best_free"]["id"]
        rows.append([InlineKeyboardButton(
            f"🚀 Bestes Setup deployen", callback_data=f"deploy_dry_{disc_id}"
        )])
    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_main"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def build_signal_radar_text() -> str:
    """
    Zeigt für jede aktive Deployment-Strategie wie nah sie am Signal-Trigger ist.
    Evaluiert auf der letzten GESCHLOSSENEN Kerze (= dieselbe Logik wie generic_deployed).
    """
    import json as _json

    try:
        from backtest.engine import SIGNAL_FNS, atr_wilder
    except Exception as e:
        return f"📡 *SIGNAL-RADAR*\n\n❌ Import-Fehler: {e}"

    def _vol_sma(candles, period):
        vols = [c["volume"] for c in candles[-period:]]
        return sum(vols) / len(vols) if vols else 0

    CANDLE_MS   = 3_600_000
    now_ms      = int(datetime.now(timezone.utc).timestamp() * 1000)
    candle_open = (now_ms // CANDLE_MS) * CANDLE_MS
    as_of_ts    = candle_open - 1

    conn = get_connection()
    deps = conn.execute(
        """SELECT d.strategy_key, d.base_strategy, d.asset, d.params_json, d.mode,
                  ld.market_regime
           FROM active_deployments d
           JOIN lab_discoveries ld ON ld.id = d.discovery_id
           WHERE d.active=1"""
    ).fetchall()

    lines = ["📡 *SIGNAL-RADAR* — Live-Puls\n"]

    _REGIME_ICON = {"TREND_UP": "📈", "TREND_DOWN": "📉", "SIDEWAYS": "↔️", "UNKNOWN": "❓"}

    for dep in deps:
        key     = dep["strategy_key"]
        base    = dep["base_strategy"]
        asset   = dep["asset"]
        params  = _json.loads(dep["params_json"])
        mode    = dep["mode"]
        regime  = dep["market_regime"]
        r_icon  = _REGIME_ICON.get(regime, "❓")
        m_icon  = "💰" if mode == "live" else "⚙️"

        fn = SIGNAL_FNS.get(base)
        if not fn:
            lines.append(f"❌ `{key}` — kein SIGNAL_FN")
            continue

        # Signal-Evaluation (exakt wie im Daemon)
        try:
            bt_sig = fn(conn, asset, as_of_ts, params)
        except Exception as e:
            lines.append(f"❌ `{key}/{asset}`: {str(e)[:60]}")
            continue

        if bt_sig is not None:
            lines.append(f"🚨 *SIGNAL AKTIV!*  `{key}`  {m_icon}")
            lines.append(f"   {asset} {bt_sig.direction.upper()} @ {bt_sig.entry_price:.4f}")
            lines.append(f"   SL={bt_sig.stop_loss:.4f}  TP={bt_sig.take_profit_1:.4f}")
            continue

        # Proximity-Analyse für donchian_breakout
        if base == "donchian_breakout":
            dc_period  = int(params.get("DC_PERIOD", 20))
            vol_factor = params.get("VOL_FACTOR", 1.5)
            atr_min    = params.get("ATR_MIN_MULT", 1.0)

            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE asset=? AND interval='1h' AND ts <= ? ORDER BY ts DESC LIMIT ?",
                (asset, as_of_ts, dc_period + 35),
            ).fetchall()
            candles = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                        "close": r[4], "volume": r[5]} for r in reversed(rows)]

            if len(candles) < dc_period + 5:
                lines.append(f"⚠️ `{key}/{asset}` — zu wenige Kerzen")
                continue

            cur     = candles[-1]
            window  = candles[-(dc_period + 1):-1]
            dc_high = max(c["high"]   for c in window)
            dc_low  = min(c["low"]    for c in window)
            vol_avg = _vol_sma(candles, 20)
            atr_val = atr_wilder(candles, 14)
            atr_avg = atr_wilder(candles[:-14], 14) if len(candles) > 28 else atr_val

            # Distanz zum Breakout in %
            dist_long  = (dc_high - cur["close"]) / cur["close"] * 100
            dist_short = (cur["close"] - dc_low)  / cur["close"] * 100
            dist_pct   = min(dist_long, dist_short)
            closer     = "LONG" if dist_long < dist_short else "SHORT"

            vol_ratio  = cur["volume"] / vol_avg if vol_avg > 0 else 0
            atr_ratio  = atr_val / atr_avg if atr_avg > 0 else 0

            # Ampel: grün < 0.5%, gelb < 1.5%, rot > 1.5%
            prox_icon  = "🟢" if dist_pct < 0.5 else ("🟡" if dist_pct < 1.5 else "🔴")
            vol_icon   = "✅" if vol_ratio >= vol_factor else ("🟡" if vol_ratio >= vol_factor * 0.7 else "❌")
            atr_icon   = "✅" if atr_ratio >= atr_min   else ("🟡" if atr_ratio >= atr_min   * 0.7 else "❌")

            lines.append(
                f"{prox_icon} `{asset}`  {r_icon} {m_icon}  →*{closer}* {dist_pct:.2f}% weg\n"
                f"   Vol {vol_icon} {vol_ratio:.1f}×  ·  ATR {atr_icon} {atr_ratio:.2f}×  ·  DC({dc_period})"
            )

        elif base == "inside_bar_breakout":
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE asset=? AND interval='1h' AND ts <= ? ORDER BY ts DESC LIMIT 50",
                (asset, as_of_ts),
            ).fetchall()
            candles = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                        "close": r[4], "volume": r[5]} for r in reversed(rows)]

            if len(candles) < 3:
                lines.append(f"⚠️ `{key}/{asset}` — zu wenige Kerzen")
                continue

            mother = candles[-2]
            child  = candles[-1]
            is_inside = child["high"] <= mother["high"] and child["low"] >= mother["low"]
            ema_p  = int(params.get("EMA_PERIOD", 50))
            ema_vals = [c["close"] for c in candles[-ema_p:]]
            ema_now  = sum(ema_vals) / len(ema_vals) if ema_vals else 0
            above_ema = child["close"] > ema_now
            icon_inside = "✅" if is_inside else "❌"
            icon_ema    = "✅" if above_ema else "❌"
            lines.append(
                f"🔵 `{asset}`  {r_icon} {m_icon}  Inside-Bar\n"
                f"   Mother-Bar {icon_inside}  ·  über EMA({ema_p}) {icon_ema}"
            )
        else:
            lines.append(f"⭕ `{key}/{asset}` — kein Signal")

    conn.close()

    # Zeitanzeige: letzte geschlossene Kerze + wie lange her
    closed_dt   = datetime.fromtimestamp(as_of_ts / 1000, tz=timezone.utc)
    age_min     = int((datetime.now(timezone.utc) - closed_dt).total_seconds() / 60)
    closed_str  = closed_dt.strftime("%H:%M UTC")
    lines += [
        "",
        f"_Letzte geschlossene Kerze: {closed_str} (vor {age_min} Min)_",
        "_Ampel: 🟢<0.5%  🟡<1.5%  🔴>1.5% vom Breakout_",
    ]
    return _p("\n".join(lines))


def build_lab_diagnose_text() -> str:
    try:
        from research.auto_lab_daemon import get_lab_stats
        stats = get_lab_stats()
    except Exception as e:
        return f"🧪 *Lab-Diagnose*\n\n❌ Fehler: {e}"

    total  = stats["total_tests"]
    passed = stats["total_pass"]
    disc   = stats["total_disc"]
    rate   = stats["hit_rate"]
    rej    = stats["top_rejection"]

    rate_icon = "🟢" if rate >= 1.0 else ("🟡" if rate >= 0.3 else "🔴")
    lines = [
        "📋 *LAB-DIAGNOSE*\n",
        f"Tests gesamt:  *{int(total):,}*".replace(",", "."),
        f"Bestanden:     *{int(passed):,}*".replace(",", "."),
        f"Discoveries:   *{disc}*",
        f"Hit-Rate:      {rate_icon} *{rate:.2f}%*",
        "",
        "*══ TOP ABLEHNUNGSGRÜNDE ══════*",
    ]

    _REJ_LABELS = {
        "zu_wenig_trades":     "Zu wenige Trades (n < 40)",
        "pf_zu_niedrig":       "PF zu niedrig (< 1.30)",
        "wr_zu_niedrig":       "WR zu niedrig (< 48%)",
        "avg_r_zu_niedrig":    "Avg R zu niedrig (< 0.08R)",
        "train_pf_zu_niedrig": "Train-PF zu niedrig",
        "ueberfit":            "Overfitting",
        "ruin_drawdown":       "Ruin-Filter (DD > $14)",
    }
    total_rej = sum(v for _, v in rej) if rej else 1
    for cat, count in rej[:5]:
        label = _REJ_LABELS.get(cat, cat)
        pct   = round(count / total_rej * 100) if total_rej else 0
        bar_w = min(int(pct / 5), 20)
        bar   = "▓" * bar_w + "·" * (20 - bar_w)
        lines.append(f"  `{bar}` *{pct}%*\n  _{label}_")

    # Zeit bis nächster Fund
    if total > 0 and passed > 0:
        conn = get_connection()
        first_ts = conn.execute("SELECT MIN(discovered_at) FROM lab_discoveries").fetchone()[0]
        last_ts  = conn.execute("SELECT MAX(discovered_at) FROM lab_discoveries").fetchone()[0]
        conn.close()
        if first_ts and last_ts and first_ts != last_ts:
            try:
                span_h   = (datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)).total_seconds() / 3600
                tests_ph = total / span_h if span_h > 0 else 0
                tests_per_find = total / passed if passed > 0 else 0
                eta_h    = tests_per_find / tests_ph if tests_ph > 0 else 0
                if tests_ph > 0 and eta_h >= 0.1:
                    lines += [
                        "",
                        "*══ PROGNOSE ══════════════════*",
                        f"Ø Tests/Stunde:   *{tests_ph:.0f}*",
                        f"Tests pro Fund:   *{tests_per_find:.0f}*",
                        f"Nächster Fund:    *ca. {eta_h:.1f}h*",
                    ]
            except Exception:
                pass

    return _p("\n".join(lines))


def build_lab_suchraum_text() -> str:
    conn = get_connection()
    queue = conn.execute(
        "SELECT asset, status, requested_at FROM asset_requests ORDER BY requested_at"
    ).fetchall()
    done  = [r for r in queue if r["status"] == "done"]
    pend  = [r for r in queue if r["status"] == "pending"]
    prog  = [r for r in queue if r["status"] == "in_progress"]
    conn.close()

    lines = [
        "🌍 *ASSET-SUCHRAUM*\n",
        f"*✅ IM SYSTEM  ({len(_LAB_ASSETS)} Assets)*",
        "  " + "  ·  ".join(_LAB_ASSETS),
        "",
        f"*⚙️ STRATEGIEN  ({len(_LAB_STRATS)})*",
    ]
    for s in _LAB_STRATS:
        lines.append(f"  · {s}")

    if pend or prog:
        lines += ["", f"*⏳ IN QUEUE  ({len(pend) + len(prog)} Assets)*"]
        for r in prog:
            dt = r["requested_at"][:10]
            lines.append(f"  🔬 *{r['asset']}*  — in Bearbeitung  (seit {dt})")
        for r in pend:
            dt = r["requested_at"][:10]
            lines.append(f"  ⏳ *{r['asset']}*  — wartet  (angefragt {dt})")
    else:
        lines += ["", "*⏳ QUEUE*  leer"]

    if done:
        lines += ["", f"*✅ ABGESCHLOSSEN  ({len(done)} Assets)*"]
        for r in done:
            lines.append(f"  {r['asset']}  — getestet")

    return _p("\n".join(lines))


def build_lab_lernkurve_text() -> str:
    conn = get_connection()
    rows = conn.execute(
        """SELECT discovered_at, micro_score FROM lab_discoveries
           WHERE micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           ORDER BY discovered_at"""
    ).fetchall()
    conn.close()

    lines = ["📈 *LERNKURVE*\n"]

    if len(rows) < 9:
        lines.append("_Noch zu wenige Daten für eine aussagekräftige Lernkurve._")
        lines.append(f"_Aktuell: {len(rows)} valide Discoveries — mind. 9 nötig._")
        return _p("\n".join(lines))

    third = len(rows) // 3
    e1 = [r[1] for r in rows[:third]]
    e2 = [r[1] for r in rows[third:2*third]]
    e3 = [r[1] for r in rows[2*third:]]

    avg1, avg2, avg3 = sum(e1)/len(e1), sum(e2)/len(e2), sum(e3)/len(e3)

    def _bar(val, max_val=20.0, w=10):
        filled = min(int(val / max_val * w), w)
        return "▓" * filled + "░" * (w - filled)

    max_val = max(avg1, avg2, avg3, 1.0)

    trend12 = ((avg2 - avg1) / avg1 * 100) if avg1 > 0 else 0
    trend23 = ((avg3 - avg2) / avg2 * 100) if avg2 > 0 else 0
    t12_sign = f"+{trend12:.0f}%" if trend12 >= 0 else f"{trend12:.0f}%"
    t23_sign = f"+{trend23:.0f}%" if trend23 >= 0 else f"{trend23:.0f}%"

    ts1 = rows[0][0][:10]
    ts2 = rows[third][0][:10]
    ts3 = rows[2*third][0][:10]

    lines += [
        f"Epoche 1  ({ts1}+)   Ø *{avg1:.1f}*",
        f"`{_bar(avg1, max_val)}` n={len(e1)}",
        "",
        f"Epoche 2  ({ts2}+)   Ø *{avg2:.1f}*  ({t12_sign})",
        f"`{_bar(avg2, max_val)}` n={len(e2)}",
        "",
        f"Epoche 3  ({ts3}+)   Ø *{avg3:.1f}*  ({t23_sign})",
        f"`{_bar(avg3, max_val)}` n={len(e3)}",
        "",
    ]

    overall = ((avg3 - avg1) / avg1 * 100) if avg1 > 0 else 0
    if abs(overall) < 5:
        verdict = "Lab läuft stabil — Suchraum noch nicht erschöpft."
    elif overall > 0:
        verdict = f"Lab verbessert sich ({overall:+.0f}%) — weiter laufen lassen."
    else:
        verdict = f"Scores leicht rückläufig ({overall:+.0f}%) — normal bei gesättigtem Bereich."

    lines.append(f"_{verdict}_")

    # Zeit-bis-Fund aus lab_stats
    try:
        conn2 = get_connection()
        total = int(conn2.execute("SELECT value FROM lab_stats WHERE key='total_tests'").fetchone()[0])
        passed = int(conn2.execute("SELECT value FROM lab_stats WHERE key='total_pass'").fetchone()[0])
        first_ts = conn2.execute("SELECT MIN(discovered_at) FROM lab_discoveries").fetchone()[0]
        last_ts2 = conn2.execute("SELECT MAX(discovered_at) FROM lab_discoveries").fetchone()[0]
        conn2.close()
        if first_ts and last_ts2 and first_ts != last_ts2 and passed > 0:
            span_h   = (datetime.fromisoformat(last_ts2) - datetime.fromisoformat(first_ts)).total_seconds() / 3600
            tests_ph = total / span_h if span_h > 0 else 0
            eta_h    = (total / passed) / tests_ph if tests_ph > 0 else 0
            # Prognose nur sinnvoll wenn Daemon aktiv und eta realistisch (> 0.1h)
            if tests_ph > 0 and eta_h >= 0.1:
                lines += [
                    "",
                    "*══ PROGNOSE ══════════════════*",
                    f"Ø Tests/Stunde:  *{tests_ph:.0f}*",
                    f"Nächster Fund:   *ca. {eta_h:.1f}h*",
                ]
    except Exception:
        pass

    return _p("\n".join(lines))


def build_lab_heatmap_text() -> str:
    conn = get_connection()
    # Alle validen Setups pro (asset, regime)
    valid = {
        (r[0], r[1])
        for r in conn.execute(
            """SELECT asset, market_regime FROM lab_discoveries
               WHERE micro_score > 0 AND wr_test >= 48 AND n_test >= 40"""
        ).fetchall()
    }
    # Alle getesteten (asset, regime) — auch ohne valides Ergebnis
    tested = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT DISTINCT asset, market_regime FROM lab_discoveries"
        ).fetchall()
    }
    conn.close()

    _REGIME_SHORT_MAP = {"TREND_UP": "↑Trend", "TREND_DOWN": "↓Trend", "SIDEWAYS": "↔Seite"}

    lines = [
        "🎯 *REGIME-ABDECKUNG*\n",
        f"{'':8}  {'↑Trend':8}  {'↓Trend':8}  {'↔Seite':8}",
        "─────────────────────────────────",
    ]

    covered = 0
    total   = len(_LAB_ASSETS) * len(_LAB_REGIMES)
    warn_assets = []

    for asset in _LAB_ASSETS:
        row = f"`{asset:4}`   "
        asset_ok = 0
        for regime in _LAB_REGIMES:
            if (asset, regime) in valid:
                row += "  ✅      "
                covered += 1
                asset_ok += 1
            elif (asset, regime) in tested:
                row += "  🔬      "   # getestet, kein valides Setup
            else:
                row += "  ⚪      "   # noch nicht getestet
        if asset_ok == 0:
            warn_assets.append(asset)
        lines.append(row)

    lines += [
        "─────────────────────────────────",
        f"✅ `{covered}/{total}` abgedeckt  ({covered/total*100:.0f}%)",
        "",
        "✅ valides Setup  ·  🔬 getestet/kein Fund  ·  ⚪ ausstehend",
    ]

    if warn_assets:
        lines += ["", f"⚠️ Keine Abdeckung: {', '.join(warn_assets)}"]

    return _p("\n".join(lines))


def build_lab_strategien_text() -> str:
    conn = get_connection()
    rows = conn.execute(
        """SELECT strategy,
                  COUNT(*) n,
                  AVG(micro_score) avg_ms,
                  MAX(micro_score) max_ms
           FROM lab_discoveries
           WHERE micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           GROUP BY strategy ORDER BY n DESC"""
    ).fetchall()
    conn.close()

    found_map = {r[0]: (r[1], r[2], r[3]) for r in rows}

    stars = {0: "", 1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐"}
    lines = ["📊 *STRATEGIE-RANGLISTE*\n", "_Tippen → Fenster-Details_\n"]

    for i, strat in enumerate(sorted(_LAB_STRATS, key=lambda s: found_map.get(s, (0,0,0))[0], reverse=True), 1):
        if strat in found_map:
            n, avg, best = found_map[strat]
            star = stars.get(min(int(n / max(len(rows), 1) * 3), 3), "⭐⭐⭐")
            lines.append(
                f"*{i}.* `{strat}`  {star}\n"
                f"   {n} Funde  ·  Ø Score {avg:.1f}  ·  Best {best:.1f}"
            )
        else:
            lines.append(f"*{i}.* `{strat}`  🔴 0 Funde")

    lines += [
        "",
        "_Nur Setups mit WR≥48%, n≥40, Score>0 gezählt._",
    ]
    return _p("\n".join(lines))


def _lab_strategien_keyboard() -> InlineKeyboardMarkup:
    """Keyboard mit einem Button pro Strategie für den Detail-Drill-Down."""
    strat_rows = []
    for i in range(0, len(_LAB_STRATS), 2):
        pair = _LAB_STRATS[i:i+2]
        strat_rows.append([
            InlineKeyboardButton(s, callback_data=f"lab_sd:{s[:20]}")
            for s in pair
        ])
    strat_rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_strategien"),
        InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
    ])
    return InlineKeyboardMarkup(strat_rows)


def build_lab_strat_detail_text(strategy: str) -> str:
    """Fenster-Performance-Breakdown für eine einzelne Strategie."""
    conn = get_connection()

    # Gesamt-Übersicht
    overview = conn.execute(
        """SELECT COUNT(*) n, AVG(micro_score) avg_ms, MAX(micro_score) max_ms,
                  AVG(pf_test) avg_pf, AVG(wr_test) avg_wr, AVG(n_test) avg_n
           FROM lab_discoveries
           WHERE strategy=? AND micro_score > 0 AND wr_test >= 48 AND n_test >= 40""",
        (strategy,),
    ).fetchone()

    # Asset-Breakdown
    asset_rows = conn.execute(
        """SELECT asset, COUNT(*) n, AVG(micro_score) avg_ms, MAX(micro_score) max_ms
           FROM lab_discoveries
           WHERE strategy=? AND micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           GROUP BY asset ORDER BY avg_ms DESC""",
        (strategy,),
    ).fetchall()

    # Fenster-Breakdown aus lab_window_results (Weg B)
    window_rows = conn.execute(
        """SELECT w.window_idx,
                  COUNT(*) n_disc,
                  AVG(w.pf_test)    avg_pf,
                  AVG(w.wr_test)    avg_wr,
                  AVG(w.avg_r_test) avg_r,
                  SUM(w.passed)     n_passed
           FROM lab_window_results w
           JOIN lab_discoveries d ON d.id = w.discovery_id
           WHERE d.strategy=? AND d.micro_score > 0 AND d.wr_test >= 48 AND d.n_test >= 40
           GROUP BY w.window_idx
           ORDER BY w.window_idx""",
        (strategy,),
    ).fetchall()

    # Zeitliche Stabilität: Funde nach Monat
    month_rows = conn.execute(
        """SELECT strftime('%Y-%m', discovered_at) mo, COUNT(*) n
           FROM lab_discoveries
           WHERE strategy=? AND micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           GROUP BY mo ORDER BY mo DESC LIMIT 6""",
        (strategy,),
    ).fetchall()

    conn.close()

    lines = [f"🔬 *{strategy.upper()}* — Fenster-Analyse\n"]

    # Gesamt
    if overview and overview["n"] > 0:
        lines.append(
            f"*Gesamt:* {overview['n']} Funde  ·  Ø Score {overview['avg_ms']:.1f}  ·  Best {overview['max_ms']:.1f}\n"
            f"Ø PF {overview['avg_pf']:.2f}  ·  Ø WR {overview['avg_wr']:.0f}%  ·  Ø n={overview['avg_n']:.0f}\n"
        )
    else:
        lines.append("_Noch keine qualifizierten Funde._")
        return _p("\n".join(lines))

    # Fenster-Breakdown
    _W_LABELS = ["F1 (480d–360d)", "F2 (240d–120d)", "F3 (60d–0d) ★"]
    if window_rows:
        lines.append("*OOS-Fenster:*")
        for wr in window_rows:
            idx   = wr["window_idx"]
            label = _W_LABELS[idx] if idx < len(_W_LABELS) else f"F{idx+1}"
            pct   = round(wr["n_passed"] / wr["n_disc"] * 100) if wr["n_disc"] else 0
            lines.append(
                f"  `{label}`  ✅{pct}%  ·  "
                f"Ø PF {wr['avg_pf']:.2f}  ·  Ø WR {wr['avg_wr']:.0f}%  ·  Ø R {wr['avg_r']:+.3f}"
            )
        lines.append("")
    else:
        lines.append("_Fenster-Daten werden ab dem nächsten Discovery-Zyklus erfasst._\n")

    # Asset-Übersicht
    if asset_rows:
        lines.append("*Assets:*")
        for ar in asset_rows:
            lines.append(
                f"  `{ar['asset']}`  {ar['n']} Funde  ·  Ø {ar['avg_ms']:.1f}  ·  Best {ar['max_ms']:.1f}"
            )
        lines.append("")

    # Zeitliche Stabilität
    if month_rows:
        lines.append("*Zeitliche Aktivität:*")
        for mr in month_rows:
            bar = "█" * min(mr["n"], 10)
            lines.append(f"  `{mr['mo']}`  {bar} {mr['n']}")

    lines += ["", "_WR≥48%, n≥40, Score>0 Filter aktiv._"]
    return _p("\n".join(lines))


def build_lab_funde_text() -> str:
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, strategy, asset, market_regime, micro_score,
                  pf_test, wr_test, n_test, discovered_at, deployment_status
           FROM lab_discoveries
           WHERE micro_score > 0 AND wr_test >= 48 AND n_test >= 40
           ORDER BY discovered_at DESC LIMIT 10"""
    ).fetchall()
    conn.close()

    if not rows:
        return "📜 *LETZTE FUNDE*\n\nNoch keine validen Discoveries\\."

    lines = [f"📜 *LETZTE FUNDE*  ({len(rows)} angezeigt)\n"]
    _DEP_ICON = {"live": "💰 Live", "dry": "⚙️ Dry-Run", "lab": "💎 frei"}

    for i, r in enumerate(rows, 1):
        dep_label = _DEP_ICON.get(r["deployment_status"], r["deployment_status"])
        since     = _lab_since(r["discovered_at"])
        regime    = _regime_short(r["market_regime"])
        lines.append(
            f"*#{i}*  `{r['strategy']}/{r['asset']}`  ·  {regime}\n"
            f"  Score *{r['micro_score']:.1f}*  ·  PF *{r['pf_test']:.2f}*  ·  WR *{r['wr_test']:.0f}%*  ·  n={r['n_test']}\n"
            f"  {since}  ·  {dep_label}"
        )
        if i < len(rows):
            lines.append("─────────────────")

    return _p("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# Asset-Anfrage Feature
# ══════════════════════════════════════════════════════════════════════════════

_KNOWN_ASSETS_IN_SYSTEM: set[str] = {"BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "AVAX"}


def _fetch_bitget_futures_assets() -> list[dict]:
    """
    Holt alle USDT-Futures von Bitget (öffentlich, kein API-Key nötig).
    Filtert auf Vol. >= $50M/24h und Nicht-im-System-Assets.
    Gibt Liste von {"asset": str, "volume_usd": float, "price": float} zurück.
    Sortiert nach Volumen absteigend, max. 50 Ergebnisse.
    """
    import urllib.request, json
    try:
        url = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
        req = urllib.request.Request(url, headers={"User-Agent": "APEX-V2/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return []

    results = []
    for item in data.get("data", []):
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        asset = symbol.replace("USDT", "")
        if asset in _KNOWN_ASSETS_IN_SYSTEM:
            continue
        try:
            price    = float(item.get("lastPr") or item.get("last") or 0)
            vol_24h  = float(item.get("usdtVolume") or item.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if vol_24h < 50_000_000:
            continue
        results.append({"asset": asset, "volume_usd": vol_24h, "price": price})

    results.sort(key=lambda x: x["volume_usd"], reverse=True)
    return results[:50]


def _db_asset_requests() -> list[dict]:
    """Gibt alle angeforderten Assets zurück (status=pending/in_progress/done)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT asset, status, requested_at FROM asset_requests ORDER BY requested_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_request_asset(asset: str) -> str:
    """
    Speichert eine Asset-Anfrage. Gibt zurück:
    'created'  — neu erstellt
    'exists'   — bereits angefragt
    """
    from datetime import datetime, timezone
    conn = get_connection()
    existing = conn.execute(
        "SELECT status FROM asset_requests WHERE asset=?", (asset,)
    ).fetchone()
    if existing:
        conn.close()
        return "exists"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO asset_requests (asset, requested_at, requested_by, status) VALUES (?,?,?,?)",
        (asset, now, "telegram", "pending"),
    )
    conn.commit()
    conn.close()
    return "created"


def _build_asset_request_list_text(assets: list[dict], requested: set[str]) -> str:
    if not assets:
        return (
            "➕ *Neues Asset anfragen*\n\n"
            "❌ Keine geeigneten Assets gefunden.\n"
            "_Bitget API nicht erreichbar oder alle Assets bereits im System._"
        )
    lines = [
        "➕ *Neues Asset anfragen*\n",
        "Wähle ein etabliertes Asset mit >$50M Tagesvolumen:\n",
        "_Nur Assets die noch nicht im System sind werden angezeigt._\n",
    ]
    for a in assets[:10]:
        vol_m = a["volume_usd"] / 1_000_000
        tag   = " ✅ angefragt" if a["asset"] in requested else ""
        lines.append(f"  `{a['asset']}` — Vol. ${vol_m:.0f}M{tag}")
    return _p("\n".join(lines))


def _build_asset_request_list_keyboard(assets: list[dict], requested: set[str], page: int = 0) -> InlineKeyboardMarkup:
    PAGE_SIZE = 10
    start = page * PAGE_SIZE
    end   = start + PAGE_SIZE
    page_assets = assets[start:end]

    rows = []
    row  = []
    for i, a in enumerate(page_assets):
        label = f"✅ {a['asset']}" if a["asset"] in requested else a["asset"]
        row.append(InlineKeyboardButton(label, callback_data=f"asset_req_detail:{a['asset']}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Zurück", callback_data=f"asset_req_list:{page-1}"))
    if end < len(assets):
        nav.append(InlineKeyboardButton("Weiter ▶️", callback_data=f"asset_req_list:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="asset_req_list:0"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Tastatur
# ══════════════════════════════════════════════════════════════════════════════

def persistent_keyboard() -> ReplyKeyboardMarkup:
    """Dauerhaftes Tastenfeld — bleibt im Chat-Eingabebereich angedockt."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Überblick"),     KeyboardButton("💰 Strategien")],
            [KeyboardButton("🏆 Top Setups"),    KeyboardButton("📂 Offene Trades")],
            [KeyboardButton("🔬 Labor"),          KeyboardButton("⚙️ System")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Überblick",       callback_data="dashboard"),
            InlineKeyboardButton("📂 Offene Signale",  callback_data="signals"),
        ],
        [
            InlineKeyboardButton("💰 Strategien",      callback_data="manage_strategies"),
            InlineKeyboardButton("🔄 Aktualisieren",   callback_data="refresh_menu"),
        ],
        [
            InlineKeyboardButton("🏆 Top Setups",      callback_data="alpha"),
            InlineKeyboardButton("⚙️ System",          callback_data="status"),
        ],
        [
            InlineKeyboardButton("📖 Hilfe",           callback_data="help"),
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
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=persistent_keyboard(),
    )
    # Inline-Menü als zweite Nachricht
    await update.message.reply_text(
        "📋 *Schnellzugriff:*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Hauptmenü*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard(),
    )


def _status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔬 Lab starten",  callback_data="lab_run_now"),
            InlineKeyboardButton("📊 Audit",        callback_data="audit_now"),
            InlineKeyboardButton("📈 Regime",       callback_data="regime_now"),
        ],
        [
            InlineKeyboardButton("🔄 Aktualisieren", callback_data="status"),
            InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
        ],
    ])


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    text = build_status_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_status_keyboard(),
    )


def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Aktualisieren",       callback_data="dashboard"),
            InlineKeyboardButton("⚙️ Strategien",          callback_data="manage_strategies"),
        ],
        [
            InlineKeyboardButton("❓ Avg R",               callback_data="info_avgr"),
            InlineKeyboardButton("❓ PnL",                 callback_data="info_pnl"),
            InlineKeyboardButton("❓ Canary",              callback_data="info_canary"),
        ],
        [InlineKeyboardButton("◀️ Menü",                   callback_data="back_menu")],
    ])


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    text = build_dashboard_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_dashboard_keyboard(),
    )


async def cmd_lab(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /lab <ASSET> [DAYS]
    Startet einen On-Demand Squeeze-Backtest im Thread-Pool.
    Antwort kommt als neue Nachricht sobald der Test fertig ist.
    """
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    args  = (ctx.args or [])
    asset = args[0].upper() if args else None
    days  = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 365

    if not asset:
        await update.message.reply_text(
            "❌ Nutzung: `/lab ETH` oder `/lab XRP 180`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Sofort bestätigen — Backtest dauert einige Sekunden
    wait_msg = await update.message.reply_text(
        f"🔬 Starte Squeeze-Backtest für *{asset}* über {days} Tage\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
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
            parse_mode=ParseMode.MARKDOWN_V2,
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
    await wait_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)


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
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    await update.message.reply_text(
        _HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
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
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    args  = ctx.args or []
    asset = args[0].upper() if len(args) >= 1 else None
    days  = int(args[1]) if len(args) >= 2 and args[1].isdigit() else None

    if not asset or not days:
        await update.message.reply_text(
            "❌ Nutzung: `/fetch ETH 365` oder `/fetch XRP 180`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    wait_msg = await update.message.reply_text(
        f"⏳ Lade Daten für *{asset}* \\({days} Tage\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
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
        parse_mode=ParseMode.MARKDOWN_V2,
    )


_REGIME_ICON = {"TREND_UP": "📈", "TREND_DOWN": "📉", "SIDEWAYS": "↔️", "UNKNOWN": "❓"}


def _build_alpha_text() -> str:
    setups = _db_alpha_setups()
    if not setups:
        return (
            "🏆 *Beste Strategien*\n\n"
            "Noch keine validierten Strategien in der Datenbank\\.\n"
            "_Der Lab\\-Daemon muss mindestens eine Iteration abgeschlossen haben\\._"
        )

    medals = ["🥇", "🥈", "🥉"]
    lines  = [f"🏆 *BESTE STRATEGIEN*  \\({len(setups)} gefunden\\)\n"]

    for i, s in enumerate(setups, 1):
        medal      = medals[i - 1] if i <= 3 else f"#{i}"
        regime_lbl = _regime_label(s["market_regime"])
        regime_ico = _REGIME_ICON.get(s["market_regime"], "❓")
        score      = s.get("micro_score") or 0.0
        score_lbl  = _pm_score_label(score)
        bar        = _score_bar(score)
        score_norm = _score_10(score)
        max_dd_usd = (s.get("max_dd_r") or 0.0) * RISK_USDT
        avg_usd    = _r_to_usd(s["avg_r_test"])
        wr_icon    = "✅" if s["wr_test"] >= 50 else "⚠️"
        pf_icon    = "✅" if s["pf_test"] >= 1.5 else ("⚠️" if s["pf_test"] >= 1.2 else "❌")

        lines.append(
            f"{medal} *{s['asset']} · {regime_ico} {regime_lbl}*\n"
            f"  Qualität: `{bar}`  *{score_norm}*  {score_lbl}\n"
            f"  {s['n_test']} Test-Trades  ·  Trefferquote: *{s['wr_test']:.0f}%* {wr_icon}\n"
            f"  Ø Gewinn/Trade: *{avg_usd}*  ·  PF: *{s['pf_test']:.2f}* {pf_icon}\n"
            f"  Max\\. Rückgang: *\\-${max_dd_usd:.2f}*"
        )
        if i < len(setups):
            lines.append("─────────────────────")

    return _p("\n".join(lines))


def _build_alpha_keyboard(setups: list) -> InlineKeyboardMarkup:
    """Inline-Keyboard für Alpha-Screen: Deploy-Buttons pro Setup."""
    rows = []
    for s in setups:
        disc_id = s["id"]
        asset   = s["asset"]
        rows.append([
            InlineKeyboardButton(f"🚀 {asset} Live",      callback_data=f"deploy_live_{disc_id}"),
            InlineKeyboardButton(f"⚙️ {asset} Test-Lauf", callback_data=f"deploy_dry_{disc_id}"),
            InlineKeyboardButton("🔍 Details",             callback_data=f"pm_detail_{disc_id}"),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="alpha"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    rows.append([
        InlineKeyboardButton("📊 Portfolio Manager", callback_data="portfolio"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_lab_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    text = _build_lab_stats_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_lab_stats_keyboard(),
    )


async def cmd_alpha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    setups = _db_alpha_setups()
    text   = _build_alpha_text()
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_build_alpha_keyboard(setups),
    )


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/portfolio — Portfolio Manager: navigierbarer Lab-Discovery-Browser."""
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    import asyncio
    text = await asyncio.get_event_loop().run_in_executor(None, _build_pm_main_text)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_pm_main_keyboard(),
    )


async def cmd_api_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /api_test — prüft API-Verbindung und gibt Futures-Balance zurück.
    Diagnostiziert häufige Fehler: falscher Key, IP-Whitelist, Futures inaktiv.
    """
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    await update.message.reply_text(
        "🔌 Teste API\\-Verbindung \\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2
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
            parse_mode=ParseMode.MARKDOWN_V2,
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
        text, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=persistent_keyboard(),
    )


async def cmd_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/promote <deployment_id> — Promotiert ein dry_run-Deployment zu live."""
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return

    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "❌ Nutzung: `/promote 8`\n"
            "Die ID findest du im `/status`\\-Screen unter Deployments\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    dep_id = int(args[0])
    import asyncio

    def _load_promote_data():
        conn = get_connection()
        try:
            dep = conn.execute(
                "SELECT id, strategy_key, base_strategy, asset, mode, active "
                "FROM active_deployments WHERE id=?",
                (dep_id,),
            ).fetchone()
            if not dep:
                return {"error": f"Deployment \\#{dep_id} nicht gefunden\\."}
            dep = dict(dep)
            if dep["mode"] != "dry_run":
                return {"error": f"Nur dry\\_run\\-Deployments können promoted werden \\(aktuell: {dep['mode']}\\)\\."}
            if not dep["active"]:
                return {"error": f"Deployment \\#{dep_id} ist nicht aktiv\\."}

            # Trades
            rows = conn.execute(
                "SELECT pnl_r FROM trades WHERE strategy=? AND asset=? AND exit_ts IS NOT NULL",
                (dep["strategy_key"], dep["asset"]),
            ).fetchall()
            n = len(rows)
            gross_win  = sum(r[0] for r in rows if r[0] and r[0] > 0)
            gross_loss = abs(sum(r[0] for r in rows if r[0] and r[0] < 0))
            pf_live = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

            # Drift
            drift_row = conn.execute(
                "SELECT status FROM live_vs_backtest_drift "
                "WHERE strategy_key=? ORDER BY checked_at DESC LIMIT 1",
                (dep["strategy_key"],),
            ).fetchone()
            drift = drift_row[0] if drift_row else "unbekannt"

            # HMM-Regime
            try:
                from research.train_hmm import get_current_regime
                regime = get_current_regime(dep["asset"], conn)
            except Exception:
                regime = "UNKNOWN"

            return {
                "dep": dep, "n": n, "pf_live": pf_live,
                "drift": drift, "regime": regime,
            }
        finally:
            conn.close()

    data = await asyncio.get_event_loop().run_in_executor(None, _load_promote_data)

    if "error" in data:
        await update.message.reply_text(data["error"], parse_mode=ParseMode.MARKDOWN_V2)
        return

    dep    = data["dep"]
    n      = data["n"]
    pf_s   = f"{data['pf_live']:.2f}" if data["pf_live"] is not None else "n/a"
    drift  = data["drift"]
    regime = data["regime"]
    sk     = _escape_md(dep["strategy_key"])
    asset  = _escape_md(dep["asset"])

    msg = (
        f"🔬 *Promote Deployment \\#{dep_id}*\n\n"
        f"Strategie: `{sk}`\n"
        f"Asset: `{asset}`\n\n"
        f"📊 n\\_trades: `{n}`\n"
        f"📈 Live\\-PF: `{pf_s}`\n"
        f"📡 Drift: `{_escape_md(drift)}`\n"
        f"🧠 HMM\\-Regime: `{_escape_md(regime)}`\n\n"
        f"⚠️ *Dieses Deployment handelt ab sofort mit echtem Kapital\\.*"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Zu Live promoten", callback_data=f"promote_confirm_{dep_id}")],
        [InlineKeyboardButton("❌ Abbrechen",         callback_data="back_menu")],
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)


async def cmd_shadow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/shadow <deployment_id> — Setzt Deployment auf shadow-Mode (read-only Beobachtung)."""
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return

    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "❌ Nutzung: `/shadow 8`\n"
            "Die ID findest du im `/status`\\-Screen unter Deployments\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    dep_id = int(args[0])
    import asyncio
    from datetime import datetime, timezone

    def _do_shadow():
        conn = get_connection()
        try:
            dep = conn.execute(
                "SELECT id, strategy_key, asset, mode, active FROM active_deployments WHERE id=?",
                (dep_id,),
            ).fetchone()
            if not dep:
                return {"error": f"Deployment \\#{dep_id} nicht gefunden\\."}
            dep = dict(dep)
            if dep["mode"] == "shadow":
                return {"error": f"Deployment \\#{dep_id} ist bereits im Shadow\\-Mode\\."}
            if not dep["active"]:
                return {"error": f"Deployment \\#{dep_id} ist nicht aktiv\\."}

            prev_mode = dep["mode"]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE active_deployments SET mode='shadow' WHERE id=?",
                (dep_id,),
            )
            conn.execute(
                """INSERT INTO kill_switch_events (ts, action, mode_from, mode_to, reason, cleared_by, asset)
                   VALUES (?, 'shadow_set', ?, 'shadow', 'Telegram /shadow command', ?, ?)""",
                (now, prev_mode, str(update.effective_user.id if update.effective_user else "bot"),
                 dep["asset"]),
            )
            conn.commit()
            return {"ok": True, "dep": dep, "prev_mode": prev_mode}
        finally:
            conn.close()

    data = await asyncio.get_event_loop().run_in_executor(None, _do_shadow)

    if "error" in data:
        await update.message.reply_text(data["error"], parse_mode=ParseMode.MARKDOWN_V2)
        return

    dep       = data["dep"]
    prev_mode = _escape_md(data["prev_mode"])
    sk        = _escape_md(dep["strategy_key"])
    asset     = _escape_md(dep["asset"])
    await update.message.reply_text(
        f"🌑 *Shadow\\-Mode gesetzt*\n\n"
        f"Deployment \\#{dep_id}: `{sk}` / `{asset}`\n"
        f"Mode: `{prev_mode}` → `shadow`\n\n"
        f"Das Deployment beobachtet nun nur noch \\— keine Orders werden gesendet\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_board(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/board — Operator-Dashboard mit 6 KPIs (read-only)."""
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return

    import asyncio
    from datetime import datetime, timezone, timedelta

    def _load_board():
        conn = get_connection()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        try:
            # 1. Live-DD heute
            dd_row = conn.execute(
                "SELECT value FROM system_state WHERE key='daily_drawdown' LIMIT 1"
            ).fetchone()
            live_dd = round(float(dd_row[0]), 4) if dd_row else 0.0

            # 2. Offene Positionen
            open_pos = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE exit_ts IS NULL"
            ).fetchone()[0]

            # 3. Letzter Cycle-Status
            cycle_row = conn.execute(
                "SELECT id, status FROM lab_cycles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cycle_str = f"#{cycle_row['id']} {cycle_row['status']}" if cycle_row else "–"

            # 4. Aktive Negative Controls
            nc_count = conn.execute(
                "SELECT COUNT(*) FROM negative_controls WHERE closed_at IS NULL"
            ).fetchone()[0] if _table_exists(conn, "negative_controls") else "n/a"

            # 5. Promotion-Kandidaten
            promo_count = conn.execute(
                "SELECT COUNT(*) FROM lab_discoveries WHERE status='approved'"
            ).fetchone()[0] if _table_exists(conn, "lab_discoveries") else "n/a"

            # 6. Watchdog-Status (Heartbeat-Alter)
            from scripts.master_watchdog import check_master_alive
            wdg = check_master_alive()
            wdg_str = f"✅ OK ({wdg['age_min']}min)" if wdg["alive"] else f"🚨 STALE ({wdg['age_min']}min)"

            return {
                "live_dd": live_dd, "open_pos": open_pos, "cycle": cycle_str,
                "nc": nc_count, "promo": promo_count, "watchdog": wdg_str,
            }
        finally:
            conn.close()

    data = await asyncio.get_event_loop().run_in_executor(None, _load_board)

    msg = (
        f"📋 <b>Operator-Board</b>\n\n"
        f"1. Live-DD heute:        <code>{data['live_dd']:.4f}</code>\n"
        f"2. Offene Positionen:    <code>{data['open_pos']}</code>\n"
        f"3. Letzter Cycle:        <code>{data['cycle']}</code>\n"
        f"4. Aktive NCs:           <code>{data['nc']}</code>\n"
        f"5. Promotion-Kandidaten: <code>{data['promo']}</code>\n"
        f"6. Watchdog:             {data['watchdog']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /deploy <ID>
    Aktiviert ein Lab-Discovery als parallele Dry-Run-Instanz.
    Berührt NICHT die laufende squeeze/canary-Konfiguration.
    """
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "❌ Nutzung: `/deploy 42`\n"
            "Die ID findest du im `/alpha`\\-Dashboard\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    disc_id = int(args[0])
    result  = _db_deploy(disc_id)

    if result.get("error"):
        await update.message.reply_text(
            f"❌ {result['error']}",
            parse_mode=ParseMode.MARKDOWN_V2,
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
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Alpha Dashboard", callback_data="alpha"),
            InlineKeyboardButton("◀️ Menü",            callback_data="back_menu"),
        ]]),
    )


async def handle_keyboard_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet Klicks auf das persistente ReplyKeyboard."""
    text = update.message.text

    if text == "📊 Überblick":
        await update.message.reply_text(
            build_dashboard_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_dashboard_keyboard(),
        )
    elif text == "💰 Strategien":
        await update.message.reply_text(
            _build_manage_strategies_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_manage_strategies_keyboard(),
        )
    elif text == "🏆 Top Setups":
        setups = _db_alpha_setups()
        await update.message.reply_text(
            _build_alpha_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_alpha_keyboard(setups),
        )
    elif text == "⚙️ System":
        await update.message.reply_text(
            build_status_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren",    callback_data="status"),
                InlineKeyboardButton("🔌 API testen",       callback_data="api_test"),
                InlineKeyboardButton("◀️ Menü",             callback_data="back_menu"),
            ]]),
        )
    elif text == "📂 Offene Trades":
        await update.message.reply_text(
            build_signals_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Aktualisieren", callback_data="signals"),
                    InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
                ],
                [InlineKeyboardButton("➕ Neues Asset anfragen", callback_data="asset_req_list:0")],
            ]),
        )
    elif text == "🔬 Labor":
        await update.message.reply_text(
            build_lab_main_text(), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_lab_main_keyboard(),
        )

    elif text == "📖 Hilfe":
        await update.message.reply_text(
            _HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
            ]]),
        )


# ── Glossar-Erklärungen für Pop-up-Alerts ────────────────────────────────────
_REJECTION_LABELS = {
    "zu_wenig_trades":    "Zu wenige Trades (n < 40)",
    "pf_zu_niedrig":      "PF zu niedrig (< 1.30)",
    "wr_zu_niedrig":      "Win-Rate zu niedrig (< 48%)",
    "avg_r_zu_niedrig":   "Avg R zu niedrig (< 0.08R)",
    "train_pf_zu_niedrig":"Train-PF zu niedrig (< 1.10)",
    "ueberfit":           "Overfitting (Train↔Test Abweichung)",
    "ruin_drawdown":      "Ruin-Filter (Max DD > $14)",
    "sonstige":           "Sonstige",
}


def _build_lab_stats_text() -> str:
    try:
        from research.auto_lab_daemon import get_lab_stats
        stats = get_lab_stats()
    except Exception as e:
        return f"🧪 *Lab\\-Stats*\n\n❌ Fehler: {e}"

    total   = stats["total_tests"]
    passed  = stats["total_pass"]
    disc    = stats["total_disc"]
    rate    = stats["hit_rate"]
    blinds  = stats["blind_spots"]
    rej     = stats["top_rejection"]

    lines = ["🧪 *Lab\\-Diagnose Dashboard*\n"]
    lines.append(f"🔬 Tests gesamt:   *{total:,}*")
    lines.append(f"✅ Bestanden:      *{passed:,}*")
    lines.append(f"🏆 Discoveries:    *{disc}*")

    rate_icon = "🟢" if rate >= 5 else ("🟡" if rate >= 1 else "🔴")
    lines.append(f"🎯 Hit\\-Rate:      {rate_icon} *{rate:.2f}%*\n")

    if rej:
        lines.append("🚧 *Top Ablehnungsgründe:*")
        total_rej = sum(v for _, v in rej)
        for cat, count in rej[:5]:
            label = _REJECTION_LABELS.get(cat, cat)
            pct   = round(count / total_rej * 100) if total_rej else 0
            bar_w = min(int(pct / 5), 20)
            bar   = "▓" * bar_w + "·" * (20 - bar_w)
            lines.append(f"  `{bar}` {pct}%\n  _{label}_  \\({count:,}\\)")
    else:
        lines.append("🚧 _Noch keine Rejection\\-Daten — Daemon gerade gestartet\\._")

    lines.append("")
    if blinds:
        lines.append(f"🔦 *Blind Spots* \\({len(blinds)} von {7*3} Kombinationen\\):")
        for b in blinds[:5]:
            lines.append(f"  ⚪ `{b}`")
        if len(blinds) > 5:
            lines.append(f"  _\\.\\.\\. und {len(blinds)-5} weitere_")
    else:
        lines.append("🔦 *Blind Spots:* ✅ Alle Kombinationen abgedeckt\\!")

    lines.append(
        "\n_Hit\\-Rate = Anteil Tests, die alle Filter bestehen\\._\n"
        "_Blind Spots = Asset/Regime ohne valides Setup in der DB\\._"
    )
    return _p("\n".join(lines))


def _lab_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❓ Hit-Rate",   callback_data="info_hitrate"),
            InlineKeyboardButton("❓ Rejection",  callback_data="info_rejection"),
            InlineKeyboardButton("❓ Blind Spots",callback_data="info_blindspots"),
        ],
        [
            InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_stats"),
            InlineKeyboardButton("◀️ Menü",           callback_data="back_menu"),
        ],
    ])


_GLOSSAR = {
    # Telegram-Limit: max 200 Zeichen pro show_alert-Text
    "info_score":
        "🎯 Micro-Score = PF ÷ (MaxDD$ / Kapital)\n"
        "Belohnt hohen Edge bei kleinem Drawdown.\n"
        "PF=1.5 / DD=5% schlägt PF=2.0 / DD=40%.\n"
        "Ideal für Micro-Accounts.",

    "info_pf":
        "💰 Profit Factor (PF)\n"
        "= Bruttogewinne ÷ Bruttoverluste\n"
        "PF > 1.0 = positiver Edge\n"
        "Lab-Minimum: PF ≥ 1.30\n"
        "Stark: PF ≥ 1.50",

    "info_wr":
        "🎰 Win-Rate = Gewinner ÷ alle Trades\n"
        "Lab-Minimum: 48%\n"
        "Allein nicht aussagekräftig —\n"
        "entscheidend ist Avg R × WR = Erwartungswert.",

    "info_avgr":
        "📈 Avg R = Ø Gewinn/Verlust in R\n"
        "1R = Risiko pro Trade ($1.50)\n"
        "Avg R +0.10 → +$0.15 je Trade im Schnitt.\n"
        "Lab-Minimum: +0.08R",

    "info_fitness":
        "🏋️ Fitness = PF × min(AvgR,1) × log(n)\n"
        "Kombiniert Edge + Signifikanz.\n"
        "Mehr Trades & höherer AvgR = höherer Score.\n"
        "Für interne Highscore-Vergleiche.",

    "info_maxdd":
        "📉 Max DD = größter kumulativer Verlust\n"
        "Berechnet in R (×$1.50 = USDT).\n"
        "Ruin-Filter: DD > $14 (25% v. $56) → abgelehnt.\n"
        "Schützt den Micro-Account vor Ruin.",

    "info_hitrate":
        "🎯 Hit-Rate = bestandene Tests ÷ Gesamt\n"
        "Filter: n≥40, PF≥1.30, WR≥48%,\n"
        "AvgR≥0.08R, kein Overfit, DD≤$14.\n"
        "Typisch: 0.5–3% — das ist normal.",

    "info_rejection":
        "🚧 Top-Ablehnungsgrund\n"
        "Häufigste Ursachen:\n"
        "1. WR < 48% — zu viele Verlust-Trades\n"
        "2. PF < 1.30 — zu schwacher Edge\n"
        "3. Ruin-Filter — DD > $14",

    "info_blindspots":
        "🔦 Blind Spots\n"
        "= Asset/Regime ohne valides Setup in der DB.\n"
        "Bsp: SOL/TREND_DOWN → kein Setup gefunden.\n"
        "Viele = Lab läuft noch. Wenige = gute Abdeckung.",

    "info_pnl":
        "💵 PnL = Profit & Loss (Gewinn/Verlust)\n"
        "Live-PnL = (Gewinner − Verlierer) × $1.50\n"
        "Basiert auf fixem Risiko: 1R = $1.50 pro Trade.\n"
        "Kein Slippage eingerechnet.",

    "info_canary":
        "🐦 Canary = Probelauf vor Go-Live\n"
        "Strategie läuft im Dry-Run (kein echtes Geld).\n"
        "Ziel: N Trades sammeln und Edge bestätigen.\n"
        "Bei positivem Ergebnis → Upgrade auf LIVE.",
}


def _build_manage_strategies_text() -> str:
    deps = _db_active_deployments()
    if not deps:
        return "💰 *STRATEGIEN VERWALTEN*\n\n_Keine aktiven Strategien\\._\n\n_Wähle ein Setup unter 🏆 Top Setups\\._"
    live_deps = [d for d in deps if d["mode"] == "live"]
    dry_deps  = [d for d in deps if d["mode"] != "live"]
    lines = ["💰 *STRATEGIEN VERWALTEN*\n"]
    if live_deps:
        lines.append("*💰 LIVE*")
        for dep in live_deps:
            name = _deployment_label(dep)
            n    = dep["n"]
            wins = dep["wins"]
            wr   = round(wins / n * 100) if n > 0 else 0
            pnl  = _r_to_usd(dep["total_r"])
            wr_icon = "✅" if wr >= 50 else "⚠️"
            lines.append(
                f"  *{name}*\n"
                f"  {n}/{dep['target_trades']} Trades  ·  {wr}% Trefferquote {wr_icon}  ·  PnL {pnl}"
            )
    if dry_deps:
        lines.append("\n*⚙️ TEST-LÄUFE*")
        for dep in dry_deps:
            name   = _deployment_label(dep)
            n      = dep["n"]
            target = dep["target_trades"]
            pct    = min(int(n / target * 100), 100) if target else 0
            pnl    = _r_to_usd(dep["total_r"])
            lines.append(
                f"  *{name}*\n"
                f"  {n}/{target} Trades  \\({pct}%\\)  ·  PnL {pnl}"
            )
    lines.append("\n_Modus wechseln oder stoppen:_")
    return _p("\n".join(lines))


def _manage_strategies_keyboard() -> InlineKeyboardMarkup:
    deps = _db_active_deployments()
    rows = []
    for dep in deps:
        sk    = dep["strategy_key"]
        mode  = dep["mode"]
        asset = dep["asset"]
        btn_live = InlineKeyboardButton(f"💰 {asset} Live schalten", callback_data=f"dep_mode:{sk}:live")
        btn_dry  = InlineKeyboardButton(f"⚙️ {asset} Test-Lauf",    callback_data=f"dep_mode:{sk}:dry_run")
        btn_stop = InlineKeyboardButton(f"🛑 {asset} Stoppen",       callback_data=f"dep_mode:{sk}:stop")
        if mode == "live":
            rows.append([btn_dry, btn_stop])
        else:
            rows.append([btn_live, btn_stop])
    rows.append([
        InlineKeyboardButton("🔄 Aktualisieren", callback_data="manage_strategies"),
        InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
    ])
    return InlineKeyboardMarkup(rows)


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram.error import BadRequest, TimedOut, Conflict, NetworkError
    err = ctx.error
    if isinstance(err, BadRequest) and "not modified" in str(err).lower():
        return
    if isinstance(err, (TimedOut, NetworkError)):
        return
    if isinstance(err, Conflict):
        return
    print(f"[BOT] Fehler: {type(err).__name__}: {err}")


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_authorized(update):
        await query.answer(text="⛔ Nicht autorisiert.", show_alert=True)
        return
    action = query.data

    # ── Glossar-Pop-ups (show_alert=True) ────────────────────────────────────
    if action in _GLOSSAR:
        await query.answer(text=_GLOSSAR[action], show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass  # Query abgelaufen — ignorieren, Handler läuft trotzdem weiter

    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Aktualisieren", callback_data=action),
        InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
    ]])

    if action == "back_menu" or action == "refresh_menu":
        await query.edit_message_text(
            "📋 *Hauptmenü*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_keyboard(),
        )

    elif action == "dashboard":
        await query.edit_message_text(
            build_dashboard_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_dashboard_keyboard(),
        )

    elif action == "signals":
        await query.edit_message_text(
            build_signals_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_btn,
        )

    elif action == "lab_main":
        await query.edit_message_text(
            build_lab_main_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_lab_main_keyboard(),
        )

    elif action == "lab_diagnose":
        await query.edit_message_text(
            build_lab_diagnose_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_diagnose"),
                InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
            ]]),
        )

    elif action == "lab_radar":
        await query.edit_message_text(
            build_signal_radar_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_radar"),
                InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
            ]]),
        )

    elif action == "lab_suchraum":
        await query.edit_message_text(
            build_lab_suchraum_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Asset anfragen", callback_data="asset_req_list:0")],
                [
                    InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_suchraum"),
                    InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
                ],
            ]),
        )

    elif action == "lab_lernkurve":
        await query.edit_message_text(
            build_lab_lernkurve_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_lernkurve"),
                InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
            ]]),
        )

    elif action == "lab_heatmap":
        await query.edit_message_text(
            build_lab_heatmap_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_heatmap"),
                InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
            ]]),
        )

    elif action == "lab_strategien":
        await query.edit_message_text(
            build_lab_strategien_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_lab_strategien_keyboard(),
        )

    elif action.startswith("lab_sd:"):
        strat = action[len("lab_sd:"):]
        await query.edit_message_text(
            build_lab_strat_detail_text(strat),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"lab_sd:{strat[:20]}"),
                InlineKeyboardButton("◀️ Strategien",    callback_data="lab_strategien"),
            ]]),
        )

    elif action == "lab_funde":
        await query.edit_message_text(
            build_lab_funde_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Top Setups deployen", callback_data="alpha")],
                [
                    InlineKeyboardButton("🔄 Aktualisieren", callback_data="lab_funde"),
                    InlineKeyboardButton("◀️ Labor",         callback_data="lab_main"),
                ],
            ]),
        )

    elif action == "status":
        await query.edit_message_text(
            build_status_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_status_keyboard(),
        )

    elif action == "lab_run_now":
        import subprocess as _sp
        _sp.Popen(
            ["python3", "research/auto_lab_daemon.py", "--single-pass"],
            cwd="/root/apex-v2",
            stdout=open("/root/apex-v2/logs/lab_daemon.log", "a"),
            stderr=_sp.STDOUT,
        )
        await query.edit_message_text(
            "🔬 *Lab Single\\-Pass gestartet*\n\n"
            "_Läuft im Hintergrund \\(\\~5 min\\)\\. Ergebnisse in logs/lab\\_daemon\\.log_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Lab-Status", callback_data="lab_main"),
                InlineKeyboardButton("◀️ Menü",       callback_data="back_menu"),
            ]]),
        )

    elif action == "audit_now":
        import subprocess as _sp2
        _res = _sp2.run(
            ["python3", "-m", "pytest", "tests/governance_invariants.py",
             "tests/parity_test.py", "-v", "--tb=short", "-q"],
            cwd="/root/apex-v2", capture_output=True, text=True, timeout=120,
        )
        out  = (_res.stdout + _res.stderr).strip()[-2800:]
        icon = "✅" if _res.returncode == 0 else "❌"
        await query.edit_message_text(
            f"{icon} *Audit\\-Ergebnis*\n\n```\n{_escape_md(out)}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Nochmal",  callback_data="audit_now"),
                InlineKeyboardButton("◀️ Status",   callback_data="status"),
            ]]),
        )

    elif action == "regime_now":
        from research.train_hmm import get_current_regime
        _conn = get_connection()
        _assets = [r[0] for r in _conn.execute(
            "SELECT DISTINCT asset FROM active_deployments WHERE active=1"
        ).fetchall()]
        _emoji = {"TREND": "🟢", "SIDEWAYS": "🟡", "HIGH_VOL": "🔴"}
        _lines = ["📈 *Aktuelles Regime aller Assets*", ""]
        for _a in _assets:
            try:
                _reg = get_current_regime(_a, _conn)
            except Exception:
                _reg = "?"
            _lines.append(f"  {_emoji.get(_reg, '⚪')} `{_escape_md(_a)}`: {_escape_md(_reg)}")
        _conn.close()
        await query.edit_message_text(
            "\n".join(_lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="regime_now"),
                InlineKeyboardButton("◀️ Status",        callback_data="status"),
            ]]),
        )

    elif action == "portfolio_overview":
        _conn = get_connection()
        from research.train_hmm import get_current_regime
        _deps = _conn.execute(
            """SELECT d.strategy_key, d.base_strategy, d.asset, d.mode,
                      ld.pf_test_netto, COUNT(t.id) n
               FROM active_deployments d
               LEFT JOIN lab_discoveries ld ON ld.id = d.discovery_id
               LEFT JOIN trades t ON t.strategy = d.strategy_key AND t.exit_ts IS NOT NULL
               WHERE d.active=1
               GROUP BY d.strategy_key
               ORDER BY d.mode DESC, d.asset"""
        ).fetchall()
        _lines = ["📋 *Portfolio — Aktive Deployments*", ""]
        _emoji = {"TREND": "🟢", "SIDEWAYS": "🟡", "HIGH_VOL": "🔴"}
        for _d in _deps:
            try:
                _reg = get_current_regime(_d["asset"], _conn)
            except Exception:
                _reg = "?"
            _pf = f"{_d['pf_test_netto']:.2f}" if _d["pf_test_netto"] else "—"
            _mode_ico = "🔴" if _d["mode"] == "live" else "🔬"
            _lines.append(
                f"{_mode_ico} `{_escape_md(_d['asset'])}/{_escape_md(_d['base_strategy'])}` "
                f"\\| n\\=`{_d['n']}` \\| PF\\=`{_escape_md(_pf)}` "
                f"\\| {_emoji.get(_reg, '⚪')}`{_escape_md(_reg)}`"
            )
        _conn.close()
        await query.edit_message_text(
            "\n".join(_lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Aktualisieren", callback_data="portfolio_overview"),
                InlineKeyboardButton("◀️ Menü",          callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("trade_pause_confirm_"):
        _disc_id = int(action[len("trade_pause_confirm_"):])
        _conn = get_connection()
        _row  = _conn.execute(
            "SELECT asset, base_strategy FROM active_deployments WHERE discovery_id=? LIMIT 1",
            (_disc_id,),
        ).fetchone()
        _conn.close()
        _label = f"{_row['asset']}/{_row['base_strategy']}" if _row else f"#{_disc_id}"
        await query.edit_message_text(
            f"⏸ *Deployment pausieren?*\n\n`{_escape_md(_label)}`\n\n"
            "_Das Deployment wird auf active\\=0 gesetzt und sammelt keine neuen Signale mehr\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ja, pausieren", callback_data=f"deploy_pause_{_disc_id}")],
                [InlineKeyboardButton("❌ Abbrechen",     callback_data="dashboard")],
            ]),
        )

    elif action == "help":
        await query.edit_message_text(
            _HELP_TEXT,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
            ]]),
        )

    elif action == "api_test":
        # Delegiert an cmd_api_test — braucht update-Objekt, daher Pseudo-Message-Wrapper
        await query.answer()

        def _run_test() -> dict:
            from execution.bitget_client import BitgetClient
            client = BitgetClient(dry_run=False)
            if not client.is_ready:
                return {"ok": False, "msg": "Keine API-Credentials in config/.env gefunden."}
            try:
                balance = client.get_balance()
                from config.settings import LIVE_ASSETS
                contract_info = {a: client.get_contract_info(a) for a in LIVE_ASSETS}
                return {"ok": True, "balance": balance, "contracts": contract_info}
            except Exception as e:
                err = str(e)
                if "40037" in err or "invalid api key" in err.lower():
                    msg = "❌ Ungültiger API-Key — bitte in Bitget prüfen."
                elif "40039" in err or "ip" in err.lower():
                    msg = "❌ IP nicht auf der Whitelist."
                elif "40034" in err or "permission" in err.lower():
                    msg = "❌ Futures-Handel nicht freigeschaltet."
                elif "429" in err:
                    msg = "❌ Rate-Limit — bitte in 60s erneut versuchen."
                else:
                    msg = f"❌ API-Fehler: {err[:200]}"
                return {"ok": False, "msg": msg}

        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(None, _run_test)
        if not result["ok"]:
            text_out = f"*API\\-Diagnose*\n\n{result['msg']}"
        else:
            bal = result["balance"]
            contract_lines = "\n".join(
                f"  `{a}USDT`: minSize={i.get('min_size','?')}"
                for a, i in result.get("contracts", {}).items()
            )
            text_out = (
                f"*API\\-Diagnose*\n\n"
                f"✅ *Verbunden\\!* Futures\\-Balance: *{bal:.4f} USDT*\n\n"
                f"*Kontrakt\\-Limits:*\n{contract_lines}"
            )
        await query.edit_message_text(
            text_out, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menü", callback_data="back_menu"),
            ]]),
        )

    elif action == "alpha":
        setups = _db_alpha_setups()
        await query.edit_message_text(
            _build_alpha_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_alpha_keyboard(setups),
        )

    elif action == "lab_stats":
        await query.edit_message_text(
            _build_lab_stats_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_lab_stats_keyboard(),
        )

    elif action == "portfolio":
        import asyncio
        text = await asyncio.get_event_loop().run_in_executor(None, _build_pm_main_text)
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_pm_main_keyboard(),
        )

    elif action == "pm_main":
        import asyncio
        text = await asyncio.get_event_loop().run_in_executor(None, _build_pm_main_text)
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_pm_main_keyboard(),
        )

    elif action == "pm_by_asset":
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_by("asset"))
        text, kb = _build_pm_group_list("asset", rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action == "pm_by_strategy":
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_by("strategy"))
        text, kb = _build_pm_group_list("strategy", rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action == "pm_by_regime":
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_by("market_regime"))
        text, kb = _build_pm_group_list("market_regime", rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action == "pm_top":
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, _pm_top)
        text, kb = _build_pm_top_text(rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action == "pm_active":
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, _pm_active)
        text, kb = _build_pm_active_text(rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action == "pm_regimefit":
        import asyncio
        text, kb = await asyncio.get_event_loop().run_in_executor(None, _build_pm_regimefit_text)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("pm_asset_"):
        value = action[len("pm_asset_"):]
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_for("asset", value))
        text, kb = _build_pm_item_list("asset", value, rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("pm_strat_"):
        value = action[len("pm_strat_"):]
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_for("strategy", value))
        text, kb = _build_pm_item_list("strategy", value, rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("pm_regime_"):
        value = action[len("pm_regime_"):]
        import asyncio
        rows = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_list_for("market_regime", value))
        text, kb = _build_pm_item_list("market_regime", value, rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("pm_detail_"):
        disc_id = int(action[len("pm_detail_"):])
        import asyncio
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_detail(disc_id))
        if r is None:
            await query.answer(text="Discovery nicht gefunden.", show_alert=True)
        else:
            text, kb = _build_pm_detail_text(r)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("deploy_live_confirm_"):
        disc_id = int(action[len("deploy_live_confirm_"):])
        import asyncio
        # Alten Score VOR dem Deploy lesen für Feedback-Nachricht
        new_r = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_detail(disc_id))
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _db_deploy(disc_id, mode="live", replace_asset=True)
        )
        if result.get("ok"):
            asset_s    = _escape_md(result["asset"])
            strat_s    = _escape_md(result["strategy_key"])
            new_ms_s   = _escape_md(f"{new_r['micro_score']:.1f}") if new_r else "?"
            replaced_ids = result.get("replaced_ids", [])
            if replaced_ids:
                old_r    = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _pm_detail(replaced_ids[0])
                )
                old_ms_s = _escape_md(f"{old_r['micro_score']:.1f}") if old_r else "?"
                old_id   = replaced_ids[0]
                replace_line = (
                    f"Setup \\#{old_id} deaktiviert \\(Score {old_ms_s}\\)\n"
                    f"Setup \\#{disc_id} aktiv \\(Score {new_ms_s}\\) ▲"
                )
            else:
                replace_line = f"Setup \\#{disc_id} \\(Score {new_ms_s}\\) ist jetzt live"
            msg = (
                f"💰 *{asset_s} — LIVE aktiv*\n\n"
                f"`{strat_s}`\n\n"
                f"{replace_line}"
            )
        else:
            msg = f"⚠️ {_escape_md(result.get('error', 'Deploy fehlgeschlagen'))}"
        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Portfolio", callback_data="pm_main"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("deploy_live_"):
        disc_id = int(action[len("deploy_live_"):])
        import asyncio
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_detail(disc_id))
        asset   = _escape_md(r["asset"]) if r else "?"
        ms_s    = _escape_md(f"{r['micro_score']:.1f}") if r else "?"
        await query.edit_message_text(
            f"⚠️ *{asset} als LIVE deployen?*\n\n"
            f"Setup \\#{disc_id} \\(Score {ms_s}\\) handelt mit echtem Kapital\\.\n"
            f"Ein bestehendes Setup für dieses Asset wird ersetzt\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ja, LIVE deployen", callback_data=f"deploy_live_confirm_{disc_id}")],
                [InlineKeyboardButton("❌ Abbrechen",          callback_data=f"pm_detail_{disc_id}")],
            ]),
        )

    elif action.startswith("deploy_dry_confirm_"):
        disc_id = int(action[len("deploy_dry_confirm_"):])
        import asyncio
        new_r  = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_detail(disc_id))
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _db_deploy(disc_id, mode="dry_run", replace_asset=True)
        )
        if result.get("ok"):
            asset_s      = _escape_md(result["asset"])
            strat_s      = _escape_md(result["strategy_key"])
            new_ms_s     = _escape_md(f"{new_r['micro_score']:.1f}") if new_r else "?"
            replaced_ids = result.get("replaced_ids", [])
            if replaced_ids:
                old_r    = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _pm_detail(replaced_ids[0])
                )
                old_ms_s = _escape_md(f"{old_r['micro_score']:.1f}") if old_r else "?"
                old_id   = replaced_ids[0]
                replace_line = (
                    f"Setup \\#{old_id} deaktiviert \\(Score {old_ms_s}\\)\n"
                    f"Setup \\#{disc_id} aktiv \\(Score {new_ms_s}\\) ▲"
                )
            else:
                replace_line = f"Setup \\#{disc_id} \\(Score {new_ms_s}\\) ist jetzt aktiv"
            msg = (
                f"⚙️ *{asset_s} — Dry\\-Run aktiv*\n\n"
                f"`{strat_s}`\n\n"
                f"{replace_line}"
            )
        else:
            msg = f"⚠️ {_escape_md(result.get('error', 'Deploy fehlgeschlagen'))}"
        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Portfolio", callback_data="pm_main"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("deploy_dry_"):
        disc_id = int(action[len("deploy_dry_"):])
        import asyncio
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: _pm_detail(disc_id))
        asset  = _escape_md(r["asset"]) if r else "?"
        ms_s   = _escape_md(f"{r['micro_score']:.1f}") if r else "?"
        status = r.get("deployment_status", "lab") if r else "lab"
        label  = "⬇️ Zu Dry\\-Run herabstufen?" if status == "live" else "⚙️ Als Dry\\-Run deployen?"
        await query.edit_message_text(
            f"{label}\n\n"
            f"Setup \\#{disc_id} \\(Score {ms_s}\\) für *{asset}*\\.\n"
            f"Ein bestehendes Dry\\-Run\\-Setup für dieses Asset wird ersetzt\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ja, bestätigen", callback_data=f"deploy_dry_confirm_{disc_id}")],
                [InlineKeyboardButton("❌ Abbrechen",       callback_data=f"pm_detail_{disc_id}")],
            ]),
        )

    elif action.startswith("deploy_pause_"):
        disc_id = int(action[len("deploy_pause_"):])
        conn = get_connection()
        conn.execute(
            "UPDATE active_deployments SET active=0 WHERE discovery_id=?", (disc_id,)
        )
        conn.execute(
            "UPDATE lab_discoveries SET deployment_status='lab' WHERE id=?", (disc_id,)
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"⏸ *Deployment \\#{disc_id} pausiert*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Portfolio", callback_data="pm_main"),
            ]]),
        )

    elif action.startswith("cio_dry:") or action.startswith("cio_live:"):
        disc_id = int(action.split(":")[1])
        mode    = "dry_run" if action.startswith("cio_dry:") else "live"
        # Asset aus der DB holen (kein Asset im callback_data)
        conn = get_connection()
        row  = conn.execute("SELECT asset FROM lab_discoveries WHERE id=?", (disc_id,)).fetchone()
        conn.close()
        asset = row["asset"] if row else "?"

        if mode == "live":
            await query.edit_message_text(
                f"⚠️ *{asset} live schalten?*\n\n"
                f"Setup \\#{disc_id} wird mit echtem Kapital gehandelt\\.\n"
                f"Bestehende {asset}\\-Instanzen werden gestoppt\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"✅ Ja, {asset} LIVE",
                        callback_data=f"cio_confirmed_live:{disc_id}",
                    )],
                    [InlineKeyboardButton("❌ Abbrechen", callback_data="portfolio")],
                ]),
            )
        else:
            import asyncio
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _db_deploy(disc_id, mode="dry_run", replace_asset=True)
            )
            if r.get("ok"):
                msg = (
                    f"⚙️ *{asset} Dry\\-Run gestartet*\n\n"
                    f"Instanz: `{r['strategy_key']}`\n"
                    f"Ziel: {r['target_trades']} Trades\n"
                    f"_Vorherige {asset}\\-Instanzen gestoppt\\._"
                )
            else:
                msg = f"⚠️ {r.get('error','Deploy fehlgeschlagen')}"
            await query.edit_message_text(
                msg, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Portfolio", callback_data="portfolio"),
                    InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
                ]]),
            )

    elif action.startswith("cio_confirmed_live:"):
        disc_id = int(action.split(":")[1])
        conn = get_connection()
        row  = conn.execute("SELECT asset FROM lab_discoveries WHERE id=?", (disc_id,)).fetchone()
        conn.close()
        asset = row["asset"] if row else "?"
        import asyncio
        r = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _db_deploy(disc_id, mode="live", replace_asset=True)
        )
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
            msg, parse_mode=ParseMode.MARKDOWN_V2,
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
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🔥 Ja, alle {n} LIVE",
                    callback_data=f"cio_all_live_execute:{ids_str}",
                )],
                [InlineKeyboardButton("❌ Abbrechen", callback_data="portfolio")],
            ]),
        )

    elif action == "manage_strategies":
        await query.edit_message_text(
            _build_manage_strategies_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_manage_strategies_keyboard(),
        )

    elif action.startswith("dep_mode:"):
        parts = action.split(":")
        sk    = parts[1]
        mode  = parts[2]

        def _do_dep_mode():
            import sqlite3 as _sq, time as _t
            _db = os.path.abspath(DB_PATH)
            for attempt in range(60):
                try:
                    c = _sq.connect(_db, timeout=5, check_same_thread=False)
                    c.row_factory = _sq.Row
                    if mode == "stop":
                        c.execute("UPDATE active_deployments SET active=0 WHERE strategy_key=?", (sk,))
                    else:
                        c.execute("UPDATE active_deployments SET mode=? WHERE strategy_key=? AND active=1", (mode, sk))
                    c.commit()
                    c.close()
                    return
                except _sq.OperationalError as e:
                    try: c.close()
                    except Exception: pass
                    if "locked" in str(e) and attempt < 59:
                        _t.sleep(0.05)
                        continue
                    raise

        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, _do_dep_mode)

        if mode == "stop":
            await query.edit_message_text(
                f"🗑️ *{sk}* gestoppt\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Dashboard", callback_data="dashboard"),
                ]]),
            )
        else:
            label = "🔴 LIVE" if mode == "live" else "🧪 DRY-RUN"
            await query.edit_message_text(
                f"{label} — `{sk}` umgeschaltet auf *{mode}*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_manage_strategies_keyboard(),
            )

    elif action.startswith("dep_info:"):
        asset    = action.split(":")[1]
        deployed = _active_deployment_for(asset)
        if deployed:
            mode_label = "LIVE" if deployed["mode"] == "live" else "DRY-RUN"
            await query.answer(
                text=f"{asset} läuft bereits als {mode_label}: {deployed['strategy_key']}",
                show_alert=True,
            )
        else:
            await query.answer(text=f"{asset}: kein aktives Deployment gefunden.")

    elif action == "noop":
        pass

    elif action.startswith("asset_req_list:"):
        page = int(action.split(":")[1])
        import asyncio
        assets    = await asyncio.get_event_loop().run_in_executor(None, _fetch_bitget_futures_assets)
        requested = {r["asset"] for r in _db_asset_requests()}
        text_out  = _build_asset_request_list_text(assets, requested)
        kb        = _build_asset_request_list_keyboard(assets, requested, page)
        await query.edit_message_text(text_out, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("asset_req_detail:"):
        asset  = action.split(":", 1)[1]
        import asyncio
        assets = await asyncio.get_event_loop().run_in_executor(None, _fetch_bitget_futures_assets)
        info   = next((a for a in assets if a["asset"] == asset), None)
        reqs   = {r["asset"]: r["status"] for r in _db_asset_requests()}

        if asset in reqs:
            status_de = {"pending": "⏳ Wartend", "in_progress": "🔬 In Bearbeitung", "done": "✅ Fertig"}.get(reqs[asset], reqs[asset])
            text_out = (
                f"*{asset}/USDT*\n\n"
                f"Status: {status_de}\n\n"
                f"_Dieses Asset wurde bereits angefragt._\n"
                f"_Das Lab testet es beim nächsten Zyklus._"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Zurück", callback_data="asset_req_list:0"),
                InlineKeyboardButton("◀️ Menü",   callback_data="back_menu"),
            ]])
        else:
            vol_m = (info["volume_usd"] / 1_000_000) if info else 0
            price = info["price"] if info else "?"
            text_out = (
                f"*{asset}/USDT*\n\n"
                f"Volumen (24h): ${vol_m:.0f}M\n"
                f"Aktueller Preis: `{price}`\n"
                f"Im System: Nein\n\n"
                f"Das Lab wird *{asset}* beim nächsten Zyklus automatisch testen.\n"
                f"Kerzen werden vorher via Binance geladen."
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ {asset} anfragen", callback_data=f"asset_req_confirm:{asset}"),
                    InlineKeyboardButton("◀️ Zurück",            callback_data="asset_req_list:0"),
                ],
            ])
        await query.edit_message_text(text_out, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    elif action.startswith("asset_req_confirm:"):
        asset  = action.split(":", 1)[1]
        result = _db_request_asset(asset)
        if result == "created":
            text_out = (
                f"✅ *{asset} wurde angefragt\\!*\n\n"
                f"{asset}/USDT wurde zur Lab\\-Warteschlange hinzugefügt\\.\n"
                f"Das Lab testet es bei der nächsten freien Runde\\.\n\n"
                f"_Status: Wartend_"
            )
        else:
            text_out = (
                f"ℹ️ *{asset} bereits angefragt*\n\n"
                f"Dieses Asset wurde schon zur Warteschlange hinzugefügt\\."
            )
        await query.edit_message_text(
            text_out, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Weiteres Asset anfragen", callback_data="asset_req_list:0"),
                InlineKeyboardButton("◀️ Menü",                   callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("promote_confirm_"):
        dep_id = int(action[len("promote_confirm_"):])
        import asyncio
        from datetime import datetime, timezone as _tz

        def _do_promote():
            conn = get_connection()
            try:
                dep = conn.execute(
                    "SELECT strategy_key, base_strategy, asset, mode, active "
                    "FROM active_deployments WHERE id=?",
                    (dep_id,),
                ).fetchone()
                if not dep:
                    return {"error": f"Deployment \\#{dep_id} nicht gefunden\\."}
                dep = dict(dep)
                if dep["mode"] != "dry_run" or not dep["active"]:
                    return {"error": "Deployment ist nicht im dry\\_run\\-Modus oder inaktiv\\."}
                now = datetime.now(_tz.utc).isoformat()
                conn.execute(
                    "UPDATE active_deployments SET mode='live' WHERE id=?",
                    (dep_id,),
                )
                conn.execute(
                    "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
                    "VALUES (?,?,?,?,?)",
                    (now, "promote", "ok",
                     f"promoted dep_id={dep_id} {dep['strategy_key']}/{dep['asset']} dry_run→live",
                     0),
                )
                conn.commit()
                return {"ok": True, "strategy_key": dep["strategy_key"], "asset": dep["asset"]}
            finally:
                conn.close()

        result = await asyncio.get_event_loop().run_in_executor(None, _do_promote)

        if result.get("ok"):
            sk    = _escape_md(result["strategy_key"])
            asset = _escape_md(result["asset"])
            msg   = (
                f"🔴 *LIVE: {asset}*\n\n"
                f"`{sk}` ist ab sofort aktiv\\.\n"
                f"Deployment \\#{dep_id} auf `live` gesetzt\\."
            )
        else:
            msg = f"⚠️ {result.get('error', 'Promote fehlgeschlagen\\.')}"

        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Portfolio", callback_data="pm_main"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )

    elif action.startswith("cio_all_live_execute:"):
        ids     = [int(x) for x in action.split(":", 1)[1].split(",") if x]
        import asyncio
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda: [_db_deploy(i, mode="live", replace_asset=True) for i in ids]
        )
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
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"),
                InlineKeyboardButton("◀️ Menü",      callback_data="back_menu"),
            ]]),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Push-Jobs (job_queue)
# ══════════════════════════════════════════════════════════════════════════════

def _trade_edge_info(strategy: str, asset: str, trade_id: int) -> dict:
    """Lädt Regime, OOS-PF (netto), DSR und Size-Modifier für einen abgeschlossenen Trade."""
    from research.train_hmm import get_current_regime
    conn = get_connection()

    # Regime live aus HMM
    try:
        regime = get_current_regime(asset, conn)
    except Exception:
        regime = "?"

    # pf_test_netto + dsr aus active_deployments JOIN lab_discoveries
    row = conn.execute(
        """SELECT ld.pf_test_netto, ld.dsr
           FROM active_deployments ad
           JOIN lab_discoveries ld ON ld.id = ad.discovery_id
           WHERE ad.strategy_key = ? AND ad.active = 1
           LIMIT 1""",
        (strategy,),
    ).fetchone()
    pf_netto = row["pf_test_netto"] if row and row["pf_test_netto"] else None
    dsr      = row["dsr"]           if row and row["dsr"]           else None

    # REGIME_HALF aus governance_log für diesen Trade (signal derselben Strategie)
    gl_row = conn.execute(
        """SELECT reason FROM governance_log
           WHERE signal_id IN (
               SELECT id FROM signals
               WHERE strategy = ? AND asset = ?
               ORDER BY created_at DESC LIMIT 5
           )
           AND reason LIKE '%REGIME_HALF%'
           ORDER BY created_at DESC LIMIT 1""",
        (strategy, asset),
    ).fetchone()
    size_mod = "HALF" if gl_row else "FULL"

    conn.close()
    return {"regime": regime, "pf_netto": pf_netto, "dsr": dsr, "size_mod": size_mod}


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
        # Tages-PnL für Kontext-Zeile
        d_summary = _db_pnl_summary()
        _regime_emoji = {"TREND": "🟢", "SIDEWAYS": "🟡", "HIGH_VOL": "🔴"}
        for t in new:
            r_val     = t["pnl_r"] or 0
            pnl_usd   = _r_to_usd(r_val)
            icon      = "✅" if r_val > 0 else "❌"
            dir_label = "Long" if t["direction"] == "long" else "Short"
            mode_lbl  = _mode_label(t["mode"])
            reason    = _exit_label(t["exit_reason"] or "")

            edge      = _trade_edge_info(t["strategy"], t["asset"], t["id"])
            reg_ico   = _regime_emoji.get(edge["regime"], "⚪")
            pf_line   = (
                f"📊 OOS\\-PF \\(Netto\\): `{edge['pf_netto']:.2f}`"
                if edge["pf_netto"] else "📊 OOS\\-PF \\(Netto\\): `—`"
            )
            dsr_line  = (
                f"🎯 Edge\\-Confidence: `{edge['dsr']:.3f}`"
                if edge["dsr"] else "🎯 Edge\\-Confidence: `—`"
            )
            msg = (
                f"{icon} *Trade abgeschlossen*  ·  {mode_lbl}\n\n"
                f"{_escape_md(t['asset'])} {dir_label}\n"
                f"Ergebnis: *{pnl_usd}*  \\({_fmt_r(r_val)}\\)  —  {reason}\n"
                f"Einstieg: `{_escape_md(str(t['entry_price']))}`  →  "
                f"Ausstieg: `{_escape_md(str(t['exit_price']))}`\n\n"
                f"📍 Regime: {_escape_md(edge['regime'])} {reg_ico}\n"
                f"{pf_line}\n"
                f"⚖️ Size\\-Modifier: `{edge['size_mod']}`\n"
                f"{dsr_line}\n\n"
                f"_Heute: {_r_to_usd(d_summary['today_r'])}  ·  "
                f"{d_summary['total_n']} Trades gesamt_"
            )
            # discovery_id für Pause-Button aus active_deployments
            _conn = get_connection()
            _dep_row = _conn.execute(
                "SELECT discovery_id FROM active_deployments WHERE strategy_key=? AND active=1 LIMIT 1",
                (t["strategy"],),
            ).fetchone()
            _conn.close()
            _disc_id = _dep_row["discovery_id"] if _dep_row else None
            trade_kb_rows = [[InlineKeyboardButton("📊 Details", callback_data="dashboard")]]
            if _disc_id:
                trade_kb_rows.insert(0, [
                    InlineKeyboardButton(
                        f"⏸ Pause {t['asset']}", callback_data=f"trade_pause_confirm_{_disc_id}"
                    ),
                ])
            await ctx.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(trade_kb_rows),
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
    dead_readable = [
        f"`{_component_label(hb['component'])}` seit {int((now - datetime.fromisoformat(hb['ts'])).total_seconds() / 60)} Min"
        for hb in hbs
        if HEARTBEAT_MAX_AGE_MIN.get(hb["component"], 30) * 2 < (
            (now - datetime.fromisoformat(hb["ts"])).total_seconds() / 60
            if "T" in hb.get("ts", "") else 999
        )
    ] or [f"`{d}`" for d in dead]
    msg = (
        "🔴 *WARNUNG: Pipeline\\-Ausfall*\n\n"
        + "\n".join(dead_readable)
        + "\n\n_Andere Komponenten laufen normal\\._"
    )
    await ctx.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Status prüfen", callback_data="status"),
        ]]),
    )


async def push_daily_status(ctx: ContextTypes.DEFAULT_TYPE):
    """Täglicher Status-Report (wird vom job_queue um 08:00 UTC getriggert)."""
    d    = _db_pnl_summary()
    can  = _db_canary()
    deps = _db_active_deployments()
    now  = datetime.now(timezone.utc)

    today_usd = _r_to_usd(d["today_r"])
    total_usd = _r_to_usd(d["total_r"])
    avg_usd   = _r_to_usd(d["avg_r"])

    lines = [
        f"📅 *TAGESBERICHT  ·  {now.strftime('%d. %b %Y')}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "*══ PERFORMANCE ══════════════════*",
        f"Trades gesamt: *{d['total_n']}*",
        f"Gesamt PnL:    *{total_usd}*  ({_fmt_r(d['total_r'])})",
        f"Heute:         *{today_usd}*",
        f"Ø pro Trade:   *{avg_usd}*",
    ]

    if deps:
        lines += ["", "*══ AKTIVE STRATEGIEN ════════════*"]
        for dep in deps:
            name   = _deployment_label(dep)
            n      = dep["n"]
            target = dep["target_trades"]
            t_r    = dep["total_r"]
            wins   = dep["wins"]
            pct    = min(int(n / target * 100), 100) if target else 0
            badge  = ("🟢 Bereit" if t_r > 0 else "🔴 Verfehlt") if n >= target else f"⏳ {pct}%"
            mode_i = "💰" if dep["mode"] == "live" else "⚙️"
            lines.append(f"  {mode_i} {name}  {n}/{target}  {badge}  PnL {_r_to_usd(t_r)}")
    else:
        lines += ["", "*══ TEST-LÄUFE (Basis) ════════════*"]
        for asset, ref in LAB_REF.items():
            target = _canary_target(asset)
            c      = can.get(asset)
            n      = c["n"]      if c else 0
            t_r    = c["total_r"] if c else 0.0
            pct    = min(int(n / target * 100), 100) if target else 0
            badge  = ("🟢" if t_r > 0 else "🔴") if n >= target else "⏳"
            lines.append(f"  {badge} `{asset}`  {n}/{target}  ({pct}%)  PnL {_r_to_usd(t_r)}")

    lines += ["", "*══ MARKT HEUTE ══════════════════*", _db_market_weather_de()]

    await ctx.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Dashboard",  callback_data="dashboard"),
            InlineKeyboardButton("🏆 Top Setups", callback_data="alpha"),
        ]]),
    )


async def push_daily_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """Täglich 08:00 CEST: kompakter Daily Digest mit Regime, Lab und Alerts."""
    from zoneinfo import ZoneInfo
    now    = datetime.now(ZoneInfo("Europe/Berlin"))
    datum  = _escape_md(now.strftime("%d.%m.%Y"))
    heute  = now.strftime("%Y-%m-%d")

    conn = get_connection()

    # ── Live & Dry-Run Deployments ────────────────────────────────────────────
    deps = conn.execute(
        "SELECT id, strategy_key, base_strategy, asset, mode "
        "FROM active_deployments WHERE active=1 ORDER BY mode DESC"
    ).fetchall()

    live_lines    = []
    dry_lines     = []
    best_dry_pf   = 0.0
    best_dry_asset = "—"

    for dep in deps:
        dep = dict(dep)
        sk  = dep["strategy_key"]
        ast = dep["asset"]

        today_rows = conn.execute(
            "SELECT pnl_r FROM trades "
            "WHERE strategy=? AND asset=? AND exit_ts IS NOT NULL "
            "AND exit_ts >= ?",
            (sk, ast, heute),
        ).fetchall()
        today_r = round(sum(r[0] for r in today_rows if r[0]), 2)

        all_rows = conn.execute(
            "SELECT pnl_r FROM trades WHERE strategy=? AND asset=? AND exit_ts IS NOT NULL",
            (sk, ast),
        ).fetchall()
        n = len(all_rows)
        gross_win  = sum(r[0] for r in all_rows if r[0] and r[0] > 0)
        gross_loss = abs(sum(r[0] for r in all_rows if r[0] and r[0] < 0))
        pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

        sign = "+" if today_r >= 0 else ""
        r_str = _escape_md(f"{sign}{today_r:.2f}R")

        if dep["mode"] == "live":
            live_lines.append(
                f"🔴 Live: `{_escape_md(ast)}/{_escape_md(dep['base_strategy'])}` "
                f"\\| n\\=`{n}` \\| PnL heute: `{r_str}`"
            )
        else:
            if pf and pf > best_dry_pf:
                best_dry_pf   = pf
                best_dry_asset = ast
            dry_lines.append(dep)

    # ── HMM-Regime (live aus Modell) ──────────────────────────────────────────
    from research.train_hmm import get_current_regime
    dep_assets = list(dict.fromkeys(dict(d)["asset"] for d in deps))
    regime_emoji = {"TREND": "🟢", "SIDEWAYS": "🟡", "HIGH_VOL": "🔴"}
    regime_lines = []
    for ast in dep_assets:
        try:
            reg = get_current_regime(ast, conn)
        except Exception:
            reg = "?"
        ico = regime_emoji.get(reg, "⚪")
        regime_lines.append(f"  {ico} `{_escape_md(ast)}`: {_escape_md(reg)}")

    # ── Lab-Stats ─────────────────────────────────────────────────────────────
    disc_heute = conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE discovered_at >= ?",
        (heute,),
    ).fetchone()[0]
    promoted_heute = conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries "
        "WHERE deployment_status IN ('dry','live') AND deployed_at >= ?",
        (heute,),
    ).fetchone()[0]

    # ── Alerts: DD-Warns + HMM_WARNs heute ───────────────────────────────────
    alert_rows = conn.execute(
        "SELECT COUNT(*) FROM governance_log "
        "WHERE (reason LIKE '%HALF_SIZE%' OR reason LIKE '%HMM_WARN%') "
        "AND ts >= ?",
        (heute,),
    ).fetchone()[0]

    conn.close()

    # ── Nachricht zusammenbauen ───────────────────────────────────────────────
    lines = [f"📊 *APEX V2 — Daily Digest {datum}*", ""]

    if live_lines:
        lines += live_lines
    else:
        lines.append("🔴 Live: _kein aktives Live\\-Deployment_")

    n_dry = len(dry_lines)
    lines.append(
        f"🔬 Dry\\-Run: `{n_dry}` aktive Deployments"
        + (f" \\| beste: `{_escape_md(best_dry_asset)}`" if best_dry_asset != "—" else "")
    )

    lines += ["", "📡 *Regime:*"]
    lines += regime_lines if regime_lines else ["  _keine Daten_"]
    lines.append("_🟢 TREND \\| 🟡 SIDEWAYS \\| 🔴 HIGH\\_VOL_")

    lines += [
        "",
        f"⚗️ *Lab:* `{disc_heute}` neue Discoveries \\| `{promoted_heute}` promoted",
    ]

    if alert_rows:
        lines.append(f"⚠️ *Alerts:* `{alert_rows}` DD\\-Warn/HMM\\_WARN heute")
    else:
        lines.append("✅ *Alerts:* keine")

    await ctx.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Portfolio", callback_data="portfolio_overview"),
            InlineKeyboardButton("⚙️ Status",    callback_data="status"),
        ]]),
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
                            wins: int, disc_id: int = 0) -> None:
        losses    = n - wins
        pf        = (wins / losses) if losses > 0 else 999.0
        wr        = round(wins / n * 100, 1) if n > 0 else 0
        pnl_usd   = _r_to_usd(total_r)
        name      = f"{asset} · {_regime_label(regime)}"
        if total_r > 0:
            msg = (
                f"🟢 *TEST-LAUF BESTANDEN\\!*\n\n"
                f"*{name}* ist bereit für Live-Trading\\.\n\n"
                f"{n} Trades  ·  {wr}% Trefferquote\n"
                f"PnL: *{pnl_usd}*  ·  PF: *{pf:.2f}*\n\n"
                f"✅ Empfehlung: Auf Live schalten"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Jetzt Live schalten", callback_data=f"deploy_live_{disc_id}") if disc_id else
                InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"),
                InlineKeyboardButton("📊 Details", callback_data="dashboard"),
            ]])
        else:
            msg = (
                f"🔴 *TEST-LAUF NICHT BESTANDEN*\n\n"
                f"*{name}* hat den Forward\\-Test verfehlt\\.\n\n"
                f"{n} Trades  ·  {wr}% Trefferquote\n"
                f"PnL: *{pnl_usd}*  ·  PF: *{pf:.2f}*\n\n"
                f"⚠️ Empfehlung: Deployment stoppen"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛑 Deployment stoppen", callback_data=f"dep_mode:{sk}:stop"),
                InlineKeyboardButton("📊 Dashboard",          callback_data="dashboard"),
            ]])
        await ctx.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
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
                            n, target, total_r, dep["wins"],
                            disc_id=dep.get("discovery_id", 0))
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
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
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
    app.add_handler(CommandHandler("alpha",     cmd_alpha))
    app.add_handler(CommandHandler("lab_stats", cmd_lab_stats))
    app.add_handler(CommandHandler("deploy",   cmd_deploy))
    app.add_handler(CommandHandler("promote",  cmd_promote))
    app.add_handler(CommandHandler("shadow",   cmd_shadow))
    app.add_handler(CommandHandler("board",    cmd_board))
    app.add_handler(CommandHandler("api_test",  cmd_api_test))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(_error_handler)

    # Persistentes ReplyKeyboard — Textnachrichten der Buttons abfangen
    _kb_buttons = {
        "📊 Überblick", "💰 Strategien", "🏆 Top Setups",
        "📂 Offene Trades", "🔬 Labor", "⚙️ System",
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

    # Daily Digest: täglich 08:00 CEST (Europe/Berlin)
    from zoneinfo import ZoneInfo as _ZI
    import datetime as _dt
    jq.run_daily(
        push_daily_digest,
        time=_dt.time(8, 0, tzinfo=_ZI("Europe/Berlin")),
    )

    # Go-Live Check: alle 15 Minuten
    jq.run_repeating(push_go_live_check, interval=900, first=60)

    print("[BOT] Polling gestartet — Ctrl+C zum Beenden")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
