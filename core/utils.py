import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def now_iso() -> str:
    return now_utc().isoformat()


class TimestampedWriter:
    def __init__(self, stream):
        self._stream = stream

    def write(self, msg):
        if msg.strip():
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            self._stream.write(f"[{ts}] {msg}")
        else:
            self._stream.write(msg)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def enable_timestamped_logging():
    sys.stdout = TimestampedWriter(sys.stdout)
    sys.stderr = TimestampedWriter(sys.stderr)


def log(msg: str):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
