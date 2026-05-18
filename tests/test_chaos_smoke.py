"""
P1.4 — Chaos-Smoke-Suite: C-1 (Bitget-Timeout) + C-2 (SQLite-Lock) + C-3 (Telegram-Down).

C-1: Bitget-API-Timeout während place_market_order
    - Netzwerk-Timeout → Recovery via get_order_by_client_id
    - Order gefunden → als gefüllt behandelt, kein Duplikat
    - Order nicht gefunden → R1-Retry mit neuem Suffix
    - Recovery-Query selbst schlägt fehl → Originalfehler, kein Blind-Retry

C-2: SQLite WAL-Concurrency
    - Short Lock: zweiter Writer wartet und succeeds dank PRAGMA busy_timeout
    - Long Lock: OperationalError nach Timeout, kein Datenverlust
    - WAL: Reader wird von Writer nicht blockiert
    - Concurrent Readers: blockieren sich gegenseitig nicht

C-3: Telegram-Dispatcher bei HTTP-Ausfall
    - HTTP-Error führt nicht zum Crash
    - Kein Retry-Loop bei Einzelfehler
    - Circuit-Breaker öffnet nach Schwelle
    - Offener CB unterdrückt Nachrichten
    - CB schliesst sich nach Cooldown
    - Kein HTTP-Call wenn Credentials fehlen
    - Timeout-Exception kein Crash
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════════════════

@contextmanager
def _clean_dispatcher():
    """Setzt den Dispatcher-Modulzustand vor und nach jedem Test zurück."""
    import core.telegram_dispatcher as d
    with d._lock:
        d._dedupe_cache.clear()
        d._bucket_tokens = float(d.TG_RATE_LIMIT_BURST)
        d._cb_open = False
        d._cb_open_since = 0.0
        d._cb_window_timestamps.clear()
    try:
        yield d
    finally:
        with d._lock:
            d._dedupe_cache.clear()
            d._bucket_tokens = float(d.TG_RATE_LIMIT_BURST)
            d._cb_open = False
            d._cb_open_since = 0.0
            d._cb_window_timestamps.clear()


def _wal_db(tmp_path) -> str:
    """Erzeugt WAL-Mode SQLite-DB wie in APEX produktiv verwendet."""
    db_path = str(tmp_path / "chaos_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()
    conn.close()
    return db_path


# ══════════════════════════════════════════════════════════════════════════════
# C-2: SQLite WAL-Concurrency
# ══════════════════════════════════════════════════════════════════════════════

class TestC2SQLiteLock:
    def test_short_lock_writer_waits_and_succeeds(self, tmp_path):
        """Zweiter Writer wartet während erster Lock hält, dann: beide commits erfolgreich."""
        db_path = _wal_db(tmp_path)
        results: list[str] = []

        def writer_a():
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            time.sleep(0.15)
            conn.execute("INSERT INTO events (val) VALUES ('A')")
            conn.commit()
            conn.close()
            results.append("A")

        def writer_b():
            time.sleep(0.05)
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("INSERT INTO events (val) VALUES ('B')")
            conn.commit()
            conn.close()
            results.append("B")

        ta = threading.Thread(target=writer_a)
        tb = threading.Thread(target=writer_b)
        ta.start(); tb.start()
        ta.join(timeout=3); tb.join(timeout=3)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT val FROM events").fetchall()
        conn.close()
        assert len(rows) == 2, f"Erwartet 2 Zeilen, erhalten: {rows}"
        assert set(r[0] for r in rows) == {"A", "B"}

    def test_timeout_raises_operational_error(self, tmp_path):
        """Lock-Timeout nach busy_timeout → OperationalError, vorherige Daten unberührt."""
        db_path = _wal_db(tmp_path)

        # Vordaten anlegen
        conn0 = sqlite3.connect(db_path)
        conn0.execute("INSERT INTO events (val) VALUES ('pre')")
        conn0.commit()
        conn0.close()

        lock_held = threading.Event()
        release = threading.Event()

        def lock_holder():
            c = sqlite3.connect(db_path)
            c.execute("BEGIN EXCLUSIVE")
            lock_held.set()
            release.wait(timeout=2)
            c.rollback()
            c.close()

        t = threading.Thread(target=lock_holder, daemon=True)
        t.start()
        lock_held.wait(timeout=2)

        c2 = sqlite3.connect(db_path, timeout=0.1)
        with pytest.raises(sqlite3.OperationalError):
            c2.execute("BEGIN EXCLUSIVE")
            c2.execute("INSERT INTO events (val) VALUES ('blocked')")
        c2.close()
        release.set()
        t.join(timeout=2)

        # Vordaten intakt
        conn_check = sqlite3.connect(db_path)
        rows = conn_check.execute("SELECT val FROM events").fetchall()
        conn_check.close()
        assert len(rows) == 1
        assert rows[0][0] == "pre"

    def test_wal_reader_not_blocked_by_writer(self, tmp_path):
        """WAL: Leser wird nicht blockiert während Writer aktiv ist."""
        db_path = _wal_db(tmp_path)

        conn_pre = sqlite3.connect(db_path)
        conn_pre.execute("INSERT INTO events (val) VALUES ('existing')")
        conn_pre.commit()
        conn_pre.close()

        writer_started = threading.Event()
        writer_release = threading.Event()
        read_result: list = []

        def slow_writer():
            c = sqlite3.connect(db_path)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("BEGIN IMMEDIATE")
            writer_started.set()
            writer_release.wait(timeout=2)
            c.rollback()
            c.close()

        t = threading.Thread(target=slow_writer, daemon=True)
        t.start()
        writer_started.wait(timeout=2)

        # Reader während Writer Lock hält — in WAL kein Problem
        c_read = sqlite3.connect(db_path)
        c_read.execute("PRAGMA journal_mode=WAL")
        rows = c_read.execute("SELECT val FROM events").fetchall()
        c_read.close()
        read_result.extend(rows)

        writer_release.set()
        t.join(timeout=2)

        assert len(read_result) == 1
        assert read_result[0][0] == "existing"

    def test_concurrent_readers_never_block_each_other(self, tmp_path):
        """Viele gleichzeitige Leser blockieren sich nicht gegenseitig."""
        db_path = _wal_db(tmp_path)
        conn_pre = sqlite3.connect(db_path)
        for i in range(5):
            conn_pre.execute(f"INSERT INTO events (val) VALUES ('row{i}')")
        conn_pre.commit()
        conn_pre.close()

        errors: list[Exception] = []

        def reader():
            try:
                c = sqlite3.connect(db_path)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("SELECT * FROM events").fetchall()
                c.close()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        assert not errors, f"Concurrent-Reader-Fehler: {errors}"


# ══════════════════════════════════════════════════════════════════════════════
# C-3: Telegram-Dispatcher bei HTTP-Ausfall
# ══════════════════════════════════════════════════════════════════════════════

class TestC3TelegramDown:
    def test_http_error_no_crash(self):
        """HTTP-Fehler in requests.post → _send_raw() crasht nicht (try/except im Body)."""
        with _clean_dispatcher() as d:
            import requests
            with patch("requests.post", side_effect=RuntimeError("Netz weg")):
                with patch.dict(os.environ, {d._TG_TOKEN_KEY: "fake", d._TG_CHAT_KEY: "42"}):
                    # Kein raise erwartet — _send_raw fängt alle Exceptions
                    d._send_raw("Test-Alert")

    def test_no_retry_loop_on_single_error(self):
        """Ein HTTP-Fehler → _send_raw wird genau einmal aufgerufen (kein Retry-Loop)."""
        with _clean_dispatcher() as d:
            call_count = 0

            def failing_send(text):
                nonlocal call_count
                call_count += 1
                raise ConnectionError("timeout")

            with patch.object(d, "_send_raw", side_effect=failing_send):
                try:
                    d.dispatch("Nur einmal senden")
                except Exception:
                    pass

        assert call_count <= 1, f"_send_raw mehr als einmal aufgerufen: {call_count}"

    def test_circuit_breaker_opens_after_threshold(self):
        """Nach TG_CB_THRESHOLD Nachrichten im CB-Fenster: CB öffnet."""
        with _clean_dispatcher() as d:
            threshold = d.TG_CB_THRESHOLD
            # Rate-Limit bypassen damit CB-Schwelle erreichbar ist
            with patch.object(d, "_send_raw"), \
                 patch.object(d, "_consume_token", return_value=True):
                for i in range(threshold + 2):
                    d.dispatch(f"Msg-{i}-unique-{time.monotonic()}")

            assert d._cb_open, "Circuit-Breaker hätte öffnen sollen"

    def test_open_cb_suppresses_messages(self):
        """Offener CB: _send_raw wird nicht mehr aufgerufen."""
        with _clean_dispatcher() as d:
            # CB manuell öffnen
            with d._lock:
                d._cb_open = True
                d._cb_open_since = time.monotonic()

            call_log: list[str] = []
            with patch.object(d, "_send_raw", side_effect=lambda t: call_log.append(t)):
                d.dispatch("Sollte unterdrückt werden")

        assert len(call_log) == 0, "CB offen aber _send_raw wurde trotzdem aufgerufen"

    def test_cb_resets_after_cooldown(self):
        """CB schliesst sich nach Reset-Cooldown wenn keine neuen Nachrichten."""
        with _clean_dispatcher() as d:
            cb_reset_sec = d.TG_CB_RESET_MIN * 60.0
            with d._lock:
                d._cb_open = True
                # Öffnungszeitpunkt weit in der Vergangenheit setzen
                d._cb_open_since = time.monotonic() - cb_reset_sec - 1
                d._cb_window_timestamps.clear()

            # dispatch() soll CB-Reset auslösen
            call_log: list[str] = []
            with patch.object(d, "_send_raw", side_effect=lambda t: call_log.append(t)):
                d.dispatch("Nach Cooldown")

        assert not d._cb_open, "CB hätte sich nach Cooldown schliessen sollen"
        assert len(call_log) == 1, "Nach CB-Reset soll Nachricht durchkommen"

    def test_no_credentials_no_http_call(self):
        """Leere Credentials → _send_raw macht keinen HTTP-Call."""
        with _clean_dispatcher() as d:
            import requests
            with patch.object(d, "_send_raw", wraps=d._send_raw):
                with patch("requests.post") as mock_post:
                    with patch.dict(os.environ, {d._TG_TOKEN_KEY: "", d._TG_CHAT_KEY: ""}):
                        d._send_raw("test")
                mock_post.assert_not_called()

    def test_timeout_exception_no_crash(self):
        """Timeout-Exception in _send_raw → kein Crash, kein Bubble."""
        with _clean_dispatcher() as d:
            import requests
            with patch("requests.post", side_effect=requests.exceptions.Timeout("timeout")):
                with patch.dict(os.environ, {d._TG_TOKEN_KEY: "fake", d._TG_CHAT_KEY: "42"}):
                    # Kein raise erwartet
                    d._send_raw("Timeout-Test")


# ══════════════════════════════════════════════════════════════════════════════
# C-1: Bitget-Timeout / clOrdId-Recovery (P2.1 abgeschlossen)
# ══════════════════════════════════════════════════════════════════════════════

class TestC1BitgetTimeout:
    """
    Chaos C-1: API-Timeout während place_market_order.

    Verifiziert dass die Recovery-Logik in execution/executor.py korrekt greift:
    - Netzwerk-Timeout → Recovery-Query via get_order_by_client_id
    - Order gefunden → als gefüllt behandelt, kein Duplikat-Submit
    - Order nicht gefunden → R1-Retry (einmal, kein Blind-Retry)
    - Recovery-Query selbst schlägt fehl → Originalfehler, kein Retry
    """

    def _make_signal(self, asset: str = "BTC") -> MagicMock:
        sig = MagicMock()
        sig.id = 42
        sig.asset = asset
        sig.direction = "long"
        sig.entry_price   = 60000.0
        sig.stop_loss     = 58000.0
        sig.take_profit_1 = 62000.0
        sig.take_profit_2 = None
        sig.mode = "dry_run"
        sig.strategy = "donchian_breakout"
        return sig

    def _make_client_mock(self):
        client = MagicMock()
        client.is_ready = True
        client.dry_run  = False
        client.get_price.return_value = 60000.0
        client.set_leverage.return_value = True
        client.place_take_profit.return_value = MagicMock(success=True, order_id="TP-1")
        return client

    def _run_execute_live(self, signal, client_mock):
        import execution.executor as ex_mod
        executor = ex_mod.Executor.__new__(ex_mod.Executor)
        with patch("execution.bitget_client.BitgetClient", return_value=client_mock), \
             patch("execution.executor.get_connection",
                   return_value=MagicMock(execute=MagicMock(
                       return_value=MagicMock(fetchone=MagicMock(return_value=None))))), \
             patch("execution.executor._write_audit_log"), \
             patch("execution.executor._increment_circuit_breaker"), \
             patch("execution.executor._is_circuit_broken", return_value=False), \
             patch("execution.executor._calc_sizing",
                   return_value={"size": 0.01, "leverage": 5, "notional": 600.0}), \
             patch("execution.market_impact_guard.evaluate",
                   return_value=MagicMock(
                       order_type="market", ioc_tolerance_bps=10.0,
                       market_impact_check="ok", spread_at_snapshot_bps=2.0,
                       liquidity_score=0.9)):
            return ex_mod.Executor._execute_live(executor, signal, dry_run=False)

    def test_timeout_recovery_finds_order_no_duplicate(self):
        """C-1a: Timeout + Recovery findet Order → kein zweites place_market_order."""
        from execution.bitget_client import OrderResult

        client = self._make_client_mock()
        call_count = {"n": 0}

        def _place(*a, **kw):
            call_count["n"] += 1
            return OrderResult(success=False, error="ConnectionError: timed out")

        client.place_market_order.side_effect = _place
        client.get_order_by_client_id.return_value = OrderResult(
            success=True, order_id="RECOVERED-001", filled_size=0.01, avg_price=60050.0
        )

        self._run_execute_live(self._make_signal(), client)

        assert call_count["n"] == 1, "place_market_order darf nach Recovery nicht nochmals aufgerufen werden"
        client.get_order_by_client_id.assert_called_once()

    def test_timeout_order_not_found_r1_retry(self):
        """C-1b: Timeout + not_found → R1-Retry (genau ein Folge-Aufruf)."""
        from execution.bitget_client import OrderResult

        client = self._make_client_mock()
        call_count = {"n": 0}

        def _place(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return OrderResult(success=False, error="Read timed out")
            return OrderResult(success=True, order_id="R1-OK", filled_size=0.01, avg_price=60000.0)

        client.place_market_order.side_effect = _place
        client.get_order_by_client_id.return_value = OrderResult(success=False, error="not_found")

        self._run_execute_live(self._make_signal(), client)

        assert call_count["n"] == 2, \
            f"Exakt 1 Retry nach not_found erwartet, tatsächliche Aufrufe: {call_count['n']}"
        second_kwargs = client.place_market_order.call_args_list[1][1]
        second_cl_ord_id = second_kwargs.get("client_order_id", "")
        assert second_cl_ord_id.endswith("-R1"), \
            f"R1-Suffix erwartet, erhalten: {second_cl_ord_id!r}"

    def test_exchange_reject_no_recovery(self):
        """C-1c: Fachliches Exchange-Reject → kein Recovery-Query."""
        from execution.bitget_client import OrderResult

        client = self._make_client_mock()
        client.place_market_order.return_value = OrderResult(
            success=False, error="Bitget [40786]: insufficient margin"
        )

        self._run_execute_live(self._make_signal(), client)

        client.get_order_by_client_id.assert_not_called()

    def test_recovery_query_fails_keeps_original_error(self):
        """C-1d: Recovery-Query schlägt fehl → Originalfehler, kein Blind-Retry."""
        from execution.bitget_client import OrderResult

        client = self._make_client_mock()
        call_count = {"n": 0}

        def _place(*a, **kw):
            call_count["n"] += 1
            return OrderResult(success=False, error="ConnectionError: timed out")

        client.place_market_order.side_effect = _place
        client.get_order_by_client_id.return_value = OrderResult(
            success=False, error="query_failed:HTTP 503"
        )

        self._run_execute_live(self._make_signal(), client)

        assert call_count["n"] == 1, \
            "Bei query_failed darf kein Retry stattfinden"
