"""
A.1 — Tägliches Hot-Backup für Live-DB und Lab-State-DB.

Nutzt sqlite3.Connection.backup() (WAL-sicher, kein Locking).
Retention: 7 tägliche + 4 wöchentliche Backups in data/backups/.

Aufruf: python3 scripts/db_backup.py
        python3 scripts/db_backup.py --dry-run  (nur prüfen, kein Schreiben)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.utils import log

ROOT         = Path(__file__).parent.parent
BACKUP_DIR   = ROOT / "data" / "backups"
DAILY_RETAIN = 7
WEEKLY_RETAIN = 4

# DB-Namen ohne Dateiendung — verhindert, dass der Pfad als Pattern abgefangen wird
_DB_NAMES = ["apex_v2", "lab_state"]


def _db_path(name: str) -> Path:
    return ROOT / "data" / f"{name}.db"


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _is_monday() -> bool:
    return datetime.now(timezone.utc).weekday() == 0


def backup_db(name: str, dry_run: bool = False) -> Path | None:
    src = _db_path(name)
    if not src.exists():
        log(f"[db-backup] {name}: Quelldatei nicht gefunden — übersprungen")
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_path  = BACKUP_DIR / f"{name}_{today}_daily.db"
    weekly_path = BACKUP_DIR / f"{name}_{today}_weekly.db"

    if dry_run:
        log(f"[db-backup] DRY-RUN: {name} → {daily_path.name}")
        return daily_path

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(daily_path))
        src_conn.backup(dst_conn)
        dst_conn.close()
        log(f"[db-backup] {name}: daily → {daily_path.name}")
    finally:
        src_conn.close()

    if _is_monday():
        import shutil
        shutil.copy2(str(daily_path), str(weekly_path))
        log(f"[db-backup] {name}: weekly → {weekly_path.name}")

    return daily_path


def prune_backups(name: str, dry_run: bool = False) -> None:
    for suffix, retain in [("daily", DAILY_RETAIN), ("weekly", WEEKLY_RETAIN)]:
        files = sorted(BACKUP_DIR.glob(f"{name}_*_{suffix}.db"))
        for f in files[:-retain] if len(files) > retain else []:
            if dry_run:
                log(f"[db-backup] DRY-RUN: würde löschen {f.name}")
            else:
                f.unlink()
                log(f"[db-backup] Pruned: {f.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="APEX V2 DB-Backup")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ok = True
    for name in _DB_NAMES:
        result = backup_db(name, dry_run=args.dry_run)
        if result is None:
            ok = False
        else:
            prune_backups(name, dry_run=args.dry_run)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
