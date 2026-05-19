"""
Lab-Evolution-Engine — Mutation-Strategien und Variant-Proposal.

REGEL: Startet KEINE Trials. Schreibt nur strategy_variants + evolution_events.
       Ist Consumer von fitness_records, Producer von strategy_variants.

Öffentliche API:
    propose_variants(conn, cycle_id, budget_trials) -> list[str]  # variant_ids
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Optional

from core.lab_families import get_family, all_families
from core.lab_lineage_tracker import depth_of, is_depth_exceeded
from core.lab_state_db import (
    write_variant, get_proposed_variants, log_evolution_event,
    StrategyVariant,
)
from core.utils import log

EXPLORATION_RATIO = 0.70  # 70% Random-Seed / Exploration
EXPLOITATION_RATIO = 0.30  # 30% Mutation / Crossover
MAX_FAMILY_BUDGET_PCT = 0.40  # max 40% Budget pro Familie
GAUSSIAN_SIGMA = 0.10  # sigma fuer Gaussian-Pertubation (10% der Range)
MAX_LINEAGE_DEPTH = 5


def _variant_id(family_id: str, strategy: str, asset: str, params: dict, ranges_version: str) -> str:
    """Deterministischer variant_id = SHA256(...)[:32]."""
    key = json.dumps({
        "family": family_id,
        "strategy": strategy,
        "asset": asset,
        "params": params,
        "ranges_version": ranges_version,
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _get_search_space(strategy: str) -> dict:
    """Lädt Search-Space-Ranges für eine Strategie."""
    try:
        from research.v72_search_space import SEARCH_SPACES
        return SEARCH_SPACES.get(strategy, {})
    except ImportError:
        return {}


def _get_top_trials(conn, strategy: str, asset: str, n: int = 3) -> list[dict]:
    """Holt Top-N Trials aus research_staging.db nach composite_score."""
    try:
        from research.v72_objective import compute_study_hash
        study_hash = compute_study_hash(strategy, asset)
        staging_path = __import__('os').path.join(
            __import__('os').path.dirname(__file__), "..", "data", "research_staging.db"
        )
        import sqlite3
        conn2 = sqlite3.connect(staging_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute(
            """
            SELECT params_json, composite_score, dsr_value, pbo_value,
                   stability_score, max_drawdown, n_oos
            FROM lab_discoveries
            WHERE strategy=? AND asset=? AND study_hash=?
            AND composite_score IS NOT NULL
            ORDER BY composite_score DESC
            LIMIT ?
            """,
            (strategy, asset, study_hash, n),
        ).fetchall()
        conn2.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_ranges_version() -> str:
    """Liest aktuelle Ranges-Version aus Search-Space-Config."""
    try:
        from research.lab_search_config import LAB_SEARCH_CFG
        return getattr(LAB_SEARCH_CFG, 'version', '1.0')
    except ImportError:
        return "1.0"


def _gaussian_pertubation(
    conn,
    strategy: str,
    asset: str,
    family_id: str,
    generation: int,
    parent_variant_id: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Mutiert Top-Trial-Parameter via Gaussian-Pertubation."""
    if rng is None:
        rng = random.Random()

    space = _get_search_space(strategy)
    if not space:
        return None

    top_trials = _get_top_trials(conn, strategy, asset, n=3)
    if not top_trials:
        return None

    # Nehme den besten Trial als Basis
    base_trial = top_trials[0]
    try:
        base_params = json.loads(base_trial["params_json"])
    except (json.JSONDecodeError, TypeError):
        return None

    new_params = {}
    for param_name, param_def in space.items():
        if param_name not in base_params:
            continue
        old_val = base_params[param_name]
        if isinstance(param_def, dict):
            lo = param_def.get("low", old_val)
            hi = param_def.get("high", old_val)
            param_range = hi - lo
            noise = rng.gauss(0, GAUSSIAN_SIGMA * param_range)
            new_val = old_val + noise
            new_val = max(lo, min(hi, new_val))
            if param_def.get("step") or isinstance(old_val, int):
                new_val = round(new_val)
            new_params[param_name] = new_val
        else:
            new_params[param_name] = old_val

    ranges_version = _get_ranges_version()
    vid = _variant_id(family_id, strategy, asset, new_params, ranges_version)

    # Elterninfo
    if parent_variant_id is None and top_trials:
        row = conn.execute(
            "SELECT variant_id FROM strategy_variants WHERE strategy=? AND asset=? "
            "AND status='evaluated' ORDER BY evaluated_at DESC LIMIT 1",
            (strategy, asset),
        ).fetchone()
        parent_variant_id = row["variant_id"] if row else None

    write_variant(
        conn, vid, family_id, strategy, asset,
        json.dumps(new_params), ranges_version, generation,
        proposed_by="mutation_gaussian",
        parent_variant_id=parent_variant_id,
    )

    if parent_variant_id:
        from core.lab_lineage_tracker import record_lineage
        record_lineage(conn, vid, parent_variant_id, "mutation",
                       {"method": "gaussian", "sigma": GAUSSIAN_SIGMA})

    log(f"[evolution] Gaussian-Variant: {strategy}/{asset} vid={vid[:8]}")
    return vid


def _random_seed_variant(
    conn,
    strategy: str,
    asset: str,
    family_id: str,
    generation: int,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Erzeugt Variant mit zufälligen Params aus voller Range."""
    if rng is None:
        rng = random.Random()

    space = _get_search_space(strategy)
    if not space:
        # Leere Params — trotzdem als Seed-Variant eintragen
        params: dict = {}
    else:
        params = {}
        for param_name, param_def in space.items():
            if isinstance(param_def, dict):
                lo = param_def.get("low", 0)
                hi = param_def.get("high", 1)
                val = rng.uniform(lo, hi)
                if param_def.get("step") or isinstance(lo, int):
                    val = round(val)
                params[param_name] = val

    ranges_version = _get_ranges_version()
    vid = _variant_id(family_id, strategy, asset, params, ranges_version)

    write_variant(
        conn, vid, family_id, strategy, asset,
        json.dumps(params), ranges_version, generation,
        proposed_by="random_seed",
    )
    log(f"[evolution] Random-Seed-Variant: {strategy}/{asset} vid={vid[:8]}")
    return vid


def _crossover_variant(
    conn,
    strategy: str,
    asset: str,
    family_id: str,
    generation: int,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Crossover zwischen zwei Top-Trials."""
    if rng is None:
        rng = random.Random()

    top_trials = _get_top_trials(conn, strategy, asset, n=3)
    if len(top_trials) < 2:
        return None

    space = _get_search_space(strategy)
    if not space:
        return None

    try:
        params_a = json.loads(top_trials[0]["params_json"])
        params_b = json.loads(top_trials[1]["params_json"])
    except (json.JSONDecodeError, TypeError):
        return None

    new_params = {}
    for param_name in space:
        if param_name in params_a and param_name in params_b:
            new_params[param_name] = params_a[param_name] if rng.random() < 0.5 else params_b[param_name]

    ranges_version = _get_ranges_version()
    vid = _variant_id(family_id, strategy, asset, new_params, ranges_version)

    write_variant(
        conn, vid, family_id, strategy, asset,
        json.dumps(new_params), ranges_version, generation,
        proposed_by="crossover",
    )
    log(f"[evolution] Crossover-Variant: {strategy}/{asset} vid={vid[:8]}")
    return vid


def _active_blocked_pairs(conn) -> set[tuple[str, str]]:
    """Liefert (strategy, asset)-Paare mit aktivem Negative-Control-Eintrag."""
    try:
        rows = conn.execute(
            "SELECT strategy, asset FROM negative_controls WHERE closed_at IS NULL"
        ).fetchall()
        return {(r["strategy"], r["asset"]) for r in rows}
    except Exception:
        return set()


def propose_variants(
    conn,
    cycle_id: int,
    budget_trials: int = 200,
    rng: Optional[random.Random] = None,
) -> list[str]:
    """
    Schlägt Variants für den nächsten Cycle vor.
    Budget-Split: 70% Random-Seed, 30% Mutation/Crossover.
    Gibt Liste von variant_ids zurück.
    """
    if rng is None:
        rng = random.Random()

    families = all_families()
    n_pairs = max(1, budget_trials // 50)  # Bei 200 Trials -> 4 Paare
    n_explore = max(1, round(n_pairs * EXPLORATION_RATIO))   # 70% = ~3
    n_exploit = max(0, n_pairs - n_explore)                  # 30% = ~1

    # Aktive Assets aus regime_history (einzige Regime-Wahrheitsquelle)
    try:
        rows = conn.execute(
            "SELECT DISTINCT asset FROM regime_history ORDER BY computed_at DESC"
        ).fetchall()
        assets = [r["asset"] for r in rows] or ["BTC", "ETH", "SOL"]
    except Exception:
        assets = ["BTC", "ETH", "SOL"]

    # Aktive Strategien aus lab_queue
    try:
        rows2 = conn.execute(
            "SELECT DISTINCT strategy FROM lab_queue WHERE status='completed' LIMIT 20"
        ).fetchall()
        strategies = [r["strategy"] for r in rows2]
    except Exception:
        strategies = []

    if not strategies:
        # Fallback: alle bekannten Familien-Mitglieder
        for fdef in families.values():
            strategies.extend(fdef.members)

    # NC-geblockte Kombinationen herausfiltern
    blocked_pairs = _active_blocked_pairs(conn)
    all_pairs = [(s, a) for s in strategies for a in assets]
    valid_pairs = [p for p in all_pairs if p not in blocked_pairs]
    if not valid_pairs:
        log(f"[evolution] Alle {len(all_pairs)} Kombinationen NC-geblockt — Fallback auf vollen Pool")
        valid_pairs = all_pairs

    proposed_ids: list[str] = []

    # Exploration: Random-Seed Variants
    for _ in range(n_explore):
        strategy, asset = rng.choice(valid_pairs)
        family_id = get_family(strategy) or "volume_action"
        vid = _random_seed_variant(conn, strategy, asset, family_id, cycle_id, rng)
        if vid:
            proposed_ids.append(vid)

    # Exploitation: Gaussian-Mutation auf Top-Trials
    for _ in range(n_exploit):
        row = conn.execute(
            """
            SELECT v.strategy, v.asset, v.family_id
            FROM strategy_variants v
            JOIN fitness_records f ON f.variant_id=v.variant_id
            WHERE v.status='evaluated'
            ORDER BY f.fitness DESC LIMIT 1
            """
        ).fetchone()
        if row and (row["strategy"], row["asset"]) not in blocked_pairs:
            vid = _gaussian_pertubation(
                conn, row["strategy"], row["asset"], row["family_id"],
                cycle_id, rng=rng,
            )
        else:
            # Kein evaluierter (oder NC-blockierter) Variant — Random-Seed aus valid_pairs
            strategy, asset = rng.choice(valid_pairs)
            family_id = get_family(strategy) or "volume_action"
            vid = _random_seed_variant(conn, strategy, asset, family_id, cycle_id, rng)
        if vid:
            proposed_ids.append(vid)

    log_evolution_event(
        conn,
        event_type="variants_proposed",
        cycle_id=cycle_id,
        actor="evolution_auto",
        payload={"n_proposed": len(proposed_ids), "variant_ids": proposed_ids},
    )

    log(f"[evolution] {len(proposed_ids)} Variants fuer Cycle #{cycle_id} proponiert")
    return proposed_ids
