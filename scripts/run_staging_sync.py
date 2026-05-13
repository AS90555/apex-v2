"""
Staging-Sync-Daemon — läuft alle 10 min via Cron.

Promotet Lab-Discoveries aus research_staging.db in die Haupt-DB,
sofern alle Integritätsbedingungen erfüllt sind.

Integritätsprüfung (V6_STATS_ENFORCED=False → PBO/Stability werden übersprungen):
  - cost_model_applied = 1
  - pf_test_netto >= PF_TEST_NETTO_MIN
  - n_test >= N_TEST_MIN
  - dsr IS NOT NULL (dsr_value optional bis Phase 4)
  - oos_folds_n >= 1 ODER NULL (bis Phase 4 nicht enforced)
  - backtest_funding_model = 'dynamic' ODER NULL (bis Phase 3 nicht enforced)
  - intrabar_model != 'static' ODER NULL (bis Phase 3 nicht enforced)
  - framework_version (beliebig — 'v6' erst nach Phase 4 Pflicht)

Bei Pass:  INSERT OR IGNORE in Haupt-DB (IMMEDIATE-Transaktion) + Telegram-Alert
Bei Fail:  sync_status='rejected_integrity' in Staging + Telegram-Alert
"""

from __future__ import annotations

import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection, get_staging_connection
from core.utils import log

# Feature-Flags (werden in Phase 4 auf True gesetzt)
V6_STATS_ENFORCED = os.getenv("V6_STATS_ENFORCED", "false").lower() == "true"

# Thresholds
PF_TEST_NETTO_MIN = float(os.getenv("PF_TEST_NETTO_MIN", "1.20"))
N_TEST_MIN        = int(os.getenv("N_TEST_MIN", "30"))
DSR_MIN           = float(os.getenv("DSR_MIN_DRY_RUN", "0.50"))
PBO_MAX           = float(os.getenv("PBO_MAX", "0.30"))
STABILITY_MIN     = float(os.getenv("STABILITY_MIN", "0.50"))


def _check_integrity(row) -> tuple[bool, str]:
    """Gibt (passed, reason) zurück."""
    if not row["cost_model_applied"]:
        return False, "cost_model_applied=0"

    pf = row["pf_test_netto"]
    if pf is None or pf < PF_TEST_NETTO_MIN:
        return False, f"pf_test_netto={pf} < {PF_TEST_NETTO_MIN}"

    n = row["n_test"] or 0
    if n < N_TEST_MIN:
        return False, f"n_test={n} < {N_TEST_MIN}"

    if V6_STATS_ENFORCED:
        dsr = row["dsr_value"] or row["dsr"]
        if dsr is None or dsr < DSR_MIN:
            return False, f"dsr={dsr} < {DSR_MIN}"

        pbo = row["pbo_value"]
        if pbo is not None and pbo > PBO_MAX:
            return False, f"pbo={pbo} > {PBO_MAX}"

        stab = row["stability_score"]
        if stab is not None and stab < STABILITY_MIN:
            return False, f"stability_score={stab} < {STABILITY_MIN}"

        if row["backtest_funding_model"] not in (None, "dynamic"):
            return False, f"backtest_funding_model={row['backtest_funding_model']} != dynamic"

        if row["intrabar_model"] == "static":
            return False, "intrabar_model=static (nicht erlaubt im v6-enforced Modus)"

    return True, "ok"


def _send_alert(text: str) -> None:
    try:
        from monitor.telegram_bot import send_message
        send_message(text)
    except Exception:
        log(f"[STAGING-SYNC] Alert (kein Telegram): {text}")


def sync_once() -> tuple[int, int]:
    """Gibt (synced, rejected) zurück."""
    staging = get_staging_connection()
    main_db = get_connection()

    pending = staging.execute(
        "SELECT * FROM lab_discoveries WHERE sync_status='pending'"
    ).fetchall()

    synced = 0
    rejected = 0
    now = datetime.now(timezone.utc).isoformat()

    for row in pending:
        passed, reason = _check_integrity(row)

        if not passed:
            staging.execute(
                "UPDATE lab_discoveries SET sync_status='rejected_integrity', "
                "sync_attempted_at=?, sync_reject_reason=? WHERE id=?",
                (now, reason, row["id"]),
            )
            staging.commit()
            log(f"[STAGING-SYNC] Rejected #{row['id']} {row['strategy']}/{row['asset']}: {reason}")
            _send_alert(
                f"⛔ Staging-Sync REJECTED: {row['strategy']}/{row['asset']}\n"
                f"Grund: {reason}\nDiscovery-ID: {row['id']}"
            )
            rejected += 1
            continue

        # Alle Fenster-Ergebnisse laden
        windows = staging.execute(
            "SELECT * FROM lab_window_results WHERE discovery_id=?", (row["id"],)
        ).fetchall()

        try:
            # isolation_level="IMMEDIATE" in get_connection() startet automatisch
            # IMMEDIATE-Transaktionen — kein explizites BEGIN nötig.
            main_db.execute("""
                INSERT OR IGNORE INTO lab_discoveries
                (discovered_at, params_hash, strategy, asset, params_json,
                 n_train, pf_train, avg_r_train, n_test, pf_test, avg_r_test,
                 wr_test, fitness_score, notified, market_regime, max_dd_r,
                 micro_score, deployment_status, cooldown_bars, signals_per_week,
                 cost_model_applied, pf_test_netto, dsr,
                 framework_version, dsr_value, pbo_value, max_drawdown,
                 calmar_ratio, stability_score, composite_score, oos_folds_n,
                 re_evaluated_at, backtest_slippage_assumption,
                 backtest_funding_model, intrabar_model)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row["discovered_at"], row["params_hash"], row["strategy"], row["asset"],
                row["params_json"], row["n_train"], row["pf_train"], row["avg_r_train"],
                row["n_test"], row["pf_test"], row["avg_r_test"], row["wr_test"],
                row["fitness_score"], row["notified"] or 0,
                row["market_regime"] or "UNKNOWN",   # NOT NULL in Haupt-DB
                row["max_dd_r"], row["micro_score"], row["deployment_status"] or "lab",
                row["cooldown_bars"] or 0, row["signals_per_week"],
                row["cost_model_applied"], row["pf_test_netto"], row["dsr"],
                row["framework_version"] or "v1", row["dsr_value"], row["pbo_value"],
                row["max_drawdown"], row["calmar_ratio"], row["stability_score"],
                row["composite_score"], row["oos_folds_n"], row["re_evaluated_at"],
                row["backtest_slippage_assumption"], row["backtest_funding_model"],
                row["intrabar_model"],
            ))

            # Haupt-DB-ID für die gerade eingefügte Discovery ermitteln
            main_row = main_db.execute(
                "SELECT id FROM lab_discoveries WHERE params_hash=?",
                (row["params_hash"],),
            ).fetchone()

            if main_row and windows:
                for w in windows:
                    main_db.execute("""
                        INSERT OR IGNORE INTO lab_window_results
                        (discovery_id, window_idx, period_start, period_end,
                         n_train, pf_train, avg_r_train, n_test, pf_test,
                         avg_r_test, wr_test, max_dd_r, passed)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        main_row["id"], w["window_idx"], w["period_start"], w["period_end"],
                        w["n_train"], w["pf_train"], w["avg_r_train"],
                        w["n_test"], w["pf_test"], w["avg_r_test"],
                        w["wr_test"], w["max_dd_r"], w["passed"],
                    ))

            main_db.commit()

            staging.execute(
                "UPDATE lab_discoveries SET sync_status='synced', sync_attempted_at=? WHERE id=?",
                (now, row["id"]),
            )
            staging.commit()

            log(f"[STAGING-SYNC] Synced #{row['id']} {row['strategy']}/{row['asset']} "
                f"(pf_netto={row['pf_test_netto']}, n={row['n_test']})")
            synced += 1

        except Exception as e:
            try:
                main_db.rollback()
            except Exception:
                pass
            log(f"[STAGING-SYNC] Fehler bei Sync #{row['id']}: {e}")
            _send_alert(f"⚠️ Staging-Sync ERROR: {row['strategy']}/{row['asset']}\n{e}")

    staging.close()
    main_db.close()
    return synced, rejected


def main() -> None:
    log("[STAGING-SYNC] Start")
    synced, rejected = sync_once()
    log(f"[STAGING-SYNC] Fertig — {synced} synced, {rejected} rejected")


if __name__ == "__main__":
    main()
