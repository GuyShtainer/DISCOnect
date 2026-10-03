"""Snapshots: consistent, verified, restorable, and honest about failure."""

import json

import pytest

from disconect import storage
from disconect.storage import backup


def _seed(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) "
                     "VALUES('2025-06-15','steps',8000,'local')")


def test_backup_manifest_and_verify(db_path, tmp_path):
    _seed(db_path)
    manifest = backup.create_backup(db_path, tmp_path / "snaps", note="first")
    snap = tmp_path / "snaps" / manifest["file"]
    assert snap.exists() and manifest["tables"]["daily_metrics"] == 1
    assert manifest["schema_version"] >= 1 and manifest["note"] == "first"
    assert json.loads(snap.with_name(snap.name + ".manifest.json").read_text()) == manifest
    assert backup.verify_backup(snap) == manifest
    listed = backup.list_backups(tmp_path / "snaps")
    assert [m["file"] for m in listed] == [manifest["file"]]
    assert backup.list_backups(tmp_path / "nowhere") == []


def test_tampered_snapshot_is_refused(db_path, tmp_path):
    _seed(db_path)
    manifest = backup.create_backup(db_path, tmp_path / "snaps")
    snap = tmp_path / "snaps" / manifest["file"]
    data = bytearray(snap.read_bytes())
    data[-1] ^= 0xFF
    snap.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="hash"):
        backup.verify_backup(snap)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(snap, db_path)


def test_restore_replaces_database_and_keeps_rollback(db_path, tmp_path):
    _seed(db_path)
    manifest = backup.create_backup(db_path, tmp_path / "snaps")
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("UPDATE daily_metrics SET value = 1")
    result = backup.restore_backup(tmp_path / "snaps" / manifest["file"], db_path)
    conn = storage.open_read_only(db_path)
    assert conn.execute("SELECT value FROM daily_metrics").fetchone()[0] == 8000
    conn.close()
    assert result["rollback_copy"] and (db_path.parent / result["rollback_copy"]).exists()


def test_missing_database_cannot_be_backed_up(db_path, tmp_path):
    with pytest.raises(backup.BackupError):
        backup.create_backup(db_path, tmp_path / "snaps")
