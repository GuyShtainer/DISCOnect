"""Snapshots of the store, with a manifest and an integrity check.

A backup is taken through SQLite's online backup API, never by copying the live
file and its WAL, so it is consistent even while an import runs. Every
snapshot gets a manifest (creation time, versions, per-table row counts, size,
SHA-256) and is verified with ``PRAGMA integrity_check`` immediately; a snapshot
that fails verification is deleted rather than left looking usable. Restore
verifies the snapshot again, keeps the current database as a rollback point,
and refuses a snapshot written by a newer schema.

An encrypted store makes encrypted snapshots (keyed-to-keyed backup; the key
file is copied beside each snapshot so "the words + any copy" recovers it), and
the manifest stays plaintext metadata: counts, sizes, the note. Restoring a
plaintext snapshot into an encrypted store converts it on the way in.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import pathlib
import shutil

from disconect import __version__, contract, identity
from disconect.storage import _time, keys, migrations
from disconect.storage.write_lock import write_lock

MANIFEST_SUFFIX = ".manifest.json"
TABLES = ("raw_records", "metric_samples", "daily_metrics", "daily_labels", "sleep_sessions",
          "sleep_stages", "monitoring_intervals", "activities", "clock_offsets", "import_runs")


class BackupError(Exception):
    """A snapshot could not be made, verified, or restored."""


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity_ok(path: pathlib.Path, master: bytes | None) -> bool:
    from disconect import storage  # storage imports this module
    try:
        conn = storage.connect(path, read_only=True, master=master)
    except storage.StorageError:
        return False
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        return master is None or not conn.execute("PRAGMA cipher_integrity_check").fetchall()
    except storage.DatabaseError:
        return False
    finally:
        conn.close()


def _master_for(db_path: pathlib.Path) -> bytes | None:
    from disconect import storage
    return storage.master_key_for(db_path, allow_prompt=True)


def _manifest_for(path: pathlib.Path, master: bytes | None, note: str | None) -> dict:
    from disconect import storage
    conn = storage.connect(path, read_only=True, master=master)
    try:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
        schema_version = migrations.current_version(conn)
    finally:
        conn.close()
    return {
        "file": path.name, "created_at": _time.utc_now_iso(), "app_version": __version__,
        "schema_version": schema_version, "contract_version": contract.CONTRACT_VERSION,
        "encrypted": master is not None, "tables": counts, "bytes": path.stat().st_size,
        "sha256": _sha256(path), "note": note,
    }


def refresh_manifest(snapshot: pathlib.Path, master: bytes | None) -> dict:
    """Rewrite a snapshot's manifest after its bytes changed (conversion to ciphertext)."""
    manifest_path = snapshot.with_name(snapshot.name + MANIFEST_SUFFIX)
    note = None
    if manifest_path.exists():
        with contextlib.suppress(ValueError):
            note = json.loads(manifest_path.read_text()).get("note")
    manifest = _manifest_for(snapshot, master, note)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def snapshot_files(dest_dir: pathlib.Path, suffix: str = "") -> list[pathlib.Path]:
    """Snapshots in ``dest_dir`` under the current and every earlier prefix, oldest first by stamp.

    ``suffix`` selects a companion (``.plaintext-rollback``); the default selects the snapshots.
    Earlier prefixes are read forever: a plaintext snapshot an old build wrote must still be found
    to be encrypted, rotated or listed.
    """
    dest_dir = pathlib.Path(dest_dir)
    prefixes = [identity.BACKUP_PREFIX, *identity.LEGACY_BACKUP_PREFIXES]
    found = {path for prefix in prefixes for path in dest_dir.glob(f"{prefix}-*.db{suffix}")}
    return sorted(found, key=lambda path: (path.name.split("-", 1)[1], path.name))


def default_backup_dir(db_path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(db_path).parent / "backups"


def create_backup(db_path: pathlib.Path, dest_dir: pathlib.Path | None = None,
                  note: str | None = None) -> dict:
    """Snapshot ``db_path`` into ``dest_dir`` and return the manifest (also written beside it).

    Raises :class:`BackupError` if the source is missing or the snapshot fails
    its integrity check (the half-made file is removed).
    """
    db_path = pathlib.Path(db_path)
    if not db_path.exists():
        raise BackupError("no database to back up")
    dest_dir = pathlib.Path(dest_dir) if dest_dir else default_backup_dir(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(_time.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"{identity.BACKUP_PREFIX}-{stamp}.db"
    if target.exists():
        raise BackupError(f"a snapshot named {target.name} already exists; wait a second and retry")
    from disconect import storage
    master = _master_for(db_path)
    source = storage.connect(db_path, read_only=True, master=master)
    destination = storage.connect(target, read_only=False, master=master)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not _integrity_ok(target, master):
        target.unlink(missing_ok=True)
        raise BackupError("snapshot failed its integrity check and was removed")
    manifest = _manifest_for(target, master, note)
    target.with_name(target.name + MANIFEST_SUFFIX).write_text(json.dumps(manifest, indent=2) + "\n")
    if master is not None:
        shutil.copy2(keys.key_path_for(db_path), target.with_name(target.name + keys.KEY_FILE_SUFFIX))
    return manifest


def list_backups(dest_dir: pathlib.Path) -> list[dict]:
    """Manifests in ``dest_dir``, newest first; snapshots without a manifest are listed as such."""
    dest_dir = pathlib.Path(dest_dir)
    if not dest_dir.is_dir():
        return []
    found = []
    for path in reversed(snapshot_files(dest_dir)):
        manifest_path = path.with_name(path.name + MANIFEST_SUFFIX)
        if manifest_path.exists():
            with contextlib.suppress(ValueError):
                found.append(json.loads(manifest_path.read_text()))
                continue
        found.append({"file": path.name, "bytes": path.stat().st_size, "manifest": "missing"})
    return found


def verify_backup(backup_path: pathlib.Path, master: bytes | None = None) -> dict:
    """Check hash against the manifest and run the integrity check; raise BackupError on any failure.

    ``master`` keys an encrypted snapshot; a plaintext snapshot is checked without it.
    """
    from disconect import storage
    backup_path = pathlib.Path(backup_path)
    manifest_path = backup_path.with_name(backup_path.name + MANIFEST_SUFFIX)
    if not backup_path.exists() or not manifest_path.exists():
        raise BackupError("snapshot or its manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    if _sha256(backup_path) != manifest.get("sha256"):
        raise BackupError("snapshot bytes do not match the manifest hash")
    encrypted = storage.is_encrypted_file(backup_path)
    if encrypted and master is None:
        raise BackupError("snapshot is encrypted and no key is available (run 'disconect key cache' or "
                          "set the passphrase)")
    if not _integrity_ok(backup_path, master if encrypted else None):
        raise BackupError("snapshot fails SQLite's integrity check")
    if int(manifest.get("schema_version", 0)) > migrations.SCHEMA_VERSION:
        raise BackupError(f"snapshot was written by a newer schema; upgrade {identity.PRODUCT} first")
    return manifest


def restore_backup(backup_path: pathlib.Path, db_path: pathlib.Path) -> dict:
    """Replace ``db_path`` with a verified snapshot, keeping the old file as a rollback point.

    Holds the write lock throughout. The previous database (with its WAL and
    SHM sidecars) is moved to ``<db>.pre-restore-<stamp>`` first; if the
    restored file fails its integrity check the old one is put back.
    """
    from disconect import storage
    from disconect.storage import encrypt
    backup_path, db_path = pathlib.Path(backup_path), pathlib.Path(db_path)
    master = _master_for(db_path)
    manifest = verify_backup(backup_path, master)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(_time.UTC).strftime("%Y%m%dT%H%M%SZ")
    rollback = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}")
    converted = False
    with write_lock(db_path, "restore"):
        if db_path.exists():
            shutil.move(db_path, rollback)
        for sidecar in ("-wal", "-shm"):  # move the sidecars with the rollback copy: un-checkpointed frames belong to it
            side = pathlib.Path(str(db_path) + sidecar)
            if side.exists():
                shutil.move(side, pathlib.Path(str(rollback) + sidecar))
        shutil.copy2(backup_path, db_path)
        if master is not None and storage.is_encrypted_file(db_path) is False:
            encrypt.encrypt_file(db_path, master, replace=True)   # a plaintext snapshot into an encrypted store
            pathlib.Path(str(db_path) + encrypt.ROLLBACK_SUFFIX).unlink(missing_ok=True)
            converted = True
        if not _integrity_ok(db_path, master):
            db_path.unlink(missing_ok=True)
            if rollback.exists():
                shutil.move(rollback, db_path)
            raise BackupError("restored file failed its integrity check; previous database put back")
    return {"restored_from": backup_path.name, "rollback_copy": rollback.name if rollback.exists() else None,
            "converted_to_ciphertext": converted, "manifest": manifest}
