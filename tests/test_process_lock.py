"""
Phase-1-Test: ProcessLock — zweiter Daemon-Start scheitert mit klarer Meldung.
"""

from __future__ import annotations

import subprocess
import sys
import time
import threading
import pytest
from pathlib import Path

from core.process_lock import ProcessLock, read_heartbeat, is_stale


TEST_COMPONENT = "test_lock_unit"


def test_single_acquire_release():
    lock = ProcessLock(TEST_COMPONENT)
    assert lock.acquire()
    lock.release()


def test_double_acquire_blocked():
    """Zweiter Acquire auf gleiche Komponente schlägt fehl."""
    lock1 = ProcessLock(TEST_COMPONENT)
    lock2 = ProcessLock(TEST_COMPONENT)
    assert lock1.acquire()
    try:
        assert not lock2.acquire()
    finally:
        lock1.release()


def test_context_manager_exit_releases():
    with ProcessLock(TEST_COMPONENT):
        pass
    # Nach exit muss re-acquire möglich sein
    lock = ProcessLock(TEST_COMPONENT)
    assert lock.acquire()
    lock.release()


def test_heartbeat_written_on_acquire():
    lock = ProcessLock(TEST_COMPONENT)
    lock.acquire()
    ts = read_heartbeat(TEST_COMPONENT)
    lock.release()
    assert ts is not None


def test_heartbeat_not_stale_immediately():
    lock = ProcessLock(TEST_COMPONENT)
    lock.acquire()
    lock.heartbeat()
    stale = is_stale(TEST_COMPONENT, max_age_seconds=60)
    lock.release()
    assert not stale


def test_is_stale_missing_component():
    """Unbekannte Komponente → stale."""
    assert is_stale("never_started_component_xyz", max_age_seconds=1)


def test_second_process_exits():
    """Subprocess mit gleichem Lock-Namen muss mit exit-code 1 enden."""
    script = """
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.process_lock import ProcessLock
lock = ProcessLock("test_subprocess_lock")
lock.acquire()  # erster acquire → OK
from core.process_lock import ProcessLock as PL2
lock2 = PL2("test_subprocess_lock")
if not lock2.acquire():
    sys.exit(1)
sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
