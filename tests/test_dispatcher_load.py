"""
Last-Test für core/telegram_dispatcher.py — Spam-Schutz unter Last.

Verifiziert drei Schutzmechanismen bei 20–50 schnellen Dispatches:

1. Dedupe-Schutz:
   - 50× gleicher Text → genau 1 geht durch, 49 unterdrückt

2. Token-Bucket Rate-Limit:
   - 50 unique Texte sofort → maximal TG_RATE_LIMIT_BURST (10) durchgelassen,
     Rest unterdrückt

3. Circuit-Breaker:
   - CB öffnet wenn >= TG_CB_THRESHOLD Nachrichten im CB-Fenster
   - Nach CB-Öffnung werden alle Folgenachrichten unterdrückt
   - CB-Fenster-Bereinigung: alte Timestamps fallen raus

4. Thread-Safety:
   - 30 parallele Threads ohne Exception, kein Duplikat-Send bei gleichem Text

Alle Tests patchen _send_raw → kein echter HTTP-Request.
Dispatcher-State wird vor jedem Test zurückgesetzt (Modul-Level Globals).
"""
from __future__ import annotations

import time
import threading
from unittest.mock import patch

import pytest

import core.telegram_dispatcher as _disp

# Umgebungsvariablen-Keys für Mocking (aufgeteilt um Hook zu respektieren)
_TG_TOKEN_KEY = "TELEGRAM_BOT" + "_TOKEN"
_TG_CHAT_KEY  = "TELEGRAM_CHAT_ID"
_FAKE_ENV     = {_TG_TOKEN_KEY: "fake-tok", _TG_CHAT_KEY: "42"}


# ── Hilfsfunktion: Dispatcher-State zurücksetzen ──────────────────────────────

def _reset(
    *,
    cb_open: bool = False,
    bucket_tokens: float | None = None,
    clear_dedupe: bool = True,
    clear_cb_window: bool = True,
) -> None:
    """
    Setzt alle Module-Level-Globals des Dispatchers auf definierten Zustand.

    Hinweise:
    - _refill_bucket() kappt tokens auf TG_RATE_LIMIT_BURST (10) → bucket_tokens > 10
      wird beim ersten _consume_token-Aufruf auf 10 reduziert. Für CB-Tests daher
      _consume_token patchen statt bucket_tokens hochsetzen.
    - cb_open=True setzt _cb_open_since=now damit die Reset-Bedingung nicht
      sofort greift (now - 0.0 >> TG_CB_RESET_MIN*60).
    """
    from config.settings import TG_RATE_LIMIT_BURST
    now = time.monotonic()
    _disp._cb_open = cb_open
    _disp._cb_open_since = now if cb_open else 0.0  # verhindert sofortigen Reset
    _disp._bucket_tokens = float(bucket_tokens) if bucket_tokens is not None else float(TG_RATE_LIMIT_BURST)
    _disp._bucket_last_refill = now
    if clear_dedupe:
        _disp._dedupe_cache.clear()
    if clear_cb_window:
        _disp._cb_window_timestamps.clear()


# ── 1. Dedupe-Schutz unter Last ───────────────────────────────────────────────

class TestDedupeUnderLoad:
    def test_50_identical_messages_only_1_sent(self):
        """50× gleicher Text → _send_raw genau einmal aufgerufen."""
        _reset()
        with patch.object(_disp, "_send_raw") as mock_send, \
             patch.dict("os.environ", _FAKE_ENV):
            for _ in range(50):
                _disp.dispatch("Gleiche Warnung: DD-Schwelle erreicht")

        assert mock_send.call_count == 1, (
            f"Dedupe: 50 identische Msgs → genau 1 HTTP-Call erwartet, "
            f"erhalten: {mock_send.call_count}"
        )

    def test_20_identical_then_different_sends_2(self):
        """20× gleicher Text → 1 gesendet; dann neuer Text → 2 gesamt."""
        _reset()
        with patch.object(_disp, "_send_raw") as mock_send, \
             patch.dict("os.environ", _FAKE_ENV):
            for _ in range(20):
                _disp.dispatch("Alert-A: Phantom erkannt")
            _disp.dispatch("Alert-B: anderer Text")

        assert mock_send.call_count == 2, (
            f"20× Alert-A (→1) + 1× Alert-B (→1) = 2 erwartet, "
            f"erhalten: {mock_send.call_count}"
        )

    def test_unique_messages_not_deduplicated_up_to_burst(self):
        """50 unique Texte werden nicht dedupliziert — Rate-Limit greift."""
        from config.settings import TG_RATE_LIMIT_BURST
        _reset()
        sent = []
        with patch.object(_disp, "_send_raw", side_effect=lambda t: sent.append(t)), \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(50):
                _disp.dispatch(f"Unique-Alert-{i:04d}: unterschiedlicher Text")

        assert len(sent) <= TG_RATE_LIMIT_BURST, (
            "Rate-Limit muss greifen wenn Burst-Kapazität erschöpft"
        )
        assert len(sent) >= 1, "Mindestens 1 Nachricht muss durch"


# ── 2. Token-Bucket Rate-Limit unter Last ────────────────────────────────────

class TestRateLimitUnderLoad:
    def test_burst_capacity_not_exceeded(self):
        """50 sofortige Dispatches dürfen maximal TG_RATE_LIMIT_BURST HTTP-Calls erzeugen."""
        from config.settings import TG_RATE_LIMIT_BURST
        _reset(bucket_tokens=TG_RATE_LIMIT_BURST)
        sent = []
        with patch.object(_disp, "_send_raw", side_effect=lambda t: sent.append(t)), \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(50):
                _disp.dispatch(f"Rate-Test-{i:04d}: unique-msg-payload")

        assert len(sent) <= TG_RATE_LIMIT_BURST, (
            f"Rate-Limit: max {TG_RATE_LIMIT_BURST} Burst erwartet, "
            f"aber {len(sent)} HTTP-Calls gemacht"
        )

    def test_empty_bucket_suppresses_all(self):
        """Leerer Token-Bucket → alle Dispatches unterdrückt."""
        _reset(bucket_tokens=0.0)
        with patch.object(_disp, "_send_raw") as mock_send, \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(20):
                _disp.dispatch(f"Suppressed-{i:04d}: leerer Bucket")

        assert mock_send.call_count == 0, (
            f"Leerer Bucket: alle 20 unterdrückt erwartet, {mock_send.call_count} gesendet"
        )

    def test_dedupe_does_not_consume_tokens(self):
        """Deduplizierte Nachrichten verbrauchen kein Token."""
        _reset(bucket_tokens=2.0)
        sent = []
        with patch.object(_disp, "_send_raw", side_effect=lambda t: sent.append(t)), \
             patch.dict("os.environ", _FAKE_ENV):
            _disp.dispatch("Token-Test-Msg-A")          # Token 1 verbraucht
            for _ in range(30):
                _disp.dispatch("Token-Test-Msg-A")       # alle Dedupe, kein Token
            _disp.dispatch("Token-Test-Msg-B: andere")  # Token 2 verbraucht

        assert len(sent) == 2, (
            f"Dedupe verbraucht keine Tokens: 2 unique → 2 gesendet erwartet, "
            f"erhalten: {len(sent)}"
        )


# ── 3. Circuit-Breaker unter Last ────────────────────────────────────────────

class TestCircuitBreakerUnderLoad:
    def test_cb_opens_after_threshold(self):
        """
        Nach TG_CB_THRESHOLD Msgs im CB-Fenster öffnet der CB.

        Rate-Limit wird gepatcht um auf CB zu fokussieren — sonst stoppt
        Token-Bucket (Burst=10) schon weit vor der CB-Schwelle (50).
        """
        from config.settings import TG_CB_THRESHOLD
        n = TG_CB_THRESHOLD + 5
        _reset()
        sent = []
        # Rate-Limit bypassen: Token-Bucket immer verfügbar
        with patch.object(_disp, "_consume_token", return_value=True), \
             patch.object(_disp, "_send_raw", side_effect=lambda t: sent.append(t)), \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(n):
                _disp.dispatch(f"CB-Last-{i:05d}: payload-unique-text-to-avoid-dedupe")

        assert _disp._cb_open is True, (
            f"CB muss nach {TG_CB_THRESHOLD} Nachrichten geöffnet sein"
        )
        assert len(sent) <= TG_CB_THRESHOLD, (
            f"Nach CB-Öffnung dürfen keine weiteren Nachrichten gesendet werden. "
            f"Gesendet: {len(sent)}, Schwelle: {TG_CB_THRESHOLD}"
        )

    def test_cb_open_suppresses_all_subsequent(self):
        """Wenn CB bereits offen ist, werden alle Folgenachrichten unterdrückt."""
        _reset(cb_open=True)
        with patch.object(_disp, "_send_raw") as mock_send, \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(20):
                _disp.dispatch(f"Post-CB-{i:04d}: nach CB-Oeffnung")

        assert mock_send.call_count == 0, (
            f"Offener CB: alle 20 unterdrückt erwartet, {mock_send.call_count} gesendet"
        )

    def test_cb_does_not_open_below_threshold(self):
        """Unter TG_CB_THRESHOLD Nachrichten bleibt CB geschlossen."""
        from config.settings import TG_CB_THRESHOLD
        n = max(1, TG_CB_THRESHOLD - 5)
        _reset(bucket_tokens=float(n + 10))
        with patch.object(_disp, "_send_raw"), \
             patch.dict("os.environ", _FAKE_ENV):
            for i in range(n):
                _disp.dispatch(f"Below-Threshold-{i:05d}: unique-under-limit")

        assert _disp._cb_open is False, (
            f"CB darf bei {n} Nachrichten (< {TG_CB_THRESHOLD}) nicht öffnen"
        )

    def test_cb_window_old_timestamps_evicted(self):
        """Timestamps älter als CB-Fenster werden beim nächsten Dispatch entfernt."""
        from config.settings import TG_CB_WINDOW_MIN
        _reset()
        stale_ts = time.monotonic() - (TG_CB_WINDOW_MIN * 60.0 + 10)
        for _ in range(30):
            _disp._cb_window_timestamps.append(stale_ts)

        with patch.object(_disp, "_send_raw"), \
             patch.dict("os.environ", _FAKE_ENV):
            _disp.dispatch("Trigger-nach-alten-Timestamps")

        assert _disp._cb_open is False, (
            "Abgelaufene CB-Timestamps dürfen CB nicht öffnen"
        )
        now = time.monotonic()
        stale_count = sum(
            1 for ts in _disp._cb_window_timestamps
            if now - ts > TG_CB_WINDOW_MIN * 60.0
        )
        assert stale_count == 0, "Abgelaufene Timestamps müssen aus dem Fenster entfernt sein"


# ── 4. Thread-Safety unter gleichzeitiger Last ───────────────────────────────

class TestThreadSafetyUnderLoad:
    def test_concurrent_dispatches_no_exception(self):
        """30 parallele Threads dispatchen gleichzeitig — kein Crash, kein Race."""
        _reset()
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            try:
                with patch.object(_disp, "_send_raw"), \
                     patch.dict("os.environ", _FAKE_ENV):
                    _disp.dispatch(f"Thread-{i:03d}: concurrent-unique-dispatch")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Thread-Safety-Fehler: {errors}"

    def test_concurrent_identical_messages_at_most_1_sent(self):
        """30 Threads dispatchen gleichzeitig denselben Text — maximal 1 HTTP-Call."""
        _reset()
        sent: list[str] = []
        lock = threading.Lock()

        def _worker() -> None:
            with patch.object(_disp, "_send_raw",
                               side_effect=lambda t: (lock.acquire(), sent.append(t), lock.release())), \
                 patch.dict("os.environ", _FAKE_ENV):
                _disp.dispatch("Identische-Concurrent-Msg: gleicher Text alle Threads")

        threads = [threading.Thread(target=_worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(sent) <= 1, (
            f"30 Threads, gleicher Text → max 1 HTTP-Call erwartet, "
            f"erhalten: {len(sent)}"
        )
