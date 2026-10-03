"""Converting a plaintext store to SQLCipher in place, and purging what plaintext remains.

The conversion never leaves a moment with no database at the path: the
encrypted copy is built beside it, checked under its key, the plaintext is
hard-linked to a rollback name, and one ``os.replace`` swaps the files. What
cannot be purged is said out loud: Time Machine and APFS snapshots keep their
copies, and unlinking on an SSD does not erase blocks (FileVault is the
mitigation for both).
"""

from __future__ import annotations

import os
import pathlib

from disconect.storage import keys, migrations
from disconect.storage.write_lock import write_lock

ROLLBACK_SUFFIX = ".plaintext-rollback"
IN_PROGRESS_SUFFIX = ".encrypting"
PLAINTEXT_WARNING = ("plaintext copies may remain in Time Machine / APFS snapshots and in freed SSD blocks; "
                     "FileVault is the protection for those")


class EncryptError(Exception):
    pass


def _fsync_dir(path: pathlib.Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _integrity_under_key(path: pathlib.Path, master: bytes) -> None:
    from disconect import storage
    conn = storage.connect(path, read_only=True, master=master)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise EncryptError("encrypted copy failed integrity_check")
        bad = conn.execute("PRAGMA cipher_integrity_check").fetchall()
        if bad:
            raise EncryptError(f"encrypted copy failed cipher_integrity_check ({len(bad)} pages)")
    finally:
        conn.close()


def encrypt_file(source: pathlib.Path, master: bytes, *, replace: bool) -> pathlib.Path:
    """Write an encrypted copy of plaintext ``source`` (``user_version`` carried over, which
    ``sqlcipher_export`` drops). With ``replace`` the copy takes the source's place and the
    plaintext becomes ``<source>.plaintext-rollback``; otherwise the ``.encrypting`` path is returned."""
    from disconect import storage
    source = pathlib.Path(source)
    target = source.with_name(source.name + IN_PROGRESS_SUFFIX)
    target.unlink(missing_ok=True)
    plain = storage.sqlite.connect(str(source), isolation_level=None)
    try:
        if plain.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        version = migrations.current_version(plain)
        plain.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{keys.db_key_hex(master)}'\"", (str(target),))
        plain.execute("PRAGMA encrypted.cipher_compatibility = 4")
        plain.execute("SELECT sqlcipher_export('encrypted')")
        plain.execute(f"PRAGMA encrypted.user_version = {int(version)}")
        plain.execute("DETACH DATABASE encrypted")
    finally:
        plain.close()
    with target.open("rb+") as handle:
        os.fsync(handle.fileno())
    os.chmod(target, 0o600)
    _integrity_under_key(target, master)
    if not replace:
        return target
    rollback = source.with_name(source.name + ROLLBACK_SUFFIX)
    rollback.unlink(missing_ok=True)
    os.link(source, rollback)
    os.chmod(rollback, 0o600)
    os.replace(target, source)
    _fsync_dir(source.parent)
    for sidecar in ("-wal", "-shm"):
        pathlib.Path(str(source) + sidecar).unlink(missing_ok=True)
    return source


def encrypt_store(db_path: pathlib.Path, master: bytes) -> dict:
    """Convert the live database and every plaintext snapshot in its backups folder. Holds the write lock."""
    from disconect import storage
    from disconect.storage import backup
    db_path = pathlib.Path(db_path)
    if not db_path.exists():
        raise EncryptError("no database to encrypt")
    if storage.is_encrypted_file(db_path):
        raise EncryptError("database is already encrypted")
    converted = {"database": None, "snapshots": [], "plaintext_left": []}
    with write_lock(db_path, "encrypt"):
        encrypt_file(db_path, master, replace=True)
        converted["database"] = db_path.name
        converted["plaintext_left"].append(db_path.name + ROLLBACK_SUFFIX)
        for snapshot in backup.snapshot_files(backup.default_backup_dir(db_path)):
            if storage.is_encrypted_file(snapshot) is False:
                encrypt_file(snapshot, master, replace=True)
                backup.refresh_manifest(snapshot, master)
                converted["snapshots"].append(snapshot.name)
                converted["plaintext_left"].append(snapshot.name + ROLLBACK_SUFFIX)
        for stray in db_path.parent.glob(db_path.name + ".pre-restore-*"):
            if storage.is_encrypted_file(stray) is False:
                converted["plaintext_left"].append(stray.name)
    converted["warning"] = PLAINTEXT_WARNING
    return converted


def purge_plaintext(db_path: pathlib.Path) -> dict:
    """Delete the plaintext left behind by ``encrypt``: rollback copies, plaintext pre-restore copies,
    and replaced key files. Refuses unless the store is actually encrypted (so it can never delete the
    only copy of plaintext data), and never touches a snapshot that was not converted."""
    from disconect import storage
    from disconect.storage import backup
    db_path = pathlib.Path(db_path)
    if not keys.key_path_for(db_path).exists() or storage.is_encrypted_file(db_path) is not True:
        raise EncryptError("refusing to purge: the database is not encrypted (run 'disconect encrypt' first)")
    removed = []
    with write_lock(db_path, "purge-plaintext"):
        candidates = list(db_path.parent.glob(db_path.name + ROLLBACK_SUFFIX))
        candidates += backup.snapshot_files(backup.default_backup_dir(db_path), ROLLBACK_SUFFIX)
        candidates += [p for p in db_path.parent.glob(db_path.name + ".pre-restore-*")
                       if p.suffix not in ("-wal", "-shm") and storage.is_encrypted_file(p) is False]
        candidates += list(db_path.parent.glob(keys.key_path_for(db_path).name + ".replaced"))
        for path in candidates:
            if not path.is_file() or storage.is_encrypted_file(path) is True:
                continue
            path.unlink()
            removed.append(path.name)
            for sidecar in ("-wal", "-shm"):
                pathlib.Path(str(path) + sidecar).unlink(missing_ok=True)
    return {"removed": sorted(removed), "warning": PLAINTEXT_WARNING}


def cleanup_stray(db_path: pathlib.Path) -> None:
    """Delete a half-written ``.encrypting`` file left by a crash (safe: the swap never happened)."""
    pathlib.Path(str(db_path) + IN_PROGRESS_SUFFIX).unlink(missing_ok=True)


def rekey_store(db_path: pathlib.Path, old_master: bytes, new_master: bytes, new_document: dict) -> dict:
    """Move the database and every encrypted copy beside it to a new master key, safely.

    Order matters: (1) every target must open under the old key or nothing is touched;
    (2) the new key file is written as ``<keys>.next`` BEFORE the first rekey, so a crash
    mid-way leaves a key on disk that ``storage`` will find and promote; (3) ``PRAGMA rekey``
    on each target; (4) the ``.next`` file replaces the key file; (5) snapshot manifests and
    their key-file copies are refreshed. Encrypted ``.pre-restore-*`` copies are rekeyed too.
    Snapshots written elsewhere with ``--to`` are not seen and stay on the old key.
    """
    from disconect import storage
    from disconect.storage import backup
    db_path = pathlib.Path(db_path)
    key_path = keys.key_path_for(db_path)
    next_path = key_path.with_name(key_path.name + keys.NEXT_SUFFIX)
    snapshots = [p for p in backup.snapshot_files(backup.default_backup_dir(db_path))
                 if storage.is_encrypted_file(p)]
    copies = [p for p in sorted(db_path.parent.glob(db_path.name + ".pre-restore-*"))
              if p.suffix not in ("-wal", "-shm") and storage.is_encrypted_file(p)]
    targets = [db_path] + snapshots + copies
    with write_lock(db_path, "rotate-recovery"):
        for path in targets:                       # (1) pre-flight
            conn = storage.connect(path, read_only=True, master=old_master)
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except storage.DatabaseError as exc:
                raise EncryptError(f"{path.name} does not open with the current key; nothing was changed") from exc
            finally:
                conn.close()
        keys.write_key_file(next_path, new_document)   # (2)
        for path in targets:                           # (3)
            conn = storage.connect(path, read_only=False, master=old_master)
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                conn.execute(f"PRAGMA rekey = \"x'{keys.db_key_hex(new_master)}'\"")
            finally:
                conn.close()
            _integrity_under_key(path, new_master)
        os.replace(next_path, key_path)                # (4)
        _fsync_dir(key_path.parent)
        for snapshot in snapshots:                     # (5)
            backup.refresh_manifest(snapshot, new_master)
            import shutil
            shutil.copy2(key_path, snapshot.with_name(snapshot.name + keys.KEY_FILE_SUFFIX))
    return {"database": db_path.name, "snapshots": [p.name for p in snapshots], "copies": [p.name for p in copies]}
