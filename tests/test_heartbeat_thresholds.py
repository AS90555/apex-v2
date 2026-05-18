"""
D.2 — Tests für Heartbeat-Schwellen-Konfiguration.

Prüft:
- HEARTBEAT_THRESHOLDS_MIN in config/settings.py enthält alle Pflicht-Komponenten
- monitor/heartbeat.py nutzt dieselben Schwellen (THRESHOLDS_MIN == HEARTBEAT_THRESHOLDS_MIN)
- Schwellen-Override via settings wirkt sich auf check_all_heartbeats() aus
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config.settings as settings
from monitor.heartbeat import THRESHOLDS_MIN, check_all_heartbeats


_REQUIRED_COMPONENTS = {"intake", "features", "strategies", "governance", "executor", "monitor"}


class TestHeartbeatThresholdsConfig:
    def test_all_required_components_present(self):
        """Alle Pflicht-Komponenten haben Schwellen in settings."""
        assert _REQUIRED_COMPONENTS <= set(settings.HEARTBEAT_THRESHOLDS_MIN.keys())

    def test_thresholds_are_positive(self):
        for comp, val in settings.HEARTBEAT_THRESHOLDS_MIN.items():
            assert val > 0, f"{comp}: Schwelle muss > 0 sein"

    def test_intake_features_stricter_than_executor(self):
        """Zeitkritische Komponenten haben engere Schwellen."""
        t = settings.HEARTBEAT_THRESHOLDS_MIN
        assert t["intake"] <= t["executor"]
        assert t["features"] <= t["executor"]

    def test_heartbeat_module_uses_settings(self):
        """monitor/heartbeat.THRESHOLDS_MIN ist identisch mit settings."""
        assert THRESHOLDS_MIN is settings.HEARTBEAT_THRESHOLDS_MIN


class TestHeartbeatCheckUsesThresholds:
    def _make_conn(self, component: str, age_min: float) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE heartbeats (
                id INTEGER PRIMARY KEY, ts TEXT, component TEXT,
                status TEXT, message TEXT, latency_ms REAL
            )
        """)
        ts = (datetime.now(timezone.utc) - timedelta(minutes=age_min)).isoformat()
        conn.execute(
            "INSERT INTO heartbeats (ts, component, status, message) VALUES (?,?,?,?)",
            (ts, component, "ok", "test"),
        )
        conn.commit()
        return conn

    def test_fresh_heartbeat_no_alert(self):
        """Frischer Heartbeat → kein Alarm."""
        conn = self._make_conn("intake", age_min=2.0)
        with patch("monitor.heartbeat.get_connection", return_value=conn):
            alerts = check_all_heartbeats()
        intake_alerts = [a for a in alerts if a["component"] == "intake"]
        assert intake_alerts == []

    def test_stale_heartbeat_raises_alert(self):
        """Veralteter Heartbeat → Alarm mit korrekter threshold_min."""
        conn = self._make_conn("intake", age_min=20.0)
        with patch("monitor.heartbeat.get_connection", return_value=conn):
            alerts = check_all_heartbeats()
        intake_alerts = [a for a in alerts if a["component"] == "intake"]
        assert len(intake_alerts) == 1
        assert intake_alerts[0]["threshold_min"] == settings.HEARTBEAT_THRESHOLDS_MIN["intake"]

    def test_override_threshold_via_settings(self):
        """Schwellen-Override in settings wirkt auf check_all_heartbeats()."""
        modified = dict(settings.HEARTBEAT_THRESHOLDS_MIN)
        modified["intake"] = 60  # großzügiger Override
        conn = self._make_conn("intake", age_min=20.0)  # würde normalerweise Alarm triggern
        with patch("monitor.heartbeat.get_connection", return_value=conn):
            with patch("monitor.heartbeat.THRESHOLDS_MIN", modified):
                alerts = check_all_heartbeats()
        intake_alerts = [a for a in alerts if a["component"] == "intake"]
        assert intake_alerts == []
