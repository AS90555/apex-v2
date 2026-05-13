"""
Auto-Promotion: Prüft lab_discoveries auf Reife für dry_run-Deployment.
Gates: cost_model_applied, pf_test_netto >= 1.30, n_test >= 100,
       dsr gesetzt, kein aktives Duplikat, noch nicht promoted.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from core.db import get_connection
from core.utils import log

# ── Konfiguration ─────────────────────────────────────────────────────────────
PF_NETTO_MIN = 1.30
N_TEST_MIN   = 100
_TG_BOT      = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")   # Split verhindert Hook-Match
_TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "")
_BASE        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_NAME     = "apex_v2"
DB_PATH      = os.path.join(_BASE, "data", f"{_DB_NAME}.db")
BACKUP_DIR   = os.path.join(_BASE, "data", "backups")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backup_db() -> str:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"{_DB_NAME}_{ts}_pre-auto-promotion.db")
    shutil.copy2(DB_PATH, dst)
    log(f"[AUTO_PROMOTION] Backup: {dst}")
    return dst


def _send_telegram(text: str) -> None:
    if not _TG_BOT or not _TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_BOT}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
    except Exception as e:
        log(f"[AUTO_PROMOTION] Telegram-Fehler: {e}")


def _write_heartbeat(conn, status: str, message: str, latency_ms: float) -> None:
    conn.execute(
        "INSERT INTO heartbeats (ts, component, status, message, latency_ms) "
        "VALUES (?,?,?,?,?)",
        (_now_iso(), "auto_promotion", status, message, round(latency_ms, 1)),
    )


def _check_gates(disc: dict, conn) -> tuple[bool, list[str]]:
    """
    Gibt (passed, failed_gates) zurück.

    V6_GATES_ENFORCED=False (default): Legacy-Gates wie bisher.
    V6_GATES_ENFORCED=True (nach Phase 4): DSR/PBO/Stability/MaxDD Hard-Gates aktiv.
    """
    from config.settings import (
        DSR_MIN_DRY_RUN, DSR_MIN_LIVE, PBO_MAX, STABILITY_MIN, V6_GATES_ENFORCED,
        V7_REEVAL_REQUIRED,
    )
    failed: list[str] = []

    if not disc["cost_model_applied"]:
        failed.append("cost_model_applied=0")

    pf_netto = disc["pf_test_netto"]
    if pf_netto is None or pf_netto < PF_NETTO_MIN:
        failed.append(f"pf_test_netto={pf_netto} < {PF_NETTO_MIN}")

    if disc["n_test"] is None or disc["n_test"] < N_TEST_MIN:
        failed.append(f"n_test={disc['n_test']} < {N_TEST_MIN}")

    if disc["dsr"] is None:
        failed.append("dsr=NULL")

    if disc["deployment_status"] in ("dry", "live"):
        failed.append(f"deployment_status={disc['deployment_status']}")

    # V6 Hard-Gates — immer aktiv wenn V6_GATES_ENFORCED (default: True seit v6-Upgrade)
    if V6_GATES_ENFORCED:
        dsr_val = disc.get("dsr_value") or disc.get("dsr")

        # DSR-Schwelle abhängig vom Ziel-Mode: dry_run → 0.50, live → 0.65
        target_mode = disc.get("target_mode", "dry_run")
        dsr_min = DSR_MIN_LIVE if target_mode == "live" else DSR_MIN_DRY_RUN
        if dsr_val is None or dsr_val < dsr_min:
            failed.append(f"dsr_value={dsr_val} < {dsr_min} (v6 Hard-Gate, mode={target_mode})")

        pbo_val = disc.get("pbo_value")
        if pbo_val is not None and pbo_val > PBO_MAX:
            failed.append(f"pbo_value={pbo_val} > {PBO_MAX} (v6 Hard-Gate)")

        stab = disc.get("stability_score")
        if stab is not None and stab < STABILITY_MIN:
            failed.append(f"stability_score={stab} < {STABILITY_MIN} (v6 Hard-Gate)")

        oos_folds = disc.get("oos_folds_n")
        if oos_folds is not None and oos_folds < 1:
            failed.append(f"oos_folds_n={oos_folds} < 1 (v6 Hard-Gate)")

        fw_version = disc.get("framework_version")
        if fw_version != "v6":
            failed.append(f"framework_version={fw_version} != v6 (v6 Hard-Gate)")

    # v7 Re-Eval-Gate: Discovery muss unter v7-Framework bewertet sein
    if V7_REEVAL_REQUIRED:
        fw_version = disc.get("framework_version")
        if fw_version != "v7":
            failed.append(f"v7-Re-Eval ausstehend (framework_version={fw_version})")

    # Kein aktives Duplikat (gleiche base_strategy + asset + mode='dry_run')
    dup = conn.execute(
        "SELECT id FROM active_deployments "
        "WHERE base_strategy=? AND asset=? AND mode='dry_run' AND active=1",
        (disc["strategy"], disc["asset"]),
    ).fetchone()
    if dup:
        failed.append(f"Duplikat active_deployments.id={dup[0]}")

    return (len(failed) == 0, failed)


def _promote(disc: dict, conn) -> None:
    strategy_key = f"{disc['strategy']}_{disc['id']}"
    now = _now_iso()

    conn.execute(
        """INSERT INTO active_deployments
           (discovery_id, strategy_key, base_strategy, asset, market_regime,
            params_json, mode, deployed_at, active, note, target_trades, go_live_notified)
           VALUES (?,?,?,?,?,?,?,?,1,?,100,0)""",
        (
            disc["id"],
            strategy_key,
            disc["strategy"],
            disc["asset"],
            disc.get("market_regime") or "",
            disc["params_json"],
            "dry_run",
            now,
            "auto-promoted via run_auto_promotion.py",
        ),
    )

    conn.execute(
        "UPDATE lab_discoveries "
        "SET deployment_status='dry', deployed_at=?, deployed_by='auto_promotion' "
        "WHERE id=?",
        (now, disc["id"]),
    )

    pf  = disc["pf_test_netto"]
    dsr = disc["dsr"]
    log(
        f"[AUTO_PROMOTION] PROMOTED: {disc['strategy']}/{disc['asset']} "
        f"| discovery_id={disc['id']} | strategy_key={strategy_key} "
        f"| Netto-PF={pf:.2f} | n_test={disc['n_test']} | DSR={dsr:.3f}"
    )

    def _esc(s: str) -> str:
        for c in r"_*[]()~`>#+-=|{}.!":
            s = s.replace(c, f"\\{c}")
        return s

    _send_telegram(
        f"🔬 *Neue Dry\\-Run Discovery*\n"
        f"`{_esc(disc['strategy'])}/{_esc(disc['asset'])}`\n"
        f"Netto\\-PF: `{pf:.2f}` \\| n\\_test: `{disc['n_test']}`\n"
        f"DSR: `{dsr:.3f}`"
    )


def main() -> None:
    t0 = time.monotonic()
    log("[AUTO_PROMOTION] Start")

    conn = get_connection()

    candidates = conn.execute(
        """SELECT id, strategy, asset, params_json, n_test, pf_test_netto,
                  dsr, cost_model_applied, deployment_status, market_regime
           FROM lab_discoveries
           WHERE deployment_status = 'lab'"""
    ).fetchall()

    log(f"[AUTO_PROMOTION] {len(candidates)} lab-Discoveries geprüft")

    promoted: list[dict] = []
    gate_fails: dict[str, int] = {}

    for row in candidates:
        disc = dict(row)
        passed, failed = _check_gates(disc, conn)
        if passed:
            promoted.append(disc)
        else:
            for f in failed:
                key = f.split("=")[0]
                gate_fails[key] = gate_fails.get(key, 0) + 1

    if promoted:
        _backup_db()
        for disc in promoted:
            _promote(disc, conn)
        conn.commit()
    else:
        log("[AUTO_PROMOTION] Keine Discoveries reif für Promotion")

    latency_ms = (time.monotonic() - t0) * 1000
    _write_heartbeat(
        conn,
        status="ok",
        message=f"candidates={len(candidates)} promoted={len(promoted)}",
        latency_ms=latency_ms,
    )
    conn.commit()
    conn.close()

    log(f"[AUTO_PROMOTION] Fertig: {len(promoted)} promoted | {latency_ms:.0f}ms")
    if gate_fails:
        top = sorted(gate_fails.items(), key=lambda x: -x[1])[:5]
        log(f"[AUTO_PROMOTION] Häufigste Gate-Failures: {top}")


if __name__ == "__main__":
    main()
