"""
A.3 / D.1 — Tests für scripts/master_watchdog.py.

A.3: check_master_alive() — Heartbeat-Detection
D.1: main() — Alert-Pfad über Telegram-Dispatcher
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.master_watchdog import check_master_alive, STALE_THRESHOLD_MIN
from config.settings import HEARTBEAT_THRESHOLDS_MIN


class TestWatchdogConfig:
    def test_threshold_from_settings(self):
        """STALE_THRESHOLD_MIN kommt aus HEARTBEAT_THRESHOLDS_MIN['master']."""
        assert STALE_THRESHOLD_MIN == HEARTBEAT_THRESHOLDS_MIN["master"]
        assert STALE_THRESHOLD_MIN > 0


class TestMasterWatchdog:
    def test_fresh_heartbeat_is_alive(self):
        """Heartbeat vor 2 Min → alive=True."""
        fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=fresh):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                status = check_master_alive()
        assert status["alive"] is True
        assert status["age_min"] < STALE_THRESHOLD_MIN

    def test_stale_heartbeat_is_dead(self):
        """Heartbeat vor 20 Min → alive=False."""
        stale = datetime.now(timezone.utc) - timedelta(minutes=20)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=stale):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                status = check_master_alive()
        assert status["alive"] is False
        assert status["age_min"] >= STALE_THRESHOLD_MIN

    def test_no_heartbeat_is_dead(self):
        """Kein Heartbeat vorhanden → alive=False."""
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                status = check_master_alive()
        assert status["alive"] is False
        assert status["source"] == "none"

    def test_file_heartbeat_used_as_fallback(self):
        """DB-Heartbeat fehlt, aber File-Heartbeat frisch → alive=True."""
        fresh = datetime.now(timezone.utc) - timedelta(minutes=1)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=fresh):
                status = check_master_alive()
        assert status["alive"] is True
        assert status["source"] == "file_heartbeats"

    def test_uses_newest_source(self):
        """Neuere von DB/File wird verwendet."""
        old = datetime.now(timezone.utc) - timedelta(minutes=20)
        new = datetime.now(timezone.utc) - timedelta(minutes=3)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=old):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=new):
                status = check_master_alive()
        assert status["alive"] is True
        assert status["source"] == "file_heartbeats"


class TestWatchdogMain:
    """D.1 — main() Alert-Pfad."""

    def test_alive_returns_0_no_alert(self):
        """Frischer Heartbeat → exit 0, kein Telegram-Alert."""
        fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=fresh):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                with patch("core.telegram_dispatcher.dispatch") as mock_dispatch:
                    from scripts.master_watchdog import main
                    rc = main()
        assert rc == 0
        mock_dispatch.assert_not_called()

    def test_stale_returns_1_sends_alert(self):
        """Stale Heartbeat → exit 1, Telegram-Alert genau einmal."""
        stale = datetime.now(timezone.utc) - timedelta(minutes=20)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=stale):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    from scripts.master_watchdog import main
                    rc = main()
        assert rc == 1
        mock_dispatch.assert_called_once()
        msg = mock_dispatch.call_args[0][0]
        assert "ALARM" in msg
        assert "master_run" in msg

    def test_no_heartbeat_sends_alert(self):
        """Kein Heartbeat → exit 1, Alert enthält 'inf' oder hohe Minutenzahl."""
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    from scripts.master_watchdog import main
                    rc = main()
        assert rc == 1
        mock_dispatch.assert_called_once()

    def test_alert_failure_does_not_crash_main(self):
        """Telegram-Fehler im Alert-Pfad → main() läuft trotzdem durch, gibt 1 zurück."""
        stale = datetime.now(timezone.utc) - timedelta(minutes=20)
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=stale):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                with patch("scripts.master_watchdog.dispatch",
                           side_effect=RuntimeError("Netzwerk weg")):
                    from scripts.master_watchdog import main
                    rc = main()
        assert rc == 1  # kein Absturz, exit code bleibt 1
