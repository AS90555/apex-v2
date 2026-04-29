"""
APEX Auto-Pilot — Automatisches Regime-Switching & Auto-Deploy

Wird von run_features.py nach jeder Regime-Berechnung aufgerufen.
Erkennt Regime-Wechsel und deployt automatisch das beste bekannte Setup.

State-Tracking:
  system_state: regime_prev_{asset}          → letztes bekanntes Regime
  system_state: autopilot_cooldown_{asset}   → ISO-Timestamp bis cooldown aktiv

Sicherheits-Checks:
  1. Cooldown: kein Re-Deploy derselben (asset, regime)-Kombination < COOLDOWN_H Stunden
  2. Duplikat:  Setup-ID bereits aktiv → überspringen
  3. Kein Fund: kein qualifying Setup in lab_discoveries → nur Alert, kein Deploy
"""

import math
import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection, get_state, set_state
from core.utils import log

# ── Konfiguration ─────────────────────────────────────────────────────────────
COOLDOWN_H       = 6     # Stunden zwischen zwei Auto-Deploys derselben (asset, regime)
MIN_PF_AUTODEPLOY = 1.30  # Nur Setups mit OOS-PF ≥ diesem Wert werden deployed
MIN_N_AUTODEPLOY  = 40    # Mindest-Trade-Zahl im OOS-Fenster

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_REGIME_ICON = {
    "TREND_UP":   "📈",
    "TREND_DOWN": "📉",
    "SIDEWAYS":   "↔️",
    "UNKNOWN":    "❓",
}


# ── Telegram-Push (synchron via requests — kein async nötig) ──────────────────

def _send(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log(f"[AUTOPILOT] Telegram-Fehler: {e}")


# ── Deploy-Logik (Single Source of Truth — auch vom Bot genutzt) ──────────────

def calc_target_trades(wr_pct: float | None) -> int:
    """max(30, ceil(15 / WR)) — Binomial-Stichprobengröße für ~95% Konfidenz."""
    if not wr_pct or wr_pct <= 0:
        return 50
    return max(30, int(math.ceil(15.0 / (wr_pct / 100.0))))


def deactivate_asset_deployments(asset: str, conn=None) -> int:
    """
    Deaktiviert alle aktiven Deployments für ein Asset.
    Gibt Anzahl deaktivierter Zeilen zurück.
    Schließt die Verbindung NICHT (kann als Teil einer Transaktion genutzt werden).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    cur = conn.execute(
        "UPDATE active_deployments SET active=0 WHERE asset=? AND active=1",
        (asset,),
    )
    if owns_conn:
        conn.commit()
        conn.close()
    return cur.rowcount


def deploy_discovery(discovery_id: int, mode: str = "dry_run",
                     replace_asset: bool = False) -> dict:
    """
    Legt eine lab_discoveries-ID als aktive Deployment-Instanz an.

    mode:           'dry_run' (Standard) oder 'live'
    replace_asset:  True → deaktiviert alle anderen aktiven Deployments
                    für dasselbe Asset bevor das neue angelegt wird.

    Rückgabe: {"ok": True, ...} oder {"error": "..."}
    Wird von /deploy (Bot), Auto-Pilot und CIO-Modus aufgerufen.
    """
    if mode not in ("dry_run", "live", "shadow"):
        return {"error": f"Ungültiger Modus: {mode}"}

    # Phase 1: Read-only — eigene Verbindung, sofort schließen
    rconn = get_connection()
    row   = rconn.execute(
        "SELECT id, strategy, asset, market_regime, params_json, wr_test "
        "FROM lab_discoveries WHERE id=?",
        (discovery_id,),
    ).fetchone()
    rconn.close()

    if not row:
        return {"error": f"Discovery #{discovery_id} nicht gefunden"}

    strategy_key  = f"{row['strategy']}_{discovery_id}"
    target_trades = calc_target_trades(row["wr_test"])
    now_iso       = datetime.now(timezone.utc).isoformat()

    # Phase 2: Write mit Retry (60× 50ms = max 3s)
    replaced = 0
    for attempt in range(60):
        try:
            conn = get_connection()
            try:
                replaced = 0
                if replace_asset:
                    cur = conn.execute(
                        "UPDATE active_deployments SET active=0 WHERE asset=? AND active=1",
                        (row["asset"],),
                    )
                    replaced = cur.rowcount

                existing = conn.execute(
                    "SELECT id, active FROM active_deployments WHERE discovery_id=?",
                    (discovery_id,),
                ).fetchone()

                if existing:
                    if existing["active"] and not replace_asset:
                        conn.close()
                        return {"error": f"Setup #{discovery_id} bereits als `{strategy_key}` aktiv",
                                "strategy_key": strategy_key}
                    conn.execute(
                        "UPDATE active_deployments "
                        "SET active=1, mode=?, deployed_at=?, target_trades=?, go_live_notified=0 "
                        "WHERE discovery_id=?",
                        (mode, now_iso, target_trades, discovery_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO active_deployments "
                        "(discovery_id, strategy_key, base_strategy, asset, market_regime, "
                        " params_json, mode, deployed_at, active, target_trades, go_live_notified) "
                        "VALUES (?,?,?,?,?,?,?,?,1,?,0)",
                        (discovery_id, strategy_key, row["strategy"], row["asset"],
                         row["market_regime"], row["params_json"],
                         mode, now_iso, target_trades),
                    )
                conn.commit()
                conn.close()
                break
            except Exception:
                try: conn.close()
                except Exception: pass
                raise
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 59:
                time.sleep(0.05)
                continue
            return {"error": f"DB gesperrt: {e}"}
    return {
        "ok":            True,
        "strategy_key":  strategy_key,
        "asset":         row["asset"],
        "regime":        row["market_regime"],
        "target_trades": target_trades,
        "wr_test":       row["wr_test"],
        "mode":          mode,
        "replaced":      replaced,
    }


# ── Best-Setup-Lookup ─────────────────────────────────────────────────────────

def best_setup_for(asset: str, regime: str) -> dict | None:
    """
    Findet das Setup mit dem höchsten OOS-PF für (asset, regime)
    unter Einhaltung der Mindest-Filter.
    """
    conn = get_connection()
    row  = conn.execute(
        """SELECT id, strategy, pf_test, avg_r_test, wr_test, n_test, params_json
           FROM lab_discoveries
           WHERE asset=? AND market_regime=? AND pf_test>=? AND n_test>=?
           ORDER BY pf_test DESC LIMIT 1""",
        (asset, regime, MIN_PF_AUTODEPLOY, MIN_N_AUTODEPLOY),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Cooldown-Check ────────────────────────────────────────────────────────────

def _cooldown_key(asset: str, regime: str) -> str:
    return f"autopilot_cooldown_{asset}_{regime}"


def _in_cooldown(asset: str, regime: str) -> bool:
    val = get_state(_cooldown_key(asset, regime))
    if not val:
        return False
    try:
        until = datetime.fromisoformat(val)
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def _set_cooldown(asset: str, regime: str) -> None:
    until = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_H)).isoformat()
    set_state(_cooldown_key(asset, regime), until)


# ── Hauptfunktion: Regime-Wechsel prüfen + ggf. Auto-Deploy ──────────────────

def check_regime_change(asset: str, new_regime: str) -> None:
    """
    Vergleicht new_regime mit dem gespeicherten Vorgänger-Regime.
    Bei Wechsel: Best-Setup suchen, deployen, Telegram-Push senden.
    Wird von run_features.py für jedes Asset aufgerufen.
    """
    prev_key  = f"regime_prev_{asset}"
    prev_regime = get_state(prev_key)

    # Erstes Mal → Baseline setzen, kein Trigger
    if prev_regime is None:
        set_state(prev_key, new_regime)
        log(f"[AUTOPILOT] {asset}: Baseline gesetzt → {new_regime}")
        return

    # Kein Wechsel → nichts tun
    if prev_regime == new_regime:
        return

    # ── Wechsel erkannt ──────────────────────────────────────────────────────
    icon_old = _REGIME_ICON.get(prev_regime, "❓")
    icon_new = _REGIME_ICON.get(new_regime,  "❓")
    log(f"[AUTOPILOT] 🌤 {asset}: Regime-Wechsel {prev_regime} → {new_regime}")

    # State sofort aktualisieren (auch wenn kein Deploy folgt)
    set_state(prev_key, new_regime)

    # UNKNOWN → kein Deploy
    if new_regime == "UNKNOWN":
        _send(
            f"⚠️ *Regime\\-Wechsel* `{asset}`\n"
            f"{icon_old} `{prev_regime}` → {icon_new} `{new_regime}`\n"
            f"_Kein Auto\\-Deploy bei UNKNOWN\\-Regime\\._"
        )
        return

    # Cooldown aktiv?
    if _in_cooldown(asset, new_regime):
        log(f"[AUTOPILOT] {asset}/{new_regime}: Cooldown aktiv — überspringe")
        return

    # Bestes Setup suchen
    setup = best_setup_for(asset, new_regime)

    if not setup:
        log(f"[AUTOPILOT] {asset}/{new_regime}: kein qualifizierendes Setup in Alpha-Library")
        _send(
            f"🌤 *Wetterumschwung:* `{asset}`\n"
            f"{icon_old} `{prev_regime}` → {icon_new} `{new_regime}`\n\n"
            f"⚠️ Kein Setup in der Alpha\\-Library für dieses Regime\\.\n"
            f"_Lab\\-Daemon läuft weiter — kommt bald\\._"
        )
        return

    disc_id = setup["id"]
    result  = deploy_discovery(disc_id)

    if result.get("error") and "bereits" in result["error"]:
        # Duplikat-Schutz: Setup läuft schon
        log(f"[AUTOPILOT] {asset}/{new_regime}: Setup #{disc_id} bereits aktiv")
        _send(
            f"🌤 *Wetterumschwung:* `{asset}`\n"
            f"{icon_old} `{prev_regime}` → {icon_new} `{new_regime}`\n\n"
            f"ℹ️ Bestes Setup \\(\\#{disc_id}\\) läuft bereits als `{result['strategy_key']}`\\."
        )
        _set_cooldown(asset, new_regime)
        return

    if result.get("error"):
        log(f"[AUTOPILOT] Deploy-Fehler: {result['error']}")
        return

    # ── Erfolgreicher Auto-Deploy ─────────────────────────────────────────────
    sk     = result["strategy_key"]
    target = result["target_trades"]
    wr     = setup["wr_test"] or 0
    _set_cooldown(asset, new_regime)

    log(f"[AUTOPILOT] ✅ Auto-Deploy: {sk} | PF={setup['pf_test']:.2f} target={target}")

    _send(
        f"🚨 *Wetterumschwung erkannt\\!*\n\n"
        f"`{asset}` wechselt: {icon_old} `{prev_regime}` → {icon_new} *`{new_regime}`*\n\n"
        f"🤖 *Auto\\-Deploy gestartet:*\n"
        f"  Setup \\#{disc_id} → Instanz `{sk}`\n"
        f"  OOS\\-PF: *{setup['pf_test']:.2f}*  |  WR: *{wr:.1f}%*  |  Ziel: *{target} Trades*\n\n"
        f"_Dry\\-Run läuft parallel zum Canary\\-Test\\._"
    )
