"""
v7 Re-Eval Smoke-Test (Phase 6).

Prüft: run_one() + _save_discovery() für 1 Strategie × 1 Asset
auf einer isolierten tmp-DB. Kein Live-DB-Zugriff.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as _db_mod
from core.db import run_migrations


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "apex_smoke.db")
    monkeypatch.setattr(_db_mod, "DB_PATH", db_file)
    run_migrations()
    return db_file


def _ensure_candles(db_path: str, asset: str = "BTC", n: int = 2000):
    """Füllt isolierte DB mit synthetischen 1h-Candles."""
    import random
    rng = random.Random(42)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now_ms = int(time.time() * 1000)
    step_ms = 3_600_000

    close = 50_000.0
    rows = []
    for i in range(n, 0, -1):
        ts = now_ms - i * step_ms
        close *= 1 + rng.gauss(0, 0.005)
        high = close * (1 + abs(rng.gauss(0, 0.003)))
        low  = close * (1 - abs(rng.gauss(0, 0.003)))
        vol  = rng.uniform(100, 1000)
        rows.append((ts, asset + "USDT", "1h", close * 0.998, high, low, close, vol))

    conn.executemany(
        "INSERT OR IGNORE INTO candles (ts, asset, interval, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_run_one_no_crash(isolated_db):
    """run_one() auf leerer DB (keine Candles) → liefert ReevalResult ohne Exception."""
    from scripts.run_v7_reeval import run_one
    result = run_one("squeeze", "BTC")
    assert result is not None
    assert result.strategy == "squeeze"
    assert result.asset == "BTC"
    assert isinstance(result.passed, bool)
    assert isinstance(result.fail_reasons, list)


def test_save_discovery_idempotent(isolated_db):
    """Doppelter _save_discovery() auf gleicher Strategie/Asset → nur 1 Zeile."""
    from scripts.run_v7_reeval import run_one, _save_discovery
    from core.db import get_connection

    result = run_one("squeeze", "BTC")
    conn = get_connection()

    id1 = _save_discovery(result, conn)
    id2 = _save_discovery(result, conn)  # INSERT OR IGNORE → 0

    count = conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE strategy='squeeze' AND asset='BTC' AND framework_version='v7'"
    ).fetchone()[0]
    conn.close()

    assert count == 1


def test_result_has_required_fields(isolated_db):
    """Alle Pflichtfelder des ReevalResult müssen populated sein."""
    from scripts.run_v7_reeval import run_one
    r = run_one("ema_pullback", "ETH")
    assert r.strategy == "ema_pullback"
    assert r.asset == "ETH"
    assert r.dsr_oos >= 0.0
    assert 0.0 <= r.pbo_val <= 1.0
    assert r.stability >= 0.0
    assert r.max_dd >= 0.0
    assert 0.0 <= r.composite <= 1.0
    assert len(r.weights_hash) == 16
    assert isinstance(r.params_json, str)
    json.loads(r.params_json)  # valides JSON


def test_framework_version_is_v7(isolated_db):
    """Gespeicherte Discovery hat framework_version='v7'."""
    from scripts.run_v7_reeval import run_one, _save_discovery
    from core.db import get_connection

    result = run_one("donchian_breakout", "SOL")
    conn = get_connection()
    _save_discovery(result, conn)

    row = conn.execute(
        "SELECT framework_version, lab_config_hash, composite_weights_hash "
        "FROM lab_discoveries WHERE strategy='donchian_breakout' AND asset='SOL' AND framework_version='v7'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["framework_version"] == "v7"
    assert row["lab_config_hash"] is not None  # Phase 3: Hash muss gesetzt sein
    assert row["composite_weights_hash"] is not None
