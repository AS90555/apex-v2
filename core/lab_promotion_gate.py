"""
E8 — Promotion-Gate: Verbindet Evolution-Layer mit bestehender Promotion-Pipeline.

Wenn ein Variant 'evaluated' ist und fitness_score ≥ FITNESS_PROMOTION_THRESHOLD,
wird run_v7_reeval.run_one() aufgerufen — das erzeugt einen lab_discoveries-Eintrag
in research_staging.db, den run_auto_promotion.py danach automatisch promoted.

Governance-Regeln:
- Kein Import von Gate-Konstanten (DSR_MIN_*, PBO_MAX etc.) — diese prüft run_v7_reeval selbst
- Kein direktes Schreiben nach lab_discoveries — das übernimmt run_v7_reeval._save_discovery()
- Idempotent: run_v7_reeval verwendet INSERT OR IGNORE via params_hash
"""
from __future__ import annotations

import queue
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

from core.utils import log

REEVAL_TIMEOUT_SEC = 600  # 10 Minuten — hängender Backtest blockt sonst den Cycle

FITNESS_PROMOTION_THRESHOLD = 0.60  # auf 0.0–1.5 Skala


@dataclass
class PromotionAttempt:
    variant_id: str
    strategy: str
    asset: str
    fitness_score: float
    triggered: bool
    result_passed: Optional[bool] = None
    fail_reasons: Optional[list[str]] = None
    error: Optional[str] = None


def promote_if_eligible(
    conn: sqlite3.Connection,
    variant_id: str,
    strategy: str,
    asset: str,
    fitness_score: float,
) -> PromotionAttempt:
    """
    Prüft ob ein evaluierter Variant die Promotion-Schwelle überschreitet.
    Bei GO: triggert run_v7_reeval.run_one() → lab_discoveries-Eintrag.
    Gibt immer ein PromotionAttempt zurück — bei Fehler mit error gesetzt.
    """
    attempt = PromotionAttempt(
        variant_id=variant_id,
        strategy=strategy,
        asset=asset,
        fitness_score=fitness_score,
        triggered=False,
    )

    if fitness_score < FITNESS_PROMOTION_THRESHOLD:
        return attempt

    # NC-Block prüfen (verhindert Re-Eval wenn Strategie/Asset blockiert)
    from core.lab_negative_controls import check_negative_control
    nc_result = check_negative_control(strategy, asset)
    if nc_result.blocked and not nc_result.reopen_available:
        log(f"[promotion-gate] {variant_id[:8]} {strategy}/{asset}: NC-Block — kein Re-Eval")
        return attempt

    attempt.triggered = True
    log(f"[promotion-gate] {variant_id[:8]} {strategy}/{asset}: "
        f"fitness={fitness_score:.3f} ≥ {FITNESS_PROMOTION_THRESHOLD} → Re-Eval")

    try:
        from scripts.run_v7_reeval import run_one, _save_discovery
        from core.db import get_connection

        result_q: queue.Queue = queue.Queue()

        def _worker() -> None:
            try:
                result_q.put(("ok", run_one(strategy, asset)))
            except Exception as exc:
                result_q.put(("err", exc))

        t = threading.Thread(target=_worker, daemon=True, name="reeval-worker")
        t.start()
        try:
            kind, value = result_q.get(timeout=REEVAL_TIMEOUT_SEC)
        except queue.Empty:
            # Thread läuft als Daemon weiter und stirbt mit dem Prozess
            attempt.error = "reeval_timeout"
            log(f"[promotion-gate] TIMEOUT: Re-Eval für {variant_id[:8]} "
                f"{strategy}/{asset} nach {REEVAL_TIMEOUT_SEC}s abgebrochen")
            return attempt

        if kind == "err":
            raise value  # fällt in äußeres except
        result = value

        attempt.result_passed = result.passed
        attempt.fail_reasons = result.fail_reasons

        staging_conn = get_connection()
        _save_discovery(result, staging_conn)
        staging_conn.close()

        status = "PASS" if result.passed else f"FAIL ({', '.join(result.fail_reasons)})"
        log(f"[promotion-gate] {strategy}/{asset} Re-Eval → {status} "
            f"DSR={result.dsr_oos:.3f} PBO={result.pbo_val:.3f}")

        _log_promotion_event(conn, variant_id, strategy, asset, fitness_score, result)

    except Exception as e:
        attempt.triggered = False
        attempt.error = str(e)
        log(f"[promotion-gate] WARNUNG: Re-Eval fehlgeschlagen für {variant_id[:8]}: {e}")

    return attempt


def get_promotion_candidates(
    conn: sqlite3.Connection,
    threshold: float = FITNESS_PROMOTION_THRESHOLD,
    limit: int = 10,
) -> list[dict]:
    """Gibt evaluierte Variants zurück die die Promotion-Schwelle überschreiten."""
    rows = conn.execute(
        """
        SELECT sv.variant_id, sv.strategy, sv.asset, sv.fitness_score,
               sv.family_id, sv.generation, sv.proposed_by
        FROM strategy_variants sv
        WHERE sv.status = 'evaluated'
        AND sv.fitness_score >= ?
        ORDER BY sv.fitness_score DESC
        LIMIT ?
        """,
        (threshold, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _log_promotion_event(
    conn: sqlite3.Connection,
    variant_id: str,
    strategy: str,
    asset: str,
    fitness_score: float,
    result,
) -> None:
    """Schreibt evolution_event für den Promotion-Attempt."""
    try:
        from core.lab_state_db import log_evolution_event
        metadata = {
            "fitness_score": round(fitness_score, 4),
            "dsr_oos": round(result.dsr_oos, 4),
            "pbo_val": round(result.pbo_val, 4),
            "stability": round(result.stability, 4),
            "passed": result.passed,
            "fail_reasons": result.fail_reasons,
        }
        log_evolution_event(
            conn,
            event_type="promotion_attempted",
            variant_id=variant_id,
            family_id=None,
            metadata=metadata,
        )
    except Exception:
        pass  # Event-Log ist best-effort
