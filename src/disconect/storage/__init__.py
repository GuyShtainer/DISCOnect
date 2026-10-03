"""Opening the store: one writer under an OS lock, any number of readers.

SQLite in WAL mode serves one writing process and concurrent read-only
processes, which is exactly the shape of "an import runs while an MCP server
answers questions". Read-only connections are opened ``mode=ro`` and
``query_only``, so a write is refused by SQLite itself, not by a branch here.

The driver is ``sqlcipher3`` for every connection, encrypted or not: its
exception classes are not ``sqlite3``'s, so one driver keeps every ``except``
honest (``DatabaseError`` below is the base class to catch). Encryption is
decided by the **key file**: when ``<db>.keys.json`` exists every open is keyed
and a plaintext file in its place is refused (no silent downgrade); a keyed file
without a key file reports exactly that instead of "file is not a database".
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import Iterator

from sqlcipher3 import dbapi2 as sqlite

from disconect import identity
from disconect.storage import home, keys, migrations
from disconect.storage.errors import HomeMoved, NotConfigured, StorageError
from disconect.storage._time import iso_utc, parse_iso_utc, utc_now_iso
from disconect.storage.write_lock import WriteLockBusy, write_lock

__all__ = [
    "DEFAULT_DB_ENV", "DatabaseError", "Encrypted", "HomeMoved", "NotConfigured", "NotEncrypted", "Row",
    "SchemaTooNew", "StorageError", "WriteLockBusy", "connect", "default_db_path", "is_encrypted_file",
    "is_unlocked", "iso_utc", "open_for_write", "open_read_only", "parse_iso_utc", "prime", "remember", "forget", "sqlite", "unlocked_master", "utc_now_iso",
]

DEFAULT_DB_ENV = "DISCONECT_DB"
BUSY_TIMEOUT_MS = 5000
DatabaseError = sqlite.Error
Row = sqlite.Row
SQLITE_MAGIC = b"SQLite format 3\x00"

#: Master keys unlocked in this process, by key-file path: the MCP server unlocks once at start.
_unlocked: dict[pathlib.Path, bytes] = {}


class SchemaTooNew(StorageError):
    """The file was written by a newer build; refuse rather than misread it."""


class Encrypted(StorageError):
    """The file is encrypted and cannot be unlocked (no key file, no unlock path, or a wrong key)."""


class NotEncrypted(StorageError):
    """A key file exists but the database is plaintext: refuse the downgrade; run ``disconect encrypt``."""


def default_db_path() -> pathlib.Path:
    """``$DISCONECT_DB``, else ``~/.disconect/disconect.db``, else the old folder's file while it exists.

    Pure: no side effects (see :mod:`disconect.storage.home`).
    """
    return home.resolve_default_db()[0]


def is_encrypted_file(path: pathlib.Path) -> bool | None:
    """True if the file on disk is not plaintext SQLite, False if it is, None if it is empty or absent."""
    path = pathlib.Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as handle:
        return handle.read(16) != SQLITE_MAGIC


def apply_key(conn: sqlite.Connection, master: bytes) -> None:
    """Key a connection with the raw SQLCipher key derived from ``master``. Hex string form only:
    a bound bytes parameter would be treated as a passphrase and run through PBKDF2."""
    conn.execute(f"PRAGMA key = \"x'{keys.db_key_hex(master)}'\"")
    conn.execute("PRAGMA cipher_compatibility = 4")


def master_key_for(db_path: pathlib.Path, *, allow_prompt: bool) -> bytes | None:
    """The unlocked master key for ``db_path``, or None when no key file exists (plaintext store).

    Raises :class:`Encrypted` when a key file exists but nothing unlocks it.
    """
    key_path = keys.key_path_for(db_path)
    if not key_path.exists():
        return None
    cached = _unlocked.get(key_path)
    if cached is not None:
        return cached
    try:
        master = keys.unlock(key_path, allow_prompt=allow_prompt)
    except keys.KeyError_ as exc:
        raise Encrypted(str(exc)) from exc
    _unlocked[key_path] = master
    return master


def prime(db_path: pathlib.Path, *, allow_prompt: bool = False) -> bool:
    """Unlock once for the life of this process (the MCP server calls this at startup).

    Returns True if the store is encrypted and now unlocked, False if it is plaintext.
    """
    return master_key_for(db_path, allow_prompt=allow_prompt) is not None


def remember(db_path: pathlib.Path, master: bytes) -> None:
    """Register a master key just created or recovered so later opens in this process need no unlock."""
    _unlocked[keys.key_path_for(db_path)] = master


def unlocked_master(db_path: pathlib.Path) -> bytes | None:
    """The master key already unlocked in this process for ``db_path``, or None. Never tries an unlock path."""
    return _unlocked.get(keys.key_path_for(db_path))


def is_unlocked(db_path: pathlib.Path) -> bool:
    """True when nothing needs unlocking (no key file) or the key was unlocked in this process."""
    return not keys.key_path_for(db_path).exists() or unlocked_master(db_path) is not None


def forget(db_path: pathlib.Path) -> None:
    _unlocked.pop(keys.key_path_for(db_path), None)


def connect(path: pathlib.Path, *, read_only: bool, master: bytes | None = None,
            allow_prompt: bool = True) -> sqlite.Connection:
    """Open ``path`` with the right key state; the caller sets pragmas and checks the schema.

    ``master`` overrides the key lookup (used while converting). A plaintext file next to a key
    file raises :class:`NotEncrypted`; an encrypted file with no key file raises :class:`Encrypted`.
    """
    path = pathlib.Path(path)
    if master is None:
        master = master_key_for(path, allow_prompt=allow_prompt)
    on_disk = is_encrypted_file(path)
    if master is None and on_disk:
        raise Encrypted(f"{path.name} is encrypted but no key file was found at "
                        f"{keys.key_path_for(path).name}")
    if master is not None and on_disk is False:
        raise NotEncrypted(f"{path.name} is plaintext but a key file exists; run '{identity.COMMAND} encrypt' "
                           "to convert it (or remove the key file if it is not yours)")
    if read_only:
        conn = sqlite.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                              timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        conn = sqlite.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    if master is not None:
        apply_key(conn, master)
    return conn


def _check_not_too_new(conn: sqlite.Connection) -> None:
    version = migrations.current_version(conn)
    if version > migrations.SCHEMA_VERSION:
        raise SchemaTooNew(
            f"database schema is version {version}; this build understands up to "
            f"{migrations.SCHEMA_VERSION}. Upgrade {identity.PRODUCT}.")


def _try_next_key(path: pathlib.Path, read_only: bool) -> sqlite.Connection | None:
    """An interrupted rotation leaves ``<keys>.next`` holding the key the database was moved to.
    If it opens the database, promote it to the key file and carry on."""
    key_path = keys.key_path_for(path)
    next_path = key_path.with_name(key_path.name + keys.NEXT_SUFFIX)
    if not next_path.exists():
        return None
    try:
        master = keys.unlock(next_path, allow_prompt=False)
    except keys.KeyError_:
        return None
    conn = connect(path, read_only=read_only, master=master)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except DatabaseError:
        conn.close()
        return None
    os.replace(next_path, key_path)
    _unlocked[key_path] = master
    import sys
    print("note: finished an interrupted key rotation; run 'disconect key rotate-recovery' again if any "
          "snapshot no longer opens", file=sys.stderr)
    return conn


def _first_touch(conn: sqlite.Connection, path: pathlib.Path, read_only: bool) -> sqlite.Connection:
    """The first statement on a keyed connection is where a wrong key surfaces; name it.
    Returns the connection to use (a replacement when a pending rotation key was the right one)."""
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except DatabaseError as exc:
        conn.close()
        if "not a database" in str(exc):
            replacement = _try_next_key(path, read_only)
            if replacement is not None:
                return replacement
            raise Encrypted(f"{path.name} could not be opened with this key (wrong key file or a damaged file)") from exc
        raise


@contextlib.contextmanager
def open_for_write(path: pathlib.Path, purpose: str, timeout_s: float = 10.0
                   ) -> Iterator[sqlite.Connection]:
    """Yield a writable connection, holding the cross-process write lock throughout.

    Creates the file and migrates the schema when needed. Raises
    :class:`WriteLockBusy` if another writer holds the lock past ``timeout_s``
    and :class:`SchemaTooNew` if the file is from a newer build. Raises :class:`HomeMoved` instead of
    re-creating an old data folder that ``migrate-home`` moved away.
    """
    path = pathlib.Path(path)
    home.ensure_parent_dir(path)
    with write_lock(path, purpose, timeout_s):
        conn = connect(path, read_only=False)
        try:
            conn = _first_touch(conn, path, read_only=False)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            _check_not_too_new(conn)
            migrations.migrate(conn)
            yield conn
        finally:
            conn.close()


def open_read_only(path: pathlib.Path, *, allow_prompt: bool = True) -> sqlite.Connection:
    """A connection that cannot write, for the MCP server and status queries.

    Raises :class:`NotConfigured` when the file does not exist (a read-only
    open must never create an empty database that then looks 'configured').
    ``allow_prompt=False`` (the MCP server) never asks for a passphrase.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise NotConfigured(f"no {identity.PRODUCT} database yet; run '{identity.COMMAND} import' first")
    conn = connect(path, read_only=True, allow_prompt=allow_prompt)
    conn = _first_touch(conn, path, read_only=True)
    conn.execute("PRAGMA query_only = 1")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = Row
    _check_not_too_new(conn)
    return conn
