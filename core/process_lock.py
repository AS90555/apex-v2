"""
Process-Lock für APEX-V2-Daemons.

Verhindert, dass ein Daemon mehrfach gleichzeitig läuft.
Verwendet fcntl.flock() auf eine Lockdatei in data/locks/.
Schreibt zusätzlich eine Heartbeat-Datei in data/heartbeats/ (für Dead Man's Switch in Phase 5).

Verwendung:
    with ProcessLock("run_governance"):
        ...  # nur ein Prozess gleichzeitig
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_BASE = Path(__file__).parent.parent
_LOCK_DIR = _BASE / "data" / "locks"
_HB_DIR   = _BASE / "data" / "heartbeats"


def _ensure_dirs() -> None:
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    _HB_DIR.mkdir(parents=True, exist_ok=True)


class ProcessLock:
    """
    Context-Manager: exklusiver Datei-Lock für einen benannten Daemon.

    Bei doppeltem Start: sys.exit(1) mit klarer Meldung.
    Schreibt beim Betreten und bei jedem heartbeat()-Aufruf die Heartbeat-Datei.
    """

    def __init__(self, component: str, timeout_seconds: float = 5.0) -> None:
        _ensure_dirs()
        self.component = component
        self.timeout   = timeout_seconds
        self._lock_path = _LOCK_DIR / f"{component}.lock"
        self._hb_path   = _HB_DIR   / f"{component}.hb"
        self._lock_fd: Optional[int] = None

    def acquire(self) -> bool:
        self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._lock_fd, str(os.getpid()).encode())
            self._write_heartbeat()
            return True
        except BlockingIOError:
            os.close(self._lock_fd)
            self._lock_fd = None
            return False

    def release(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def heartbeat(self) -> None:
        """Aktualisiert die Heartbeat-Datei (periodisch im Daemon-Loop aufrufen)."""
        self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._hb_path.write_text(f"{now}\n{self.component}\n{os.getpid()}\n")

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            print(
                f"[PROCESS-LOCK] {self.component} läuft bereits (Lock: {self._lock_path}). "
                f"Zweiter Start abgebrochen.",
                file=sys.stderr,
            )
            sys.exit(1)
        return self

    def __exit__(self, *_) -> None:
        self.release()


def read_heartbeat(component: str) -> Optional[datetime]:
    """Gibt den Zeitstempel des letzten Heartbeats zurück, oder None wenn nicht vorhanden."""
    _ensure_dirs()
    hb_path = _HB_DIR / f"{component}.hb"
    if not hb_path.exists():
        return None
    try:
        first_line = hb_path.read_text().splitlines()[0]
        return datetime.fromisoformat(first_line)
    except Exception:
        return None


def is_stale(component: str, max_age_seconds: float) -> bool:
    """True wenn kein Heartbeat vorhanden oder älter als max_age_seconds."""
    ts = read_heartbeat(component)
    if ts is None:
        return True
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > max_age_seconds
