"""Cross-process single-writer lock, held by the operating system.

The CLI, a future scheduler and a future UI may all want to write the same
database. An in-process mutex cannot stop a second process, and two imports
running at once would at best duplicate work and at worst interrupt a
migration halfway. Every writing action takes this lock first; read-only
connections never do, or every MCP query would stall during a long import.

The lock is an ``flock`` on a sidecar file, so the kernel releases it when the
holder exits or crashes. There is no stale-lockfile failure mode that needs a
human to delete a file. (POSIX only for now; Windows needs an exclusive-share
open instead.)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import time
from collections.abc import Iterator

from disconect import identity
from disconect.storage import _time, home

POLL_INTERVAL_S = 0.12


class WriteLockBusy(Exception):
    """Another process holds the write lock. ``holder`` is its self-reported purpose."""

    def __init__(self, holder: dict | None):
        self.holder = holder
        what = f" ({holder.get('purpose')}, pid {holder.get('pid')})" if holder else ""
        super().__init__(f"another {identity.PRODUCT} process is writing the database{what}")


def lock_path_for(db_path: pathlib.Path) -> pathlib.Path:
    """Sidecar lock file next to the database, named after the store's path with symlinks resolved: a symlinked
    store *name* then gets the same lock file as the name it points to (review: the Rust core resolves
    the same way). A dangling link is followed too (SQLite follows it and creates the target); a store that does
    not exist yet and is no link is taken as spelled; a symlinked folder needs no resolving (both spellings open
    the one lock file); a hard link cannot be resolved by path."""
    db_path = pathlib.Path(db_path)
    try:
        resolved = db_path.resolve()          # non-strict: a missing tail is kept, a dangling link followed
    except (OSError, RuntimeError):           # 3.12 raises RuntimeError on a symlink loop
        resolved = db_path
    return resolved.with_name(resolved.name + ".write-lock")


def _read_holder(holder_path: pathlib.Path) -> dict | None:
    try:
        return json.loads(holder_path.read_text())
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def write_lock(db_path: pathlib.Path, purpose: str, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold the exclusive write lock for ``db_path`` while the block runs.

    Waits up to ``timeout_s`` for a current holder to finish, then raises
    :class:`WriteLockBusy`. ``purpose`` is written to a holder file purely so a
    waiting process can tell the user who is writing.
    """
    lock_path = lock_path_for(db_path)
    holder_path = lock_path.with_name(lock_path.name + ".holder")
    home.ensure_parent_dir(lock_path)
    deadline = time.monotonic() + timeout_s
    handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - closed in finally
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriteLockBusy(_read_holder(holder_path)) from None
                time.sleep(POLL_INTERVAL_S)
        holder_path.write_text(json.dumps(
            {"purpose": purpose, "pid": os.getpid(), "since": _time.utc_now_iso()}))
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                holder_path.unlink()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
