"""
A.5 — Tests für core/telegram_dispatcher.py.
Prüft Dedupe, Rate-Limit und Error-Loop-Circuit-Breaker.
"""
from __future__ import annotations

import importlib
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


def _fresh_dispatcher():
    """Lädt den Dispatcher mit frischem Modul-State (reset aller Globals)."""
    mod_name = "core.telegram_dispatcher"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import core.telegram_dispatcher as d
    # Reset aller mutablen Globals
    d._dedupe_cache.clear()
    d._bucket_tokens = float(d.TG_RATE_LIMIT_BURST)
    d._bucket_last_refill = time.monotonic()
    d._cb_open = False
    d._cb_open_since = 0.0
    d._cb_window_timestamps.clear()
    return d


class TestDedupe:
    def test_duplicate_within_window_suppressed(self):
        d = _fresh_dispatcher()
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Hallo Welt")
            d.dispatch("Hallo Welt")
        assert mock_send.call_count == 1, "Duplikat innerhalb Fenster soll unterdrückt werden"

    def test_different_messages_both_sent(self):
        d = _fresh_dispatcher()
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Nachricht A")
            d.dispatch("Nachricht B")
        assert mock_send.call_count == 2

    def test_duplicate_after_window_sent_again(self):
        d = _fresh_dispatcher()
        with patch.object(d, "_send_raw") as mock_send:
            msg_hash = d._msg_hash("Alt")
            # Eintrag direkt abgelaufen setzen
            d._dedupe_cache[msg_hash] = time.time() - 1
            d.dispatch("Alt")
        assert mock_send.call_count == 1, "Abgelaufener Dedupe-Eintrag → Nachricht soll gesendet werden"


class TestRateLimit:
    def test_burst_exhausted_suppresses_extra(self):
        d = _fresh_dispatcher()
        # Burst auf 2 reduzieren
        d._bucket_tokens = 2.0
        with patch.object(d, "_send_raw") as mock_send:
            for i in range(5):
                d.dispatch(f"Msg {i}")
        assert mock_send.call_count == 2, "Nach Burst-Exhaustion sollen weitere Msgs unterdrückt werden"

    def test_tokens_refill_over_time(self):
        d = _fresh_dispatcher()
        d._bucket_tokens = 0.0
        # Letzten Refill weit in die Vergangenheit setzen → viele Tokens verfügbar
        d._bucket_last_refill = time.monotonic() - 120.0
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Nach Refill")
        assert mock_send.call_count == 1


class TestCircuitBreaker:
    def test_cb_opens_after_threshold(self):
        d = _fresh_dispatcher()
        # CB-Schwelle auf 3 drücken
        with patch("core.telegram_dispatcher.TG_CB_THRESHOLD", 3):
            with patch.object(d, "_send_raw"):
                # 3 Nachrichten senden — CB sollte beim 4. öffnen
                for i in range(3):
                    d.dispatch(f"Storm {i}")
            # Manuell prüfen: nächste Nachricht landet im CB-Fenster-Check
            # Wir rufen _check_and_update_cb direkt auf
            with d._lock:
                cb_open = d._check_and_update_cb(time.monotonic())
        assert cb_open is True, "CB soll nach Threshold öffnen"

    def test_cb_suppresses_messages_when_open(self):
        d = _fresh_dispatcher()
        d._cb_open = True
        d._cb_open_since = time.monotonic()
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Unterdrückt")
        assert mock_send.call_count == 0

    def test_cb_resets_after_quiet_period(self):
        d = _fresh_dispatcher()
        # CB offen setzen, Reset-Zeit weit in Vergangenheit
        d._cb_open = True
        d._cb_open_since = time.monotonic() - (d.TG_CB_RESET_MIN * 60 + 10)
        d._cb_window_timestamps.clear()  # keine neuen Nachrichten
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Nach Reset")
        assert mock_send.call_count == 1, "CB soll nach Quiet-Period schließen"
        assert d._cb_open is False

    def test_cb_stays_open_if_load_continues(self):
        d = _fresh_dispatcher()
        d._cb_open = True
        d._cb_open_since = time.monotonic() - (d.TG_CB_RESET_MIN * 60 + 10)
        # Neuere Einträge im Fenster vorhanden → kein Reset
        d._cb_window_timestamps.append(time.monotonic())
        with patch.object(d, "_send_raw") as mock_send:
            d.dispatch("Noch offen")
        assert mock_send.call_count == 0
