#!/usr/bin/env python3
"""
APEX Lab Safety Bridge — Proaktives Lab-Monitoring

Prüft alle kritischen Lab-Invarianten und sendet Telegram-Alerts.
Läuft täglich 1× via Cron (empfohlen: 08:00 UTC).

Geprüfte Bereiche:
  1. Datenbasis     — reicht die History für alle WF-Fenster?
  2. Lab-Aktivität  — findet der Daemon überhaupt neue Strategien?
  3. Signal-Dedup   — häufen sich NULL-signal_key Duplikate?
  4. Execution      — werden Signale dauerhaft aborted? Warum?
  5. Deployments    — performen aktive Strategien noch? Shadow-Alarm?
  6. Lab-Drift      — keine neuen Discoveries in N Tagen?
"""

import sys
import os
import json
import sqlite3
import requests
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "apex_v2.db")

# ── Thresholds ────────────────────────────────────────────────────────────────

# WF-Fenster brauchen mind. diese Candle-History (in Tagen)
WF_MIN_DAYS = 500          # 480d train + puffer

# Alert wenn Lab seit N Tagen keine neue Discovery gefunden hat
LAB_STALE_DAYS = 5

# Alert wenn mehr als X% aller Signale der letzten 7 Tage execution_aborted sind
ABORT_RATE_THRESHOLD = 0.50

# Alert wenn mehr als X NULL-signal_key Duplikate in den letzten 7 Tagen
NULL_KEY_THRESHOLD = 20

# Alert wenn aktive Deployment WR in letzten 30 Trades < X%
LIVE_WR_MIN = 40.0


def _send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[LAB-BRIDGE] (kein Telegram) {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[LAB-BRIDGE] Telegram-Fehler: {e}")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 1: Datenbasis
# ─────────────────────────────────────────────────────────────────────────────
def check_data_coverage() -> list[str]:
    issues = []
    conn = _conn()
    now = datetime.now(timezone.utc)
    required_start = now - timedelta(days=WF_MIN_DAYS)

    assets = conn.execute(
        "SELECT DISTINCT asset FROM candles WHERE interval='1h'"
    ).fetchall()

    for row in assets:
        asset = row["asset"]
        res = conn.execute(
            "SELECT MIN(ts) as earliest, COUNT(*) as n FROM candles WHERE asset=? AND interval='1h'",
            (asset,),
        ).fetchone()
        if not res or not res["earliest"]:
            issues.append(f"  ⚠️ {asset}: keine 1h-Daten")
            continue

        earliest_dt = datetime.fromtimestamp(res["earliest"] / 1000, tz=timezone.utc)
        gap_days = (earliest_dt - required_start).days

        if earliest_dt > required_start:
            issues.append(
                f"  ⚠️ {asset}: Daten ab {earliest_dt.strftime('%Y-%m-%d')} "
                f"({gap_days}d zu kurz für WF-Window 1)"
            )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 2: Lab-Aktivität (neue Discoveries)
# ─────────────────────────────────────────────────────────────────────────────
def check_lab_activity() -> list[str]:
    issues = []
    conn = _conn()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=LAB_STALE_DAYS)).isoformat()

    # Letzte Discovery
    last = conn.execute(
        "SELECT discovered_at FROM lab_discoveries ORDER BY discovered_at DESC LIMIT 1"
    ).fetchone()

    if not last:
        issues.append("  🔴 Lab: Noch nie eine Discovery gefunden")
        conn.close()
        return issues

    last_dt = datetime.fromisoformat(last["discovered_at"].replace("Z", "+00:00"))
    age_days = (now - last_dt).days

    if age_days >= LAB_STALE_DAYS:
        issues.append(
            f"  🔴 Lab: Letzte Discovery vor {age_days}d ({last_dt.strftime('%Y-%m-%d')}) "
            f"— Daemon läuft aber findet nichts"
        )

    # Rejection-Muster aus lab_stats
    stats = conn.execute("SELECT key, value FROM lab_stats").fetchall()
    stat_dict = {r["key"]: r["value"] for r in stats}
    total = stat_dict.get("total_tests", 0)
    zu_wenig = stat_dict.get("reject_zu_wenig_trades", 0)

    if total > 0 and zu_wenig / total > 0.99:
        issues.append(
            f"  ⚠️ Lab: {zu_wenig}/{total} Tests scheitern an n_test "
            f"— History wahrscheinlich zu kurz (WF-Fenster prüfen)"
        )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 3: Signal-Dedup (NULL signal_key)
# ─────────────────────────────────────────────────────────────────────────────
def check_signal_dedup() -> list[str]:
    issues = []
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    null_count = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE signal_key IS NULL AND created_at >= ?",
        (cutoff,),
    ).fetchone()[0]

    if null_count >= NULL_KEY_THRESHOLD:
        # Welche Strategien sind betroffen?
        rows = conn.execute(
            """SELECT strategy, COUNT(*) as n FROM signals
               WHERE signal_key IS NULL AND created_at >= ?
               GROUP BY strategy ORDER BY n DESC LIMIT 5""",
            (cutoff,),
        ).fetchall()
        detail = ", ".join(f"{r['strategy']}×{r['n']}" for r in rows)
        issues.append(
            f"  ⚠️ Dedup: {null_count} Signale ohne signal_key (7d) — {detail}\n"
            f"    → squeeze.py prüft return-Wert von _save_signal nicht"
        )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 4: Execution — Abort-Rate
# ─────────────────────────────────────────────────────────────────────────────
def check_execution_aborts() -> list[str]:
    issues = []
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    total = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE created_at >= ?", (cutoff,)
    ).fetchone()[0]

    aborted = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE reject_reason='execution_aborted' AND created_at >= ?",
        (cutoff,),
    ).fetchone()[0]

    if total > 0 and aborted / total >= ABORT_RATE_THRESHOLD:
        # Welche Strategien?
        rows = conn.execute(
            """SELECT strategy, COUNT(*) as n FROM signals
               WHERE reject_reason='execution_aborted' AND created_at >= ?
               GROUP BY strategy ORDER BY n DESC LIMIT 5""",
            (cutoff,),
        ).fetchall()
        detail = ", ".join(f"{r['strategy']}×{r['n']}" for r in rows)
        rate = int(aborted / total * 100)
        issues.append(
            f"  🔴 Execution: {rate}% Abort-Rate ({aborted}/{total} Signale, 7d)\n"
            f"    Betroffene Strategien: {detail}\n"
            f"    → Sizing-Prüfung (Balance zu klein?) oder Deploy-Mode falsch"
        )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 5: Deployment-Performance
# ─────────────────────────────────────────────────────────────────────────────
def check_deployments() -> list[str]:
    issues = []
    conn = _conn()
    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    # Aktive Deployments
    deployments = conn.execute(
        "SELECT id, strategy_key, asset, mode, target_trades FROM active_deployments WHERE active=1"
    ).fetchall()

    for dep in deployments:
        key = dep["strategy_key"]

        # Abgeschlossene Trades der letzten 30 Tage
        closed_trades = conn.execute(
            """SELECT pnl_r FROM trades
               WHERE strategy=? AND entry_ts >= ? AND exit_ts IS NOT NULL""",
            (key, cutoff_30d),
        ).fetchall()

        if len(closed_trades) >= 10:
            wins = sum(1 for t in closed_trades if t[0] is not None and t[0] > 0)
            wr = wins / len(closed_trades) * 100
            if wr < LIVE_WR_MIN:
                issues.append(
                    f"  🔴 Live {key}: WR {wr:.0f}% < {LIVE_WR_MIN}% "
                    f"({wins}/{len(closed_trades)} Trades, 30d) — Edge verloren?"
                )

        # Signale als Fortschritts-Proxy
        closed = conn.execute(
            """SELECT COUNT(*) FROM signals
               WHERE strategy=? AND status='executed' AND created_at >= ?""",
            (key, cutoff_30d),
        ).fetchone()[0]

        # Shadow-Deployments ohne Canary-Fortschritt warnen
        if dep["mode"] == "shadow" and closed == 0:
            # Wann deployed?
            deployed_row = conn.execute(
                "SELECT deployed_at FROM active_deployments WHERE strategy_key=?", (key,)
            ).fetchone()
            if deployed_row:
                dep_dt = datetime.fromisoformat(deployed_row[0].replace("Z", "+00:00"))
                dep_age = (now - dep_dt).days
                if dep_age > 14:
                    issues.append(
                        f"  ⚠️ Shadow {key}: seit {dep_age}d deployed, 0 Canary-Trades "
                        f"— Strategie triggert nicht"
                    )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 6: Strategy-Freeze (keine approved Signale trotz laufendem Lab)
# ─────────────────────────────────────────────────────────────────────────────
def check_signal_flow() -> list[str]:
    issues = []
    conn = _conn()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Wurden in den letzten 24h überhaupt Signale erzeugt (auch failed)?
    recent = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE created_at >= ?", (cutoff_24h,)
    ).fetchone()[0]

    if recent == 0:
        issues.append(
            "  🔴 Pipeline: 0 Signale in den letzten 24h — Strategies laufen nicht?"
        )
    else:
        # Wurden irgendwelche executed?
        executed = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status='executed' AND created_at >= ?",
            (cutoff_24h,),
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status='pending' AND created_at >= ?",
            (cutoff_24h,),
        ).fetchone()[0]
        if pending > 10:
            issues.append(
                f"  ⚠️ Pipeline: {pending} Signale stecken als 'pending' (24h) "
                f"— Governance oder Executor hängt?"
            )

    conn.close()
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Hauptlauf
# ─────────────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"[LAB-BRIDGE] Start {now.strftime('%Y-%m-%d %H:%M UTC')}")

    checks = [
        ("Datenbasis",        check_data_coverage),
        ("Lab-Aktivität",     check_lab_activity),
        ("Signal-Dedup",      check_signal_dedup),
        ("Execution",         check_execution_aborts),
        ("Deployments",       check_deployments),
        ("Signal-Flow",       check_signal_flow),
    ]

    all_issues: list[tuple[str, list[str]]] = []
    for name, fn in checks:
        try:
            issues = fn()
            if issues:
                all_issues.append((name, issues))
        except Exception as e:
            all_issues.append((name, [f"  🔴 Check-Fehler: {e}"]))

    if not all_issues:
        msg = (
            f"✅ *Lab Safety Bridge* | {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Alle {len(checks)} Checks grün — Lab, Pipeline und Deployments OK."
        )
        print(msg)
        _send(msg)
        return

    lines = [f"🔴 *Lab Safety Bridge* | {now.strftime('%Y-%m-%d %H:%M UTC')}\n"]
    for name, issues in all_issues:
        lines.append(f"*{name}*")
        lines.extend(issues)
        lines.append("")

    lines.append(f"_{len(all_issues)}/{len(checks)} Bereiche mit Problemen_")
    msg = "\n".join(lines)
    print(msg)
    _send(msg)


if __name__ == "__main__":
    main()
