"""
A.3 — Tests für scripts/master_watchdog.py.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.master_watchdog import check_master_alive, STALE_THRESHOLD_MIN


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
