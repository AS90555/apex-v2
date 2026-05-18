"""
P1.1 — DB-Backup-Retention-Test.

Verifiziert:
- Tägliche Backups werden nach 7 Files beschnitten (älteste zuerst)
- Wöchentliche Backups werden nach 4 Files beschnitten
- Fremde/Legacy-Dateien im Backup-Verzeichnis bleiben unangetastet
- backup_db() erzeugt File und gibt Pfad zurück
- backup_db() gibt None zurück wenn Quelldatei fehlt
- Dry-Run schreibt keine Files
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import scripts.db_backup as backup_mod


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    """Temporäres Backup-Verzeichnis für isolierte Tests."""
    bd = tmp_path / "backups"
    bd.mkdir()
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", bd)
    return bd


@pytest.fixture()
def fake_db(tmp_path):
    """Minimale SQLite-DB als Backup-Quelle."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db_path


def _make_daily_files(backup_dir: Path, name: str, count: int) -> list[Path]:
    """Erzeugt `count` künstliche Daily-Backup-Files mit aufsteigendem Datum."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    files = []
    for i in range(count):
        day = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        f = backup_dir / f"{name}_{day}_daily.db"
        f.touch()
        files.append(f)
    return files


def _make_weekly_files(backup_dir: Path, name: str, count: int) -> list[Path]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    files = []
    for i in range(count):
        day = (base + timedelta(weeks=i)).strftime("%Y-%m-%d")
        f = backup_dir / f"{name}_{day}_weekly.db"
        f.touch()
        files.append(f)
    return files


class TestRetention:
    def test_daily_pruned_to_7(self, backup_dir):
        _make_daily_files(backup_dir, "apex_v2", 10)
        backup_mod.prune_backups("apex_v2")
        remaining = sorted(backup_dir.glob("apex_v2_*_daily.db"))
        assert len(remaining) == backup_mod.DAILY_RETAIN

    def test_daily_keeps_newest(self, backup_dir):
        files = _make_daily_files(backup_dir, "apex_v2", 10)
        backup_mod.prune_backups("apex_v2")
        remaining = {f.name for f in backup_dir.glob("apex_v2_*_daily.db")}
        # Die 7 neuesten sollen erhalten bleiben
        expected = {f.name for f in files[-7:]}
        assert remaining == expected

    def test_weekly_pruned_to_4(self, backup_dir):
        _make_weekly_files(backup_dir, "apex_v2", 8)
        backup_mod.prune_backups("apex_v2")
        remaining = sorted(backup_dir.glob("apex_v2_*_weekly.db"))
        assert len(remaining) == backup_mod.WEEKLY_RETAIN

    def test_below_limit_unchanged(self, backup_dir):
        _make_daily_files(backup_dir, "apex_v2", 5)
        backup_mod.prune_backups("apex_v2")
        remaining = sorted(backup_dir.glob("apex_v2_*_daily.db"))
        assert len(remaining) == 5

    def test_legacy_files_untouched(self, backup_dir):
        """Dateien mit anderen Namensmustern bleiben unangetastet."""
        legacy = backup_dir / "apex_v2_20260506_205544.db"
        legacy.touch()
        pre_v6 = backup_dir / "apex_v2_2026-05-07_pre-autopromo.db"
        pre_v6.touch()
        _make_daily_files(backup_dir, "apex_v2", 10)
        backup_mod.prune_backups("apex_v2")
        assert legacy.exists(), "Legacy-File wurde irrtümlich gelöscht"
        assert pre_v6.exists(), "pre-autopromo-File wurde irrtümlich gelöscht"

    def test_dry_run_does_not_delete(self, backup_dir):
        _make_daily_files(backup_dir, "apex_v2", 10)
        backup_mod.prune_backups("apex_v2", dry_run=True)
        remaining = list(backup_dir.glob("apex_v2_*_daily.db"))
        assert len(remaining) == 10

    def test_lab_state_pruned_independently(self, backup_dir):
        _make_daily_files(backup_dir, "apex_v2", 10)
        _make_daily_files(backup_dir, "lab_state", 10)
        backup_mod.prune_backups("apex_v2")
        backup_mod.prune_backups("lab_state")
        assert len(list(backup_dir.glob("apex_v2_*_daily.db"))) == backup_mod.DAILY_RETAIN
        assert len(list(backup_dir.glob("lab_state_*_daily.db"))) == backup_mod.DAILY_RETAIN


class TestBackupDb:
    def test_backup_creates_file(self, backup_dir, fake_db, monkeypatch):
        monkeypatch.setattr(backup_mod, "_db_path", lambda name: fake_db)
        result = backup_mod.backup_db("test_db")
        assert result is not None
        assert result.exists()
        # Backup ist lesbare SQLite-DB
        conn = sqlite3.connect(str(result))
        conn.execute("SELECT * FROM t")
        conn.close()

    def test_backup_missing_source_returns_none(self, backup_dir, tmp_path, monkeypatch):
        missing = tmp_path / "nonexistent.db"
        monkeypatch.setattr(backup_mod, "_db_path", lambda name: missing)
        result = backup_mod.backup_db("ghost_db")
        assert result is None

    def test_dry_run_returns_path_without_creating(self, backup_dir, fake_db, monkeypatch):
        monkeypatch.setattr(backup_mod, "_db_path", lambda name: fake_db)
        result = backup_mod.backup_db("test_db", dry_run=True)
        assert result is not None
        # Im Dry-Run wird keine Datei erzeugt
        assert not result.exists()

    def test_backup_content_matches_source(self, backup_dir, fake_db, monkeypatch):
        """Backup-DB enthält die gleichen Daten wie die Quelle."""
        src_conn = sqlite3.connect(str(fake_db))
        src_conn.execute("INSERT INTO t VALUES (42)")
        src_conn.commit()
        src_conn.close()
        monkeypatch.setattr(backup_mod, "_db_path", lambda name: fake_db)
        result = backup_mod.backup_db("test_db")
        conn = sqlite3.connect(str(result))
        row = conn.execute("SELECT id FROM t").fetchone()
        conn.close()
        assert row[0] == 42
