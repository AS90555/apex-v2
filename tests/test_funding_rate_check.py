"""
C.4 — Tests für FundingRateCheck in governance/checks.py.

Prüft:
- Normaler Funding-Wert (unter Warn-Schwelle) → ok
- Warn-Schwelle überschritten → FUNDING_WARN, Signal durchgelassen
- Block-Schwelle + gegen Signal-Richtung → rejected
- Block-Schwelle + gleiche Richtung → nur Warn, kein Block
- Fehlende Funding-Daten → fail-open (skip)
- Stale Funding-Daten (Alter > FUNDING_RATE_STALE_MIN) → fail-open (FUNDING_STALE)
- Frische Funding-Daten → nicht als stale klassifiziert
- funding_time parse-Fehler → fail-open
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from core.models import Signal
from governance.checks import FundingRateCheck
from config.settings import (
    FUNDING_RATE_WARN_THRESHOLD,
    FUNDING_RATE_BLOCK_THRESHOLD,
    FUNDING_RATE_STALE_MIN,
)


def _make_signal(direction: str = "long", asset: str = "BTC") -> Signal:
    return Signal(
        strategy="donchian_breakout",
        asset=asset,
        direction=direction,
        mode="dry_run",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_conn(
    rate: float | None = None,
    asset: str = "BTC",
    age_min: float = 1.0,
) -> sqlite3.Connection:
    """In-Memory-DB mit optionalem funding_rates-Eintrag."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE funding_rates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            asset        TEXT NOT NULL,
            funding_rate REAL NOT NULL,
            funding_time TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    if rate is not None:
        funding_time = (
            datetime.now(timezone.utc) - timedelta(minutes=age_min)
        ).isoformat()
        conn.execute(
            "INSERT INTO funding_rates (asset, funding_rate, funding_time) VALUES (?,?,?)",
            (asset, rate, funding_time),
        )
    conn.commit()
    return conn


class TestFundingRateCheckNormal:
    def test_below_warn_threshold_ok(self):
        rate = FUNDING_RATE_WARN_THRESHOLD * 0.5
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert passed is True
        assert "ok" in reason

    def test_above_warn_below_block_warns_but_passes(self):
        rate = (FUNDING_RATE_WARN_THRESHOLD + FUNDING_RATE_BLOCK_THRESHOLD) / 2
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert passed is True
        assert "FUNDING_WARN" in reason

    def test_above_warn_threshold_short_direction_warns(self):
        """Warn tritt unabhängig von Richtung auf wenn |rate| > WARN."""
        rate = FUNDING_RATE_WARN_THRESHOLD * 2
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("short"))
        assert passed is True
        assert "FUNDING_WARN" in reason


class TestFundingRateCheckBlock:
    def test_block_threshold_against_long_blocks(self):
        """Hohe positive Rate + Long-Signal → blockiert."""
        rate = FUNDING_RATE_BLOCK_THRESHOLD * 1.5
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert passed is False
        assert "funding_rate_block" in reason
        assert "long" in reason

    def test_block_threshold_against_short_blocks(self):
        """Hohe negative Rate + Short-Signal → blockiert."""
        rate = -FUNDING_RATE_BLOCK_THRESHOLD * 1.5
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("short"))
        assert passed is False
        assert "funding_rate_block" in reason

    def test_block_threshold_same_direction_only_warns(self):
        """Hohe positive Rate + Short-Signal (profitiert) → kein Block, nur Warn."""
        rate = FUNDING_RATE_BLOCK_THRESHOLD * 1.5
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("short"))
        assert passed is True
        assert "FUNDING_WARN" in reason

    def test_negative_rate_against_long_only_warns(self):
        """Negative Rate + Long-Signal (profitiert) → kein Block."""
        rate = -FUNDING_RATE_BLOCK_THRESHOLD * 1.5
        conn = _make_conn(rate=rate)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert passed is True


class TestFundingRateCheckMissingData:
    def test_no_data_fail_open(self):
        """Keine Funding-Daten → fail-open (Signal durchgelassen)."""
        conn = _make_conn(rate=None)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal())
        assert passed is True
        assert "skip" in reason
        assert "keine_daten" in reason

    def test_different_asset_no_data(self):
        """Funding-Daten für anderes Asset → skip für angefragtes Asset."""
        conn = _make_conn(rate=0.001, asset="ETH")
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal(asset="BTC"))
        assert passed is True
        assert "skip" in reason


class TestFundingRateCheckStale:
    def test_stale_funding_data_fail_open(self):
        """Funding-Daten älter als FUNDING_RATE_STALE_MIN → FUNDING_STALE, fail-open."""
        rate = FUNDING_RATE_BLOCK_THRESHOLD * 2  # Würde normalerweise blockieren
        conn = _make_conn(rate=rate, age_min=FUNDING_RATE_STALE_MIN + 30)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert passed is True, "Stale Daten sollen fail-open sein, nicht blockieren"
        assert "FUNDING_STALE" in reason
        assert "fail-open" in reason

    def test_fresh_funding_data_not_stale(self):
        """Frische Daten (1 Minute alt) → nicht als stale behandelt."""
        rate = FUNDING_RATE_BLOCK_THRESHOLD * 2
        conn = _make_conn(rate=rate, age_min=1.0)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal("long"))
        assert "FUNDING_STALE" not in reason

    def test_exactly_at_stale_boundary_is_stale(self):
        """Exakt FUNDING_RATE_STALE_MIN Minuten alt → als stale behandelt."""
        rate = 0.0001
        conn = _make_conn(rate=rate, age_min=FUNDING_RATE_STALE_MIN + 0.1)
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal())
        assert "FUNDING_STALE" in reason
        assert passed is True

    def test_parse_error_fail_open(self):
        """Korruptes funding_time-Format → fail-open."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE funding_rates (
                id INTEGER PRIMARY KEY, asset TEXT,
                funding_rate REAL, funding_time TEXT, created_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO funding_rates VALUES (1, 'BTC', 0.005, 'KEIN_DATUM', datetime('now'))"
        )
        conn.commit()
        with patch("governance.checks.get_connection", return_value=conn):
            passed, reason = FundingRateCheck().evaluate(_make_signal())
        assert passed is True
        assert "parse_error" in reason


class TestFundingRateCheckName:
    def test_check_name(self):
        assert FundingRateCheck().name == "funding_rate"
