"""
Tests für research/v72_staging_writer.py (Phase 4).
"""
from __future__ import annotations

import sqlite3
import pytest

from backtest.v7_eval import V7EvalResult
from core.staging_schema import STAGING_DDL
from research.v72_objective import V72TrialResult
from research.v72_staging_writer import (
    batch_write_v72,
    make_params_hash_v72,
    write_v72_discovery,
)


@pytest.fixture()
def staging_db(tmp_path):
    db_file = str(tmp_path / "staging_test.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.executescript(STAGING_DDL)
    conn.commit()
    yield conn
    conn.close()


def _make_eval(strategy="donchian_breakout", asset="BTC", **kw) -> V7EvalResult:
    defaults = dict(
        params_json="{}",
        dsr_oos=0.8, pbo_val=0.1, stability=0.7,
        max_dd=2.0, composite=0.65, weights_hash="abc",
        n_oos=200, oos_folds_n=18, passed=True, fail_reasons=[],
    )
    defaults.update(kw)
    return V7EvalResult(strategy=strategy, asset=asset, **defaults)


STUDY_HASH = "a" * 32
PARAMS = {"DC_PERIOD": 20, "VOL_FACTOR": 1.5}


def test_make_params_hash_deterministic():
    h1 = make_params_hash_v72("donchian_breakout", "BTC", PARAMS, STUDY_HASH)
    h2 = make_params_hash_v72("donchian_breakout", "BTC", PARAMS, STUDY_HASH)
    assert h1 == h2


def test_make_params_hash_params_dependent():
    h1 = make_params_hash_v72("donchian_breakout", "BTC", {"DC_PERIOD": 20}, STUDY_HASH)
    h2 = make_params_hash_v72("donchian_breakout", "BTC", {"DC_PERIOD": 30}, STUDY_HASH)
    assert h1 != h2


def test_make_params_hash_study_dependent():
    h1 = make_params_hash_v72("donchian_breakout", "BTC", PARAMS, "a" * 32)
    h2 = make_params_hash_v72("donchian_breakout", "BTC", PARAMS, "b" * 32)
    assert h1 != h2


def test_make_params_hash_length():
    h = make_params_hash_v72("donchian_breakout", "BTC", PARAMS, STUDY_HASH)
    assert len(h) == 32


def test_write_v72_discovery_inserted(staging_db):
    ev = _make_eval()
    rc = write_v72_discovery(staging_db, "donchian_breakout", "BTC", PARAMS, ev, STUDY_HASH)
    staging_db.commit()
    assert rc == 1

    row = staging_db.execute(
        "SELECT * FROM lab_discoveries WHERE framework_version='v7.2'"
    ).fetchone()
    assert row is not None
    assert row["strategy"] == "donchian_breakout"
    assert row["asset"] == "BTC"
    assert row["study_hash"] == STUDY_HASH
    assert row["objective_version"] == "v72.0"
    assert row["sync_status"] == "pending"


def test_write_v72_discovery_idempotent(staging_db):
    ev = _make_eval()
    rc1 = write_v72_discovery(staging_db, "donchian_breakout", "BTC", PARAMS, ev, STUDY_HASH)
    staging_db.commit()
    rc2 = write_v72_discovery(staging_db, "donchian_breakout", "BTC", PARAMS, ev, STUDY_HASH)
    staging_db.commit()
    assert rc1 == 1
    assert rc2 == 0

    count = staging_db.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE framework_version='v7.2'"
    ).fetchone()[0]
    assert count == 1


def test_batch_write_v72_inserts(staging_db):
    trials = [
        V72TrialResult(
            params={"DC_PERIOD": 20 + i},
            eval_result=_make_eval(),
            study_hash=STUDY_HASH,
            objective_version="v72.0",
            pruned=False,
        )
        for i in range(5)
    ]
    inserted, ignored = batch_write_v72(staging_db, trials)
    assert inserted == 5
    assert ignored == 0


def test_batch_write_v72_idempotent(staging_db):
    trials = [
        V72TrialResult(
            params={"DC_PERIOD": 20 + i},
            eval_result=_make_eval(),
            study_hash=STUDY_HASH,
            objective_version="v72.0",
            pruned=False,
        )
        for i in range(5)
    ]
    batch_write_v72(staging_db, trials)
    ins2, ign2 = batch_write_v72(staging_db, trials)
    assert ins2 == 0
    assert ign2 == 5
