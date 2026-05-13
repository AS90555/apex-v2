"""
v7.2 Staging-Writer (Phase 4).

Batch-Write von V72TrialResult in research_staging.db.
Idempotent via INSERT OR IGNORE + SHA256-basiertem params_hash.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from backtest.v7_eval import V7EvalResult
from config.settings import OBJECTIVE_V72_VERSION
from research.v72_objective import V72TrialResult


def make_params_hash_v72(strategy: str, asset: str, params: dict, study_hash: str) -> str:
    """SHA256(f'v72__{strategy}__{asset}__{sorted_json(params)}__{study_hash}')[:32]"""
    payload = f"v72__{strategy}__{asset}__{json.dumps(params, sort_keys=True)}__{study_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def write_v72_discovery(
    conn: sqlite3.Connection,
    strategy: str,
    asset: str,
    params: dict,
    eval_result: V7EvalResult,
    study_hash: str,
    objective_version: str = OBJECTIVE_V72_VERSION,
) -> int:
    """
    INSERT OR IGNORE in research_staging.db mit framework_version='v7.2'.
    Gibt rowcount zurück (0 = bereits vorhanden / ignoriert, 1 = neu eingefügt).
    """
    params_hash = make_params_hash_v72(strategy, asset, params, study_hash)
    now = datetime.now(timezone.utc).isoformat()

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO lab_discoveries (
            discovered_at, params_hash, strategy, asset, params_json,
            framework_version, dsr_value, pbo_value, stability_score,
            max_drawdown, composite_score, oos_folds_n,
            study_hash, objective_version, sync_status
        ) VALUES (
            ?, ?, ?, ?, ?,
            'v7.2', ?, ?, ?,
            ?, ?, ?,
            ?, ?, 'pending'
        )
        """,
        (
            now,
            params_hash,
            strategy,
            asset,
            json.dumps(params, sort_keys=True),
            eval_result.dsr_oos,
            eval_result.pbo_val,
            eval_result.stability,
            eval_result.max_dd,
            eval_result.composite,
            eval_result.oos_folds_n,
            study_hash,
            objective_version,
        ),
    )
    return cur.rowcount


def batch_write_v72(
    conn: sqlite3.Connection,
    trial_results: list[V72TrialResult],
) -> tuple[int, int]:
    """
    Schreibt N Trials in einer Transaktion.
    Gibt (inserted, ignored) zurück.
    """
    inserted = 0
    ignored = 0
    for tr in trial_results:
        rc = write_v72_discovery(
            conn,
            strategy=tr.eval_result.strategy,
            asset=tr.eval_result.asset,
            params=tr.params,
            eval_result=tr.eval_result,
            study_hash=tr.study_hash,
            objective_version=tr.objective_version,
        )
        if rc == 1:
            inserted += 1
        else:
            ignored += 1
    conn.commit()
    return inserted, ignored
