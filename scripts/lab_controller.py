"""
Lab-Controller für APEX V2 Research-Lab.

Orchestriert Weekly-Cycle: Regime-Update → Queue-Build → Pre-Scan → Research.
Einziger Schreiber für Orchestrierungs-Tabellen (lab_cycles, lab_queue, lab_locks).

Modi:
    asset-profile-update   Regime für alle Assets neu berechnen
    build-queue            Prioritäts-Queue für aktuellen Cycle aufbauen
    run-cycle              Neuen Cycle starten + abarbeiten
    resume-cycle           Unterbrochenen Cycle fortsetzen
    health-check           Monitoring-Metriken prüfen
    heartbeat              Heartbeat in apex_v2.db schreiben
    status                 Aktuellen Cycle-Status ausgeben
    generate-report        Weekly-Report triggern (Phase 5)
    set-config             lab_config-Wert ändern
    release-lock           Lock manuell freigeben (nur mit --confirm)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent.parent / "config" / ".env")
except ImportError:
    pass

from core.lab_negative_controls import (
    classify_and_archive,
    check_negative_control,
)
from core.lab_borderline_registry import classify_and_register, TrialResult
from core.lab_state_db import (
    acquire_lock,
    create_lab_cycle,
    get_asset_profile,
    get_current_regime,
    get_borderline_candidates_pending_review,
    get_config_value,
    get_lab_state_connection,
    get_pending_queue,
    log_governance_event,
    release_lock,
    set_config_value,
    update_cycle_status,
    update_queue_status,
    write_queue_entry,
)
from core.utils import log
from scripts.lab_asset_profiler import (
    InsufficientDataError,
    compute_and_store_profile,
    get_asset_priority_factors,
    get_compatible_strategies,
    _load_matrix,
)
from scripts.lab_pre_scan import PreScanResult, _load_staging_stats, run_pre_scan
from scripts.lab_report_generator import generate_weekly_report, _send_telegram as _report_send_telegram
from research.v72_objective import compute_study_hash

LAB_STATE_DB = Path(__file__).parent.parent / "data" / "lab_state.db"
APEX_DB = Path(__file__).parent.parent / "data" / "apex_v2.db"
STAGING_DB = Path(__file__).parent.parent / "data" / "research_staging.db"

# Circuit-Breaker-Schwellen
CB_ZERO_RATE_THRESHOLD = 0.80
CB_CONSECUTIVE_ERROR_THRESHOLD = 3
_DEFAULT_REVIEW_TIMEOUT_DAYS = 7

_TG_TOKEN_KEY = "TELEGRAM_BOT" + "_TOKEN"
_TG_CHAT_KEY = "TELEGRAM_CHAT_ID"


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Asset-Profile-Update ────────────────────────────────────────────────────

def mode_asset_profile_update(db_path: str) -> None:
    conn = get_lab_state_connection(db_path)
    lock_ok = acquire_lock(conn, "asset_profiler", "lab_controller", ttl_hours=2)
    conn.close()
    if not lock_ok:
        log("[lab-ctrl] Lock 'asset_profiler' aktiv — Profiler läuft bereits, übersprungen")
        return

    matrix = _load_matrix()
    assets = list(matrix.get("asset_universe", {}).keys())
    errors = []

    for asset in assets:
        try:
            compute_and_store_profile(asset, db_path=db_path)
        except InsufficientDataError as e:
            log(f"[lab-ctrl] WARNUNG Profiler {asset}: {e}")
        except Exception as e:
            log(f"[lab-ctrl] FEHLER Profiler {asset}: {e}")
            errors.append(asset)

    conn = get_lab_state_connection(db_path)
    release_lock(conn, "asset_profiler")
    conn.close()

    if errors:
        _send_telegram(f"⚠️ Lab-Profiler-Fehler für Assets: {errors}")


# ─── Queue-Build ─────────────────────────────────────────────────────────────

def mode_build_queue(db_path: str, cycle_id: int | None = None) -> int:
    """Baut Prioritäts-Queue für aktuellen Cycle. Gibt Anzahl eingestellter Paare zurück.

    Strategie:
      1. Evolution-Engine propose_variants() aufrufen (70/30 Exploration/Exploitation)
      2. Proposed Variants in Queue eintragen (mit variant_id-Link)
      3. Falls Evolution-Engine keine Variants liefert: Fallback auf klassische Matrix
    """
    conn = get_lab_state_connection(db_path)
    try:
        if cycle_id is None:
            row = conn.execute(
                "SELECT id FROM lab_cycles WHERE status='running' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cycle_id = row["id"] if row else create_lab_cycle(conn)

        budget = int(get_config_value(conn, "weekly_trial_budget") or "200")
        trials_per_pair = 50
        max_pairs = budget // trials_per_pair

        queued = 0

        # Versuch 1: Evolution-Engine
        evolution_variant_ids: list[str] = []
        try:
            from core.lab_evolution_engine import propose_variants
            evolution_variant_ids = propose_variants(conn, cycle_id=cycle_id, budget_trials=budget)
        except Exception as e:
            log(f"[lab-ctrl] Evolution-Engine nicht verfügbar (Fallback): {e}")

        if evolution_variant_ids:
            from core.lab_state_db import get_variant, update_variant_status
            for vid in evolution_variant_ids[:max_pairs]:
                variant = get_variant(conn, vid)
                if variant is None:
                    continue
                nc_result = check_negative_control(variant.strategy, variant.asset, db_path=db_path)
                if nc_result.blocked and not nc_result.reopen_available:
                    update_variant_status(conn, vid, "blocked", "negative_control")
                    continue
                entry_id = write_queue_entry(
                    conn, cycle_id, variant.strategy, variant.asset,
                    priority_score=1.0,  # Evolution-Engine bestimmt Priorität intern
                    budget_trials=trials_per_pair,
                    variant_id=vid,
                )
                update_variant_status(conn, vid, "queued", f"queue_entry={entry_id}")
                log(f"[lab-ctrl] Queue #{entry_id}: {variant.strategy}/{variant.asset} "
                    f"[variant={vid[:8]} proposed_by={variant.proposed_by}]")
                queued += 1
            conn.commit()
            log(f"[lab-ctrl] Queue gebaut (Evolution): {queued} Variants für Cycle #{cycle_id}")
            return queued

        # Fallback: klassische Matrix-basierte Queue
        log("[lab-ctrl] Fallback: Queue-Build aus Strategy-Matrix")
        priority_factors = get_asset_priority_factors()
        matrix = _load_matrix()
        assets = matrix.get("asset_universe", {})

        scored_pairs = []
        for asset in assets:
            strategies = get_compatible_strategies(asset, db_path=db_path)
            if not strategies:
                continue
            profile = get_asset_profile(conn, asset)
            profile_penalty = 1.0 if profile else 0.5
            for strategy in strategies:
                nc_result = check_negative_control(strategy, asset, db_path=db_path)
                if nc_result.blocked and not nc_result.reopen_available:
                    continue
                priority = (
                    priority_factors.get(asset, 0.75)
                    * profile_penalty
                    * (0.3 if nc_result.blocked else 1.0)
                )
                scored_pairs.append((priority, strategy, asset))

        scored_pairs.sort(key=lambda x: x[0], reverse=True)
        for priority, strategy, asset in scored_pairs[:max_pairs]:
            entry_id = write_queue_entry(conn, cycle_id, strategy, asset, priority, trials_per_pair)
            log(f"[lab-ctrl] Queue #{entry_id}: {strategy}/{asset} prio={priority:.3f}")
            queued += 1

        conn.commit()
        log(f"[lab-ctrl] Queue gebaut (Matrix-Fallback): {queued} Paare für Cycle #{cycle_id}")
        return queued
    finally:
        conn.close()


# ─── Cycle ausführen ─────────────────────────────────────────────────────────

def mode_run_cycle(db_path: str) -> None:
    conn = get_lab_state_connection(db_path)
    lock_ok = acquire_lock(conn, "weekly_cycle", "lab_controller", ttl_hours=8)
    if not lock_ok:
        log("[lab-ctrl] Lock 'weekly_cycle' aktiv — Cycle läuft bereits")
        conn.close()
        return

    cycle_id = create_lab_cycle(conn)
    conn.commit()

    # Evolution-Layer: Familien-Ontologie synchronisieren
    try:
        from core.lab_families import sync_to_db as _sync_families
        _sync_families(conn)
        log(f"[lab-ctrl] Familien-Ontologie synchronisiert")
    except Exception as e:
        log(f"[lab-ctrl] WARNUNG: Familien-Sync fehlgeschlagen: {e}")

    conn.close()

    log(f"[lab-ctrl] Neuer Cycle #{cycle_id} gestartet")
    _send_telegram(f"🔬 <b>Lab-Cycle #{cycle_id} gestartet</b>\n{_now_iso()}")

    queued = mode_build_queue(db_path, cycle_id)
    if queued == 0:
        _finish_cycle(db_path, cycle_id, "completed", "Keine Paare in Queue")
        _send_telegram(f"⚠️ Lab-Cycle #{cycle_id}: Universum erschöpft — keine Paare zu testen")
        return

    _process_queue(db_path, cycle_id)


def mode_resume_cycle(db_path: str, cycle_id: int) -> None:
    conn = get_lab_state_connection(db_path)
    row = conn.execute(
        "SELECT status FROM lab_cycles WHERE id=?", (cycle_id,)
    ).fetchone()
    conn.close()

    if not row:
        log(f"[lab-ctrl] FEHLER: Cycle #{cycle_id} nicht gefunden")
        sys.exit(1)

    if row["status"] not in ("running", "paused", "circuit_broken"):
        log(f"[lab-ctrl] Cycle #{cycle_id} hat Status '{row['status']}' — kann nicht fortgesetzt werden")
        sys.exit(1)

    conn = get_lab_state_connection(db_path)
    update_cycle_status(conn, cycle_id, "running")
    conn.commit()

    # Orphaned Variants bereinigen: Variants in queued/pre_scanning ohne offenes Queue-Entry
    from core.lab_state_db import update_variant_status
    orphaned = conn.execute(
        """
        SELECT sv.variant_id, sv.status FROM strategy_variants sv
        WHERE sv.status IN ('queued', 'pre_scanning')
        AND NOT EXISTS (
            SELECT 1 FROM lab_queue lq
            WHERE lq.variant_id = sv.variant_id
            AND lq.cycle_id = ?
            AND lq.status NOT IN ('done', 'blocked', 'skipped', 'paused_inconclusive')
        )
        """,
        (cycle_id,),
    ).fetchall()
    for row in orphaned:
        try:
            update_variant_status(conn, row["variant_id"], "archived", "cycle_aborted")
            log(f"[lab-ctrl] Orphaned Variant {row['variant_id'][:8]} ({row['status']}) → archived")
        except Exception as e:
            log(f"[lab-ctrl] WARNUNG: Konnte Orphan {row['variant_id'][:8]} nicht archivieren: {e}")
    conn.commit()
    conn.close()

    log(f"[lab-ctrl] Cycle #{cycle_id} wird fortgesetzt")
    _process_queue(db_path, cycle_id)


def _process_queue(db_path: str, cycle_id: int) -> None:
    consecutive_errors = 0
    total_done = 0
    zero_score_count = 0

    while True:
        conn = get_lab_state_connection(db_path)
        pending = get_pending_queue(conn, cycle_id)
        conn.close()

        if not pending:
            log(f"[lab-ctrl] Cycle #{cycle_id}: Queue abgearbeitet")
            break

        entry = pending[0]
        conn = get_lab_state_connection(db_path)
        update_queue_status(conn, entry.id, "pre_scanning")
        conn.commit()
        conn.close()

        pre_result = run_pre_scan(entry.strategy, entry.asset, n_trials=10, db_path=db_path)

        if pre_result.status == "governance_block":
            _set_queue_blocked(db_path, entry.id, "governance_block")
            continue

        if pre_result.status == "error":
            consecutive_errors += 1
            _set_queue_blocked(db_path, entry.id, f"pre_scan_error: {pre_result.error_message}")
            if consecutive_errors >= CB_CONSECUTIVE_ERROR_THRESHOLD:
                _trigger_circuit_breaker(db_path, cycle_id, "3 konsekutive Fehler im Pre-Scan")
                return
            continue

        if pre_result.status in ("signal_absent", "frequency_incompatible"):
            conn = get_lab_state_connection(db_path)
            regime = get_current_regime(conn, entry.asset)
            conn.close()
            classify_and_archive(
                entry.strategy, entry.asset, pre_result.study_hash,
                pre_result.run_stats, regime_at_archive=regime, db_path=db_path,
            )
            _set_queue_done(db_path, entry.id, f"pre_scan_{pre_result.status}")
            if entry.variant_id:
                from core.lab_state_db import update_variant_status
                _conn = get_lab_state_connection(db_path)
                try:
                    update_variant_status(
                        _conn, entry.variant_id, "archived",
                        f"pre_scan_{pre_result.status}",
                    )
                finally:
                    _conn.close()
            consecutive_errors = 0
            continue

        if pre_result.status == "inconclusive":
            _handle_inconclusive(db_path, entry, pre_result)
            continue

        # signal_present → voller Run
        _set_queue_status_running(db_path, entry.id)
        run_result = _run_full_research(db_path, entry)

        if run_result is None:
            consecutive_errors += 1
            _set_queue_blocked(db_path, entry.id, "run_error")
            if consecutive_errors >= CB_CONSECUTIVE_ERROR_THRESHOLD:
                _trigger_circuit_breaker(db_path, cycle_id, "3 konsekutive Fehler im Run")
                return
            continue

        consecutive_errors = 0
        total_done += 1

        if run_result.get("composite_max", 1.0) < 0.01:
            zero_score_count += 1
        zero_rate = zero_score_count / total_done if total_done > 0 else 0
        if total_done >= 3 and zero_rate > CB_ZERO_RATE_THRESHOLD:
            _trigger_circuit_breaker(
                db_path, cycle_id,
                f"Zero-Score-Rate {zero_rate:.0%} > {CB_ZERO_RATE_THRESHOLD:.0%}",
            )
            return

        _post_run_governance(db_path, entry, run_result)
        _set_queue_done(db_path, entry.id, "completed")

    _finish_cycle(db_path, cycle_id, "completed")
    _send_cycle_report(db_path, cycle_id)


def _send_cycle_report(db_path: str, cycle_id: int) -> None:
    """E.4 — Einmaliger aggregierter Cycle-Report am Ende jedes Cycles."""
    try:
        conn = get_lab_state_connection(db_path)
        # Queue-Statistiken
        q = conn.execute(
            """SELECT
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status='paused_inconclusive' THEN 1 ELSE 0 END) AS inconclusive,
                SUM(CASE WHEN status IN ('blocked','skipped') THEN 1 ELSE 0 END) AS blocked,
                COUNT(*) AS total
               FROM lab_queue WHERE cycle_id=?""",
            (cycle_id,),
        ).fetchone()
        done        = q["done"] or 0
        inconclusive = q["inconclusive"] or 0
        blocked     = q["blocked"] or 0
        total       = q["total"] or 0

        # Negative Controls in diesem Cycle (abgeleitet via lab_queue)
        nc_count = conn.execute(
            """SELECT COUNT(*) FROM negative_controls nc
               WHERE EXISTS (
                   SELECT 1 FROM lab_queue lq
                   WHERE lq.cycle_id=? AND lq.strategy=nc.strategy AND lq.asset=nc.asset
               )""",
            (cycle_id,),
        ).fetchone()[0]

        # Bestes Variant des Cycles
        top = conn.execute(
            """SELECT sv.strategy_key, sv.asset, fr.composite
               FROM fitness_records fr
               JOIN strategy_variants sv ON sv.variant_id = fr.variant_id
               WHERE fr.cycle_id=?
               ORDER BY fr.composite DESC LIMIT 1""",
            (cycle_id,),
        ).fetchone()
        conn.close()

        top_str = (
            f"{top['strategy_key']}/{top['asset']} ({top['composite']:.3f})"
            if top else "–"
        )
        _send_telegram(
            f"✅ <b>Lab-Cycle #{cycle_id} abgeschlossen</b>\n"
            f"  Queue: {done}/{total} erledigt | {inconclusive} inconclusive | {blocked} blockiert\n"
            f"  Negative Controls: {nc_count}\n"
            f"  Top-Variant: {top_str}"
        )
    except Exception as exc:
        log(f"[lab-ctrl] Cycle-Report fehlgeschlagen: {exc}")
        _send_telegram(f"✅ <b>Lab-Cycle #{cycle_id} abgeschlossen</b>")


def _run_full_research(db_path: str, entry) -> dict | None:
    import subprocess
    # study_hash deterministisch berechnen — vor dem Subprocess, kein stdout-Parsing
    study_hash = compute_study_hash(entry.strategy, entry.asset)
    run_research = Path(__file__).parent / "run_v72_research.py"
    cmd = [
        sys.executable, str(run_research),
        "--strategy", entry.strategy,
        "--asset", entry.asset,
        "--n-trials", str(entry.budget_trials),
        "--lab-db-path", db_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        log(f"[lab-ctrl] FEHLER: Run-Timeout für {entry.strategy}/{entry.asset}")
        return None

    if result.returncode not in (0, 1):
        log(f"[lab-ctrl] FEHLER: Run exit={result.returncode}")
        return None

    stats = _load_staging_stats(entry.strategy, entry.asset, study_hash, entry.budget_trials)
    return {
        "study_hash": study_hash,
        "n_trials": stats.n_trials,
        "dsr_rate": stats.dsr_rate,
        "n_oos_median": stats.n_oos_median,
        "four_gate_pass_count": stats.four_gate_pass_count,
        "composite_max": stats.composite_max,
        "run_stats": stats,
    }


def _post_run_governance(db_path: str, entry, run_result: dict) -> None:
    conn = get_lab_state_connection(db_path)
    regime = get_current_regime(conn, entry.asset)
    conn.close()

    nc = classify_and_archive(
        entry.strategy, entry.asset, run_result["study_hash"],
        run_result["run_stats"], regime_at_archive=regime, db_path=db_path,
    )

    # Evolution: Variant-Status + Fitness-Record
    variant_id = getattr(entry, "variant_id", None)
    if variant_id:
        _update_variant_after_run(db_path, variant_id, entry, run_result, no_go=nc is not None)

    if nc:
        return

    if run_result["four_gate_pass_count"] > 0:
        trial_results = _load_trial_results_from_staging(
            entry.strategy, entry.asset, run_result["study_hash"]
        )
        candidates = classify_and_register(
            entry.strategy, entry.asset, run_result["study_hash"],
            trial_results, db_path=db_path,
        )
        if candidates:
            lineage_info = ""
            if variant_id:
                lineage_info = f"\n  Variant: {variant_id[:8]}"
            log(f"[lab-ctrl] {len(candidates)} Borderline-Kandidat(en) registriert")
            _send_telegram(
                f"🔶 <b>Borderline-Kandidat</b>: {entry.strategy}/{entry.asset}\n"
                f"  Kandidaten: {len(candidates)}\n  User-Review erforderlich (Timeout: 7 Tage)"
                f"{lineage_info}"
            )


def _update_variant_after_run(
    db_path: str,
    variant_id: str,
    entry,
    run_result: dict,
    no_go: bool,
) -> None:
    """Setzt Variant-Status auf 'evaluated' und schreibt Fitness-Record."""
    try:
        from core.lab_state_db import update_variant_status, log_evolution_event
        from core.lab_fitness_metric import compute_fitness, TrialSummary

        conn = get_lab_state_connection(db_path)

        trial_results_raw = _load_trial_results_from_staging(
            entry.strategy, entry.asset, run_result["study_hash"]
        )
        trial_summaries = [
            TrialSummary(
                composite=t.composite,
                dsr_oos=t.dsr,
                pbo_val=t.pbo,
                stability=t.stability,
                max_dd=t.max_dd,
                n_oos=t.n_oos,
                four_gate_pass=(
                    t.dsr >= 0.50 and t.pbo <= 0.30
                    and t.stability >= 0.50 and t.max_dd <= 5.0
                ),
            )
            for t in trial_results_raw
        ]

        fitness = 0.0
        if trial_summaries and not no_go:
            cycle_id = entry.cycle_id
            fitness = compute_fitness(variant_id, entry.asset, trial_summaries, conn, cycle_id)

        update_variant_status(conn, variant_id, "evaluated",
                              reason="no_go" if no_go else "completed")
        log_evolution_event(
            conn,
            event_type="variant_evaluated",
            variant_id=variant_id,
            asset=entry.asset,
            actor="lab_controller",
            payload={
                "strategy": entry.strategy,
                "four_gate_pass": run_result["four_gate_pass_count"],
                "composite_max": run_result["composite_max"],
                "no_go": no_go,
                "fitness": fitness,
            },
        )
        conn.commit()
        conn.close()
        log(f"[lab-ctrl] Variant {variant_id[:8]} evaluiert: fitness={fitness:.3f} no_go={no_go}")

        # E8: Promotion-Gate — bei GO-Fitness Re-Eval triggern
        if not no_go and fitness > 0.0:
            try:
                from core.lab_promotion_gate import promote_if_eligible
                _conn = get_lab_state_connection(db_path)
                attempt = promote_if_eligible(
                    _conn, variant_id, entry.strategy, entry.asset, fitness
                )
                _conn.close()
                if attempt.triggered and attempt.result_passed:
                    _send_telegram(
                        f"🟢 <b>Promotion-Kandidat</b>: {entry.strategy}/{entry.asset}\n"
                        f"  Variant: {variant_id[:8]}  Fitness: {fitness:.3f}\n"
                        f"  DSR/PBO/Stability: Gates bestanden — in Promotion-Queue"
                    )
            except Exception as promo_err:
                log(f"[lab-ctrl] WARNUNG: Promotion-Gate fehlgeschlagen für {variant_id}: {promo_err}")

    except Exception as e:
        log(f"[lab-ctrl] WARNUNG: Variant-Update fehlgeschlagen für {variant_id}: {e}")


def _load_trial_results_from_staging(strategy: str, asset: str, study_hash: str) -> list[TrialResult]:
    if not STAGING_DB.exists():
        return []
    conn = sqlite3.connect(str(STAGING_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT rowid as trial_id, composite_score, dsr_value, pbo_value,
                      stability_score, max_drawdown, n_oos
               FROM lab_discoveries
               WHERE strategy=? AND asset=? AND study_hash=?
               ORDER BY composite_score DESC""",
            (strategy, asset, study_hash),
        ).fetchall()
    finally:
        conn.close()

    return [
        TrialResult(
            trial_id=r["trial_id"],
            composite=r["composite_score"] or 0.0,
            dsr=r["dsr_value"] or 0.0,
            pbo=r["pbo_value"] or 1.0,
            stability=r["stability_score"] or 0.0,
            max_dd=r["max_drawdown"] or 99.0,
            n_oos=r["n_oos"] or 0,
        )
        for r in rows
        if r["composite_score"] is not None
    ]


def _handle_inconclusive(db_path: str, entry, pre_result: PreScanResult) -> None:
    """Inconclusive: Queue-Entry auf PAUSED_INCONCLUSIVE + Telegram-Eskalation. Kein Auto-Archive."""
    conn = get_lab_state_connection(db_path)
    conn.execute(
        "UPDATE lab_queue SET status='paused_inconclusive', skip_reason=? WHERE id=?",
        ("inconclusive_pre_scan", entry.id),
    )
    log_governance_event(
        conn, "inconclusive_pre_scan", "lab_queue", entry.id,
        actor="lab_auto",
        reason=f"Pre-Scan inconclusive: n_oos_median={pre_result.n_oos_median:.1f} dsr_rate={pre_result.dsr_rate:.2f}",
    )
    conn.commit()
    conn.close()

    _send_telegram(
        f"❓ <b>Pre-Scan INCONCLUSIVE</b>\n"
        f"  Strategie: {entry.strategy} | Asset: {entry.asset}\n"
        f"  n_trials={pre_result.n_trials} | DSR-Rate={pre_result.dsr_rate:.2f}\n"
        f"  n_oos_median={pre_result.n_oos_median:.1f}\n\n"
        f"  Grund: n_oos im Grenzbereich (30–59) mit wenig Signal\n\n"
        f"  ⚠️ Bitte entscheiden:\n"
        f"  /lab_decide {entry.id} full_run\n"
        f"  /lab_decide {entry.id} skip\n"
        f"  /lab_decide {entry.id} archive"
    )


def _trigger_circuit_breaker(db_path: str, cycle_id: int, reason: str) -> None:
    conn = get_lab_state_connection(db_path)
    update_cycle_status(conn, cycle_id, "circuit_broken", reason)
    log_governance_event(conn, "cycle_paused", "lab_cycle", cycle_id,
                         actor="lab_auto", reason=f"Circuit-Breaker: {reason}")
    release_lock(conn, "weekly_cycle")
    conn.commit()
    conn.close()
    log(f"[lab-ctrl] CIRCUIT-BREAKER: {reason}")
    _send_telegram(f"🚨 <b>Circuit-Breaker</b>\nCycle #{cycle_id} pausiert\nGrund: {reason}")


LAB_STOP_FLAG = Path(__file__).parent.parent / "data" / "lab_stop.flag"


def mode_run_continuous(db_path: str, pause_s: int = 300) -> None:
    """Kontinuierlicher Lab-Betrieb: Cycle → Pause → Cycle → … bis lab_stop.flag existiert.

    Ablauf pro Iteration:
      1. Stop-Flag prüfen → sauberer Ausstieg wenn data/lab_stop.flag existiert
      2. Asset-Profiler einmal täglich (nicht bei jedem Cycle)
      3. run-cycle (baut Queue intern + verarbeitet sie)
      4. pause_s Sekunden warten, dann weiter

    Stop: `touch data/lab_stop.flag`  →  nach aktuellem Cycle-Abschluss wird gestoppt.
    """
    import time as _time

    log("[lab-ctrl] Kontinuierlicher Lab-Betrieb gestartet")
    _send_telegram("🔬 <b>Lab Continuous-Mode gestartet</b>\nStoppt bei <code>data/lab_stop.flag</code>")

    last_profiler_date: str | None = None
    iteration = 0

    while True:
        # Stop-Flag prüfen
        if LAB_STOP_FLAG.exists():
            log("[lab-ctrl] Stop-Flag erkannt — kontinuierlicher Betrieb beendet")
            _send_telegram("🛑 <b>Lab Continuous-Mode gestoppt</b> (lab_stop.flag)")
            LAB_STOP_FLAG.unlink(missing_ok=True)
            break

        iteration += 1
        today = _time.strftime("%Y-%m-%d", _time.gmtime())

        # Asset-Profiler einmal täglich
        if last_profiler_date != today:
            log(f"[lab-ctrl] Continuous #{iteration}: Asset-Profiler läuft (täglich)")
            try:
                mode_asset_profile_update(db_path)
                last_profiler_date = today
            except Exception as e:
                log(f"[lab-ctrl] WARNUNG Asset-Profiler: {e}")

        # Cycle starten
        log(f"[lab-ctrl] Continuous #{iteration}: Cycle wird gestartet")
        try:
            mode_run_cycle(db_path)
        except Exception as e:
            log(f"[lab-ctrl] FEHLER in Cycle #{iteration}: {e}")
            _send_telegram(f"⚠️ <b>Lab Continuous: Fehler in Cycle #{iteration}</b>\n{e}")

        # Stop-Flag nochmals prüfen (nach Cycle-Ende, vor Pause)
        if LAB_STOP_FLAG.exists():
            log("[lab-ctrl] Stop-Flag erkannt nach Cycle — beendet")
            _send_telegram("🛑 <b>Lab Continuous-Mode gestoppt</b> (lab_stop.flag nach Cycle)")
            LAB_STOP_FLAG.unlink(missing_ok=True)
            break

        log(f"[lab-ctrl] Continuous #{iteration}: Pause {pause_s}s vor nächstem Cycle")
        _time.sleep(pause_s)


def _finish_cycle(db_path: str, cycle_id: int, status: str, reason: str = "") -> None:
    conn = get_lab_state_connection(db_path)
    update_cycle_status(conn, cycle_id, status, reason or None)
    release_lock(conn, "weekly_cycle")
    conn.commit()
    conn.close()


def _set_queue_blocked(db_path: str, entry_id: int, reason: str) -> None:
    conn = get_lab_state_connection(db_path)
    update_queue_status(conn, entry_id, "blocked", reason)
    conn.commit()
    conn.close()


def _set_queue_done(db_path: str, entry_id: int, reason: str = "") -> None:
    conn = get_lab_state_connection(db_path)
    update_queue_status(conn, entry_id, "done", reason or None)
    conn.commit()
    conn.close()


def _set_queue_status_running(db_path: str, entry_id: int) -> None:
    conn = get_lab_state_connection(db_path)
    update_queue_status(conn, entry_id, "running")
    conn.commit()
    conn.close()


# ─── Health Check ────────────────────────────────────────────────────────────

def mode_health_check(db_path: str) -> None:
    conn = get_lab_state_connection(db_path)
    alerts = []

    timeout_days = int(get_config_value(conn, "borderline_review_timeout_days") or str(_DEFAULT_REVIEW_TIMEOUT_DAYS))
    pending = get_borderline_candidates_pending_review(conn)
    now = datetime.now(timezone.utc)
    for bc in pending:
        created = datetime.fromisoformat(bc.created_at)
        age_days = (now - created).days
        if age_days >= timeout_days:
            alerts.append(
                f"⏰ Borderline-Review überfällig ({age_days}d): "
                f"{bc.strategy}/{bc.asset} composite={bc.composite:.3f}"
            )

    new_ncs = conn.execute(
        "SELECT COUNT(*) FROM negative_controls WHERE created_at >= datetime('now', '-7 days')"
    ).fetchone()[0]
    if new_ncs > 5:
        alerts.append(f"⚠️ NC-Wachstum: {new_ncs} neue Negative Controls in 7 Tagen")

    cb_cycle = conn.execute(
        "SELECT id FROM lab_cycles WHERE status='circuit_broken' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if cb_cycle:
        alerts.append(f"🚨 Circuit-Breaker aktiv auf Cycle #{cb_cycle['id']}")

    conn.close()

    if alerts:
        msg = "🔍 <b>Lab Health Check</b>\n\n" + "\n".join(alerts)
        log(f"[lab-ctrl] Health-Check-Alerts:\n" + "\n".join(alerts))
        _send_telegram(msg)
    else:
        log("[lab-ctrl] Health-Check: alle OK")


# ─── Heartbeat ───────────────────────────────────────────────────────────────

def mode_heartbeat(db_path: str) -> None:
    if not APEX_DB.exists():
        return
    conn = sqlite3.connect(str(APEX_DB))
    try:
        conn.execute(
            """INSERT OR REPLACE INTO heartbeats (component, last_seen, status, metadata)
               VALUES ('lab_controller', CURRENT_TIMESTAMP, 'running', '{}')"""
        )
        conn.commit()
    except Exception as e:
        log(f"[lab-ctrl] Heartbeat-Fehler: {e}")
    finally:
        conn.close()


# ─── Status ──────────────────────────────────────────────────────────────────

def mode_status(db_path: str) -> None:
    conn = get_lab_state_connection(db_path)
    cycle = conn.execute(
        "SELECT * FROM lab_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not cycle:
        log("[lab-ctrl] Kein Cycle in der DB")
        conn.close()
        return

    log(f"[lab-ctrl] Letzter Cycle: #{cycle['id']} status={cycle['status']}")
    log(f"  Start: {cycle['cycle_start']}")
    log(f"  Ende:  {cycle['cycle_end'] or '—'}")
    if cycle["paused_reason"]:
        log(f"  Grund: {cycle['paused_reason']}")

    queue = conn.execute(
        "SELECT status, COUNT(*) as n FROM lab_queue WHERE cycle_id=? GROUP BY status",
        (cycle["id"],),
    ).fetchall()
    for row in queue:
        log(f"  Queue {row['status']}: {row['n']}")

    pending_bc = get_borderline_candidates_pending_review(conn)
    log(f"  Offene Borderline-Reviews: {len(pending_bc)}")
    conn.close()


# ─── Set-Config ──────────────────────────────────────────────────────────────

def mode_set_config(db_path: str, key: str, value: str, reason: str) -> None:
    conn = get_lab_state_connection(db_path)
    old = get_config_value(conn, key)
    set_config_value(conn, key, value, reason=reason)
    log_governance_event(
        conn, "config_changed", "lab_config", None,
        actor="user_manual",
        reason=f"{key}: {old} → {value} ({reason})",
    )
    conn.commit()
    conn.close()
    log(f"[lab-ctrl] Config geändert: {key}={value} (war: {old})")


# ─── Release-Lock ────────────────────────────────────────────────────────────

def mode_release_lock(db_path: str, lock_name: str, confirm: bool) -> None:
    if not confirm:
        log("[lab-ctrl] FEHLER: --confirm erforderlich für release-lock")
        sys.exit(1)
    conn = get_lab_state_connection(db_path)
    release_lock(conn, lock_name)
    conn.commit()
    conn.close()
    log(f"[lab-ctrl] Lock '{lock_name}' manuell freigegeben")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Lab-Controller")
    parser.add_argument("--mode", required=True, choices=[
        "asset-profile-update", "build-queue", "run-cycle", "resume-cycle",
        "run-continuous",
        "health-check", "heartbeat", "status", "generate-report",
        "set-config", "release-lock",
    ])
    parser.add_argument("--cycle-id",  type=int,   default=None)
    parser.add_argument("--pause",     type=int,   default=300,
                        help="Pause in Sekunden zwischen Cycles (nur run-continuous, default 300)")
    parser.add_argument("--key", type=str, default=None)
    parser.add_argument("--value", type=str, default=None)
    parser.add_argument("--reason", type=str, default="")
    parser.add_argument("--lock-name", type=str, default=None)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--db-path", type=str, default=str(LAB_STATE_DB))
    args = parser.parse_args()

    db = args.db_path

    if args.mode == "asset-profile-update":
        mode_asset_profile_update(db)
    elif args.mode == "build-queue":
        mode_build_queue(db)
    elif args.mode == "run-cycle":
        mode_run_cycle(db)
    elif args.mode == "run-continuous":
        mode_run_continuous(db, pause_s=args.pause)
    elif args.mode == "resume-cycle":
        if not args.cycle_id:
            log("[lab-ctrl] FEHLER: --cycle-id erforderlich für resume-cycle")
            sys.exit(1)
        mode_resume_cycle(db, args.cycle_id)
    elif args.mode == "health-check":
        mode_health_check(db)
    elif args.mode == "heartbeat":
        mode_heartbeat(db)
    elif args.mode == "status":
        mode_status(db)
    elif args.mode == "generate-report":
        report = generate_weekly_report(db)
        log(f"[lab-ctrl] Weekly-Report:\n{report}")
        _report_send_telegram(report)
    elif args.mode == "set-config":
        if not args.key or not args.value:
            log("[lab-ctrl] FEHLER: --key und --value erforderlich")
            sys.exit(1)
        mode_set_config(db, args.key, args.value, args.reason)
    elif args.mode == "release-lock":
        if not args.lock_name:
            log("[lab-ctrl] FEHLER: --lock-name erforderlich")
            sys.exit(1)
        mode_release_lock(db, args.lock_name, args.confirm)


if __name__ == "__main__":
    main()
