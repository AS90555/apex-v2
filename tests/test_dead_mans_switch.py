"""
D.3 — Integrationstest für den Dead-Man-Switch.

Prüft den vollständigen Detektions- und Alert-Pfad:
  1. Heartbeat-Datei ist stale (alt) oder fehlt
  2. master_watchdog.check_master_alive() erkennt Stille
  3. master_watchdog.main() sendet Telegram-Alert via Dispatcher

Der Test mockt ausschließlich I/O-Grenzen (Heartbeat-Dateien via process_lock,
DB-Query, Dispatcher) — die gesamte Erkennungslogik läuft real.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.master_watchdog import (
    STALE_THRESHOLD_MIN,
    check_master_alive,
    main,
)


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _stale_ts(extra_min: float = 5.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=STALE_THRESHOLD_MIN + extra_min)


def _fresh_ts(age_min: float = 2.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=age_min)


# ── End-to-End: Heartbeat-Datei stale → Alarm ────────────────────────────────

class TestDMSFileHeartbeatIntegration:
    def test_stale_file_heartbeat_triggers_alarm(self):
        """
        Stale File-Heartbeat + kein DB-Heartbeat
        → check_master_alive gibt alive=False zurück.
        """
        stale = _stale_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=stale):
                status = check_master_alive()
        assert status["alive"] is False
        assert status["age_min"] >= STALE_THRESHOLD_MIN
        assert status["source"] == "file_heartbeats"

    def test_missing_file_heartbeat_triggers_alarm(self):
        """
        Keine Heartbeat-Datei + kein DB-Heartbeat
        → alive=False, source='none'.
        """
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                status = check_master_alive()
        assert status["alive"] is False
        assert status["source"] == "none"

    def test_fresh_file_heartbeat_no_alarm(self):
        """Frische Heartbeat-Datei → alive=True."""
        fresh = _fresh_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=fresh):
                status = check_master_alive()
        assert status["alive"] is True


class TestDMSAlertPfad:
    """Vollständiger Pfad: stale Heartbeat → main() → Telegram-Alert."""

    def test_stale_triggers_telegram_alert(self):
        """Stale Heartbeat → main() gibt 1 zurück und ruft dispatch() auf."""
        stale = _stale_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=stale):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    rc = main()
        assert rc == 1
        mock_dispatch.assert_called_once()
        msg = mock_dispatch.call_args[0][0]
        assert "ALARM" in msg
        assert "master_run" in msg

    def test_no_heartbeat_triggers_telegram_alert(self):
        """Kein Heartbeat → Alert wird gesendet."""
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=None):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    rc = main()
        assert rc == 1
        mock_dispatch.assert_called_once()

    def test_fresh_heartbeat_no_alert_sent(self):
        """Frischer Heartbeat → kein Telegram-Alert."""
        fresh = _fresh_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=fresh):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    rc = main()
        assert rc == 0
        mock_dispatch.assert_not_called()

    def test_db_heartbeat_stale_but_file_fresh_no_alarm(self):
        """
        DB stale, aber File frisch → alive=True (neueste Quelle gewinnt).
        """
        stale = _stale_ts()
        fresh = _fresh_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=stale):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=fresh):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    rc = main()
        assert rc == 0
        mock_dispatch.assert_not_called()

    def test_alert_contains_age_minutes(self):
        """Alert-Nachricht enthält die Stille-Dauer in Minuten."""
        stale = _stale_ts(extra_min=10.0)  # ~25 Min Stille
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=stale):
                with patch("scripts.master_watchdog.dispatch") as mock_dispatch:
                    main()
        msg = mock_dispatch.call_args[0][0]
        # Nachricht enthält gerundete Minuten-Angabe
        assert "min" in msg.lower()

    def test_alert_network_failure_does_not_crash(self):
        """Netzwerkfehler im Dispatcher → main() läuft durch, gibt 1 zurück."""
        stale = _stale_ts()
        with patch("scripts.master_watchdog._newest_db_heartbeat", return_value=None):
            with patch("scripts.master_watchdog._newest_file_heartbeat", return_value=stale):
                with patch("scripts.master_watchdog.dispatch",
                           side_effect=ConnectionError("Netzwerk weg")):
                    rc = main()
        assert rc == 1
