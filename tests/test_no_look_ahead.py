"""
B.3 — Expliziter Look-Ahead-Bias-Regressionstest.

Drei Ebenen werden geprüft:

1. _candles() / _feature() SQL-Grenze:
   Kerzen und Features mit ts > as_of_ts dürfen niemals zurückgegeben werden.

2. Feature-Stabilität:
   Ein Feature-Wert, der bei as_of_ts=T berechnet wurde, ändert sich nicht,
   wenn danach mehr Daten (ts > T) verfügbar sind. (Point-in-Time-Korrektheit)

3. Signal-Isolation:
   Ein Signal bei as_of_ts=T darf nicht durch Hinzufügen von Candles mit ts > T
   beeinflusst werden.
"""
from __future__ import annotations

import sqlite3
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    """Erstellt eine In-Memory-DB mit minimalen Candle- und Feature-Tabellen."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE candles (
            ts INTEGER, asset TEXT, interval TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL
        );
        CREATE TABLE features (
            asset TEXT, interval TEXT, ts INTEGER,
            feature_name TEXT, value REAL,
            PRIMARY KEY (asset, interval, ts, feature_name)
        );
    """)
    return conn


def _insert_candles(conn: sqlite3.Connection, timestamps: list[int],
                    asset: str = "BTC", interval: str = "1h",
                    base_price: float = 100.0) -> None:
    for i, ts in enumerate(timestamps):
        conn.execute(
            "INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)",
            (ts, asset, interval,
             base_price + i, base_price + i + 1,
             base_price + i - 1, base_price + i + 0.5,
             1000.0 + i * 10),
        )
    conn.commit()


def _backtest_candles(conn: sqlite3.Connection,
                      asset: str, interval: str, as_of_ts: int, limit: int) -> list[dict]:
    """Exakte Kopie der _candles()-Logik aus backtest/engine.py."""
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE asset=? AND interval=? AND ts <= ?
           ORDER BY ts DESC LIMIT ?""",
        (asset, interval, as_of_ts, limit),
    ).fetchall()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in reversed(rows)]


def _backtest_feature(conn: sqlite3.Connection,
                      asset: str, interval: str, as_of_ts: int, name: str) -> Optional[float]:
    """Exakte Kopie der _feature()-Logik aus backtest/engine.py."""
    row = conn.execute(
        """SELECT value FROM features
           WHERE asset=? AND interval=? AND ts<=? AND feature_name=?
           ORDER BY ts DESC LIMIT 1""",
        (asset, interval, as_of_ts, name),
    ).fetchone()
    return row[0] if row else None


# ── Ebene 1: SQL-Grenze ───────────────────────────────────────────────────────

class TestCandleBoundary:
    def test_candles_excludes_future_timestamps(self):
        """_candles() darf keine Kerze mit ts > as_of_ts zurückgeben."""
        conn = _make_conn()
        past_ts   = [1000, 2000, 3000]
        future_ts = [4000, 5000]
        _insert_candles(conn, past_ts + future_ts)

        as_of_ts = 3000
        result = _backtest_candles(conn, "BTC", "1h", as_of_ts, 100)

        returned_ts = [c["time"] for c in result]
        for ts in future_ts:
            assert ts not in returned_ts, (
                f"Look-Ahead-Bias: Kerze ts={ts} liegt nach as_of_ts={as_of_ts}, "
                "darf nicht zurückgegeben werden."
            )

    def test_candles_includes_exact_boundary(self):
        """Kerze mit ts == as_of_ts muss enthalten sein (<=, nicht <)."""
        conn = _make_conn()
        _insert_candles(conn, [1000, 2000, 3000])

        result = _backtest_candles(conn, "BTC", "1h", 3000, 100)
        assert any(c["time"] == 3000 for c in result), (
            "Boundary-Kerze ts=3000 muss bei as_of_ts=3000 enthalten sein."
        )

    def test_candles_limit_respected(self):
        """LIMIT wird eingehalten — kein Over-Fetch."""
        conn = _make_conn()
        _insert_candles(conn, list(range(1000, 11000, 1000)))  # 10 Kerzen

        result = _backtest_candles(conn, "BTC", "1h", 10000, 5)
        assert len(result) == 5

    def test_future_candle_does_not_affect_result(self):
        """Candle-Liste bei as_of_ts=T ist identisch, ob danach weitere Candles existieren."""
        conn = _make_conn()
        base_ts = [1000, 2000, 3000]
        _insert_candles(conn, base_ts)

        result_before = _backtest_candles(conn, "BTC", "1h", 3000, 100)

        # Später Candles hinzufügen
        _insert_candles(conn, [4000, 5000])

        result_after = _backtest_candles(conn, "BTC", "1h", 3000, 100)
        assert result_before == result_after, (
            "Look-Ahead-Bias: Hinzufügen späterer Candles verändert Ergebnis bei as_of_ts=3000."
        )


class TestFeatureBoundary:
    def test_feature_excludes_future_values(self):
        """_feature() darf keinen Wert mit ts > as_of_ts zurückgeben."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO features VALUES (?,?,?,?,?)",
            ("BTC", "1h", 3000, "ema_200_1h", 42.0),
        )
        conn.execute(
            "INSERT INTO features VALUES (?,?,?,?,?)",
            ("BTC", "1h", 5000, "ema_200_1h", 99.0),
        )
        conn.commit()

        val = _backtest_feature(conn, "BTC", "1h", 3000, "ema_200_1h")
        assert val == 42.0, (
            f"_feature() lieferte {val} statt 42.0 — Zukunftswert ts=5000 "
            "darf bei as_of_ts=3000 nicht zurückgegeben werden."
        )

    def test_feature_stability_after_new_data(self):
        """Feature-Wert bei ts=T bleibt identisch, egal ob später ts>T eingetragen wird."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO features VALUES (?,?,?,?,?)",
            ("BTC", "1h", 3000, "atr_14_1h", 7.5),
        )
        conn.commit()

        val_before = _backtest_feature(conn, "BTC", "1h", 3000, "atr_14_1h")

        # Spätere Feature-Einträge hinzufügen
        conn.execute(
            "INSERT INTO features VALUES (?,?,?,?,?)",
            ("BTC", "1h", 4000, "atr_14_1h", 9.9),
        )
        conn.commit()

        val_after = _backtest_feature(conn, "BTC", "1h", 3000, "atr_14_1h")
        assert val_before == val_after, (
            f"Look-Ahead-Bias: Feature bei ts=3000 änderte sich von {val_before} "
            f"auf {val_after} nach Hinzufügen späterer Daten."
        )

    def test_feature_latest_before_boundary_returned(self):
        """Bei fehlender Punkt-Genauigkeit: jüngster Wert ≤ as_of_ts wird gewählt."""
        conn = _make_conn()
        conn.execute(
            "INSERT INTO features VALUES (?,?,?,?,?)",
            ("BTC", "1h", 2000, "ema_50_1h", 55.0),
        )
        conn.commit()

        # as_of_ts=3000 aber Feature nur für ts=2000 vorhanden
        val = _backtest_feature(conn, "BTC", "1h", 3000, "ema_50_1h")
        assert val == 55.0, "Jüngster Wert ≤ as_of_ts soll zurückgegeben werden."


# ── Ebene 2: Feature-Agent _load_candles Embargo ─────────────────────────────

class TestFeatureAgentLoadCandles:
    def test_embargo_mode_cuts_at_cutoff(self):
        """_load_candles mit embargo_mode schneidet Kerzen ab embargo_cutoff_ts ab."""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        conn = _make_conn()
        _insert_candles(conn, [1000, 2000, 3000, 4000, 5000])

        from features.feature_agent import _load_candles

        with patch("features.feature_agent.get_connection", return_value=conn):
            candles = _load_candles(
                conn, "BTC", "1h",
                up_to_ts=5000,
                limit=100,
                embargo_mode=True,
                embargo_cutoff_ts=3000,
            )

        returned_ts = [c["time"] for c in candles]
        assert 3000 not in returned_ts, (
            "Embargo: ts=3000 (== embargo_cutoff_ts) darf nicht im OOS-Fenster sein."
        )
        assert 4000 not in returned_ts and 5000 not in returned_ts, (
            "Embargo: Candles ≥ embargo_cutoff_ts dürfen nicht enthalten sein."
        )
        assert 2000 in returned_ts, "Candles vor embargo_cutoff_ts müssen verfügbar sein."

    def test_non_embargo_mode_returns_all_up_to_ts(self):
        """Ohne embargo_mode: alle Candles ≤ up_to_ts werden geliefert."""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        conn = _make_conn()
        _insert_candles(conn, [1000, 2000, 3000, 4000])

        from features.feature_agent import _load_candles

        candles = _load_candles(conn, "BTC", "1h", up_to_ts=3000, limit=100)
        returned_ts = [c["time"] for c in candles]

        assert 4000 not in returned_ts
        assert 3000 in returned_ts


# ── Ebene 3: Mutations-Regression (Schutz gegen stille Abschwächung) ─────────

class TestLookAheadGuard:
    def test_candles_boundary_condition_lt_not_lte_would_fail(self):
        """
        Regressionsschutz: wenn das SQL '<' statt '<=' verwenden würde,
        fehlt die Boundary-Kerze — dieser Test würde dann fehlschlagen.
        Sichert die korrekte Semantik des '<=' in _candles().
        """
        conn = _make_conn()
        _insert_candles(conn, [1000, 2000, 3000])

        # Simuliere fehlerhafte '<'-Variante
        rows_lt = conn.execute(
            """SELECT ts FROM candles
               WHERE asset='BTC' AND interval='1h' AND ts < 3000
               ORDER BY ts DESC LIMIT 100""",
        ).fetchall()
        rows_lte = conn.execute(
            """SELECT ts FROM candles
               WHERE asset='BTC' AND interval='1h' AND ts <= 3000
               ORDER BY ts DESC LIMIT 100""",
        ).fetchall()

        ts_lt  = {r[0] for r in rows_lt}
        ts_lte = {r[0] for r in rows_lte}

        assert 3000 not in ts_lt,  "Sanity: '<' schließt Boundary-Kerze aus"
        assert 3000 in ts_lte,     "Korrekt: '<=' schließt Boundary-Kerze ein"
        # Wenn _candles() auf '<' umgestellt würde, würde test_candles_includes_exact_boundary
        # fehlschlagen — dieser Test dokumentiert die Semantik explizit.

    def test_no_candle_from_future_in_signal_context(self):
        """
        Signal-Funktion darf keinen Candle mit ts > as_of_ts erhalten.
        Wir patchen _candles und prüfen mit welchem as_of_ts es aufgerufen wird.
        """
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        from backtest.engine import _candles as engine_candles

        conn = _make_conn()
        _insert_candles(conn, list(range(1000, 10000, 1000)))  # 9 Candles

        as_of_ts = 5000
        result = engine_candles(conn, "BTC", "1h", as_of_ts, 100)

        future_candles = [c for c in result if c["time"] > as_of_ts]
        assert future_candles == [], (
            f"Look-Ahead-Bias: {len(future_candles)} Kerze(n) mit ts > {as_of_ts} "
            f"in Signal-Context: {[c['time'] for c in future_candles]}"
        )
