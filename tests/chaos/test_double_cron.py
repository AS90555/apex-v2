"""
Chaos-Test 2: Zwei parallele Daemon-Instanzen → Process-Lock.

Nur eine Instanz darf die Lock-Datei halten.
Zweite Instanz muss sauber scheitern.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_double_cron_only_one_wins():
    """Zwei Threads versuchen ProcessLock — nur einer gewinnt."""
    from core.process_lock import ProcessLock

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, "test_daemon.lock")
        hb_path   = os.path.join(tmpdir, "test_daemon.hb")

        winners = []
        errors  = []

        def try_lock(idx: int):
            lock = ProcessLock("test_daemon")
            lock._lock_path = type(lock._lock_path)(lock_path)
            lock._hb_path   = type(lock._hb_path)(hb_path)
            try:
                ok = lock.acquire()
                if ok:
                    winners.append(idx)
                    time.sleep(0.2)
                    lock.release()
                else:
                    errors.append(idx)
            except SystemExit:
                errors.append(idx)
            except Exception as e:
                errors.append(f"{idx}:{e}")

        t1 = threading.Thread(target=try_lock, args=(1,))
        t2 = threading.Thread(target=try_lock, args=(2,))

        t1.start()
        time.sleep(0.05)  # t1 gewinnt zuerst
        t2.start()

        t1.join(timeout=2)
        t2.join(timeout=2)

    assert len(winners) == 1, f"Nur ein Winner erlaubt, aber: {winners}"
    assert len(errors) == 1, f"Genau ein Verlierer erwartet, aber: {errors}"


def test_process_lock_releases_on_exit():
    """Nach release() kann die Lock-Datei neu belegt werden."""
    from core.process_lock import ProcessLock
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        lock1 = ProcessLock("daemon2_rel")
        lock1._lock_path = Path(tmpdir) / "daemon2.lock"
        lock1._hb_path   = Path(tmpdir) / "daemon2.hb"
        ok1 = lock1.acquire()
        assert ok1
        lock1.release()

        lock2 = ProcessLock("daemon2_rel")
        lock2._lock_path = Path(tmpdir) / "daemon2.lock"
        lock2._hb_path   = Path(tmpdir) / "daemon2.hb"
        ok2 = lock2.acquire()
        assert ok2, "Nach release() muss zweiter Lock gewinnen"
        lock2.release()


def test_heartbeat_written_on_acquire():
    """Nach acquire() existiert die Heartbeat-Datei."""
    from core.process_lock import ProcessLock
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        lock = ProcessLock("daemon3_hb")
        lock._lock_path = Path(tmpdir) / "daemon3.lock"
        lock._hb_path   = Path(tmpdir) / "daemon3.hb"
        lock.acquire()
        assert lock._hb_path.exists(), "Heartbeat-Datei muss nach acquire() existieren"
        lock.release()
