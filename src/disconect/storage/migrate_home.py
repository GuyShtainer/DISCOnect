"""``disconect migrate-home``: move the old data folder to the new name, safely and only on request.

Nothing else ever moves data (``home.resolve_default_db`` just reads through the old folder). The
move is ordered so that every crash leaves a state a rerun finishes:

1. refuse unless it is safe: no old-name env vars, no database in both folders, the old store's write
   lock is ours (non-blocking ``flock``) and no other process has the database, ``-wal`` or ``-shm`` open
   (``lsof -t``; it works on encrypted stores too, which a ``PRAGMA`` probe cannot open without a key),
   and no program of the product is running at all (``pgrep``): an idle app or MCP server holds no file
   open between calls, yet would re-create the old folder as a new store on its next write;
2. holding that lock, checkpoint a non-empty ``-wal`` (opens the store, keyed when a key file exists)
   and assert it is empty: renaming a database whose committed pages sit in its ``-wal`` drops them;
3. rename every ``hearthbeat.db*`` sibling to ``disconect.db*`` inside the old folder (SQLite, the key
   file, the write lock and the rollback/pre-restore copies all find each other by the database name);
   ``backups/`` file names are left alone, ``disconect`` reads both prefixes;
4. rename the folder itself, **last**: that is the commit point. When ``~/.disconect`` already exists
   without a database (``sync --remember`` creates it for ``relay.json``) the entries are moved into it
   one by one and the empty old folder is removed last.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Callable

from disconect import identity
from disconect.storage.write_lock import WriteLockBusy, write_lock

HOLDER_SUFFIX = ".write-lock.holder"
LSOF_TIMEOUT_S = 20
#: Programs that may hold the old folder's path in memory: the app, both cores (also under their old
#: name), the MCP server and the Python serve entry point.
RUNNING_NAMES = ("disconect-app", "disconect-core", "hearthbeat-core", "disconect-mcp", "disconect-serve")
PGREP_TIMEOUT_S = 10
MAX_ANCESTORS = 64


class MigrateRefused(Exception):
    """A precondition failed; nothing was moved (or the move can be finished by rerunning)."""


def _legacy_env_set(environ: dict | os._Environ) -> list[str]:
    names = [name for name in environ if name.startswith(identity.LEGACY_ENV_PREFIX)]
    if environ.get(identity.ENV_PREFIX + "DB"):
        names.append(identity.ENV_PREFIX + "DB")
    return sorted(names)


def _is_sibling(name: str, base: str) -> bool:
    return name == base or name.startswith((base + ".", base + "-"))


def _renamed(name: str) -> str:
    """``hearthbeat.db-wal`` -> ``disconect.db-wal``: the database name swapped, the rest kept."""
    return identity.DB_FILENAME + name[len(identity.LEGACY_DB_FILENAME):]


def _listing(folder: pathlib.Path) -> list[str]:
    return sorted(entry.name for entry in folder.iterdir()) if folder.is_dir() else []


def _lsof_pids(paths: list[pathlib.Path]) -> list[str]:
    """Pids of other processes holding any of ``paths`` open. Refuses when that cannot be asked."""
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return []
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        done = subprocess.run([lsof, "-t", "--", *existing], capture_output=True, text=True,
                              timeout=LSOF_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrateRefused(f"cannot check whether another process has the database open ({exc}); "
                             "quit the app and Claude Desktop, then retry") from exc
    if done.returncode not in (0, 1):
        raise MigrateRefused(f"cannot check whether another process has the database open "
                             f"(lsof exit {done.returncode}); quit the app and Claude Desktop, then retry")
    return sorted(set(done.stdout.split()))


def _ancestors(pid: int) -> set[int]:
    """``pid`` and its parents up to launchd, so a wrapper that merely contains a name is never "running"."""
    chain = {pid}
    for _ in range(MAX_ANCESTORS):
        try:
            done = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=PGREP_TIMEOUT_S, check=False)
            pid = int(done.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            break
        if pid <= 1 or pid in chain:
            break
        chain.add(pid)
    return chain


def _is_alive(pid: int) -> bool:
    """False for a zombie (``Z``) or a process already exiting (``E``): it holds nothing and cannot write."""
    try:
        done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=PGREP_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    state = done.stdout.strip()
    return bool(state) and not (state.startswith("Z") or "E" in state)


def running_programs(own_pid: int | None = None) -> list[tuple[str, int]]:
    """``(name, pid)`` of every running program in :data:`RUNNING_NAMES`, this process and its parents excluded.

    Zombies and exiting processes do not count. ``pgrep -f`` matches the whole command line, so a bundle path
    (``.../disconect-app.app/Contents/MacOS/disconect-app``) and an interpreter running a console script
    (``python .../bin/disconect-serve``) both count; the name must be a whole path component or word.
    Refuses (raises :class:`MigrateRefused`) when ``pgrep`` cannot be asked.
    """
    skip = _ancestors(os.getpid() if own_pid is None else own_pid)
    found: list[tuple[str, int]] = []
    for name in RUNNING_NAMES:
        pattern = rf"(^|[ /]){re.escape(name)}( |$)"
        try:
            done = subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True, text=True,
                                  timeout=PGREP_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MigrateRefused(f"cannot check whether {name} is running ({exc}); "
                                 "quit the app and Claude Desktop, then retry") from exc
        if done.returncode not in (0, 1):
            raise MigrateRefused(f"cannot check whether {name} is running (pgrep exit {done.returncode}); "
                                 "quit the app and Claude Desktop, then retry")
        found += [(name, int(pid)) for pid in done.stdout.split()
                  if int(pid) not in skip and _is_alive(int(pid))]
    return sorted(found, key=lambda item: item[1])


def _checkpoint(db_file: pathlib.Path, wal_file: pathlib.Path) -> None:
    """Fold a non-empty ``-wal`` into the database and require it empty afterwards."""
    if wal_file.exists() and wal_file.stat().st_size > 0:
        from disconect import storage

        conn = storage.connect(db_file, read_only=False)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    if wal_file.exists() and wal_file.stat().st_size > 0:
        raise MigrateRefused(f"{wal_file.name} still holds {wal_file.stat().st_size} bytes after a "
                             "checkpoint; nothing was moved")


def _plan(legacy_dir: pathlib.Path) -> dict[str, str]:
    """Old entry name -> name it ends up with (holder files are dropped, not moved)."""
    return {entry.name: (_renamed(entry.name) if _is_sibling(entry.name, identity.LEGACY_DB_FILENAME)
                         else entry.name)
            for entry in legacy_dir.iterdir() if not entry.name.endswith(HOLDER_SUFFIX)}


def _clashes(plan: dict[str, str], new_dir: pathlib.Path) -> list[str]:
    targets = list(plan.values())
    clashes = {name for name in targets if targets.count(name) > 1}
    if new_dir.is_dir():
        clashes |= {name for name in targets if (new_dir / name).exists()}
    return sorted(clashes)


def _drop_holders(folder: pathlib.Path) -> None:
    for base in (identity.LEGACY_DB_FILENAME, identity.DB_FILENAME):
        (folder / (base + HOLDER_SUFFIX)).unlink(missing_ok=True)


def _remove_legacy(legacy_dir: pathlib.Path) -> list[str]:
    """Remove the emptied old folder; when it is a symlink, remove the link and leave its target alone.

    A target that still has entries is left as it is (the entries are not ours to delete) and the link
    stays too, so nothing dangles. Returns the names left behind (empty when the folder is gone).
    """
    if legacy_dir.is_symlink():
        left = _listing(legacy_dir)
        if not left:
            legacy_dir.unlink()
        return left
    legacy_dir.rmdir()
    return []


def migrate_home(home: pathlib.Path | None = None, environ: dict | os._Environ | None = None,
                 find_running: Callable[[], list[tuple[str, int]]] | None = None) -> dict:
    """Move ``~/<legacy home>`` to ``~/<data dir>``; return a report. Raises :class:`MigrateRefused`.

    ``moved`` is False (and nothing is touched) when the old folder is already gone and the new
    database is in place: a second run is a no-op. ``find_running`` is the process check (tests inject it).
    """
    home = pathlib.Path.home() if home is None else pathlib.Path(home)
    environ = os.environ if environ is None else environ
    find_running = running_programs if find_running is None else find_running
    legacy_name = identity.LEGACY_HOMES[0]
    legacy_dir, new_dir = home / legacy_name, home / identity.DATA_DIR
    legacy_label, new_label = f"~/{legacy_name}", f"~/{identity.DATA_DIR}"

    stale = _legacy_env_set(environ)
    if stale:
        raise MigrateRefused(f"{', '.join(stale)} is set; unset it and rerun (the old names are no longer "
                             f"read and {identity.ENV_PREFIX}DB pins the path, so a move could not be "
                             "followed)")
    if not legacy_dir.is_dir():
        if (new_dir / identity.DB_FILENAME).is_file():
            return {"moved": False, "message": "nothing to migrate", "from": legacy_label, "to": new_label,
                    "before": [], "after": _listing(new_dir)}
        raise MigrateRefused(f"no legacy data folder {legacy_label} to migrate")
    for name, pid in find_running():
        raise MigrateRefused(f"{name} (pid {pid}) is running; quit the app and Claude Desktop, then retry")

    # a half-done run has the siblings renamed already: the database is then under its new name
    legacy_db = legacy_dir / identity.LEGACY_DB_FILENAME
    renamed_db = legacy_dir / identity.DB_FILENAME
    have_db = legacy_db.is_file() or renamed_db.is_file()
    if have_db and (new_dir / identity.DB_FILENAME).is_file():
        raise MigrateRefused(f"a database exists in both {legacy_label} and {new_label}; nothing was moved. "
                             "Move one of them away by hand, then rerun")
    if legacy_db.is_file() and renamed_db.is_file():
        raise MigrateRefused(f"both {legacy_db.name} and {renamed_db.name} exist in {legacy_label}; "
                             "nothing was moved. Move one of them away by hand, then rerun")
    clashes = _clashes(_plan(legacy_dir), new_dir)
    if clashes:
        raise MigrateRefused(f"{', '.join(clashes)} would overwrite a file that already exists; "
                             "nothing was moved. Move one of them away by hand, then rerun")

    before = _listing(legacy_dir)
    # lock under the name the lock file currently has; a folder with no database has nothing to lock
    legacy_lock = legacy_dir / (legacy_db.name + ".write-lock")
    lock_db = renamed_db if renamed_db.is_file() and not legacy_lock.exists() else legacy_db
    guard = (write_lock(lock_db, "migrate-home", timeout_s=0) if have_db
             else contextlib.nullcontext())
    try:
        with guard:
            if have_db:
                busy = _lsof_pids([lock_db, *(lock_db.with_name(lock_db.name + s) for s in ("-wal", "-shm"))])
                if busy:
                    raise MigrateRefused(f"another process (pid {', '.join(busy)}) has the legacy database "
                                         "open; quit the app and Claude Desktop, then retry")
                db_file = legacy_db if legacy_db.is_file() else renamed_db
                _checkpoint(db_file, db_file.with_name(db_file.name + "-wal"))
            plan = _plan(legacy_dir)                     # again: taking the lock may have created its file
            for old, new in plan.items():
                if new != old:
                    os.rename(legacy_dir / old, legacy_dir / new)
            if new_dir.is_dir():
                for new in plan.values():
                    os.rename(legacy_dir / new, new_dir / new)
            else:
                os.rename(legacy_dir, new_dir)           # the commit point
    except WriteLockBusy as exc:
        held = f" by {exc.holder.get('purpose')}, pid {exc.holder.get('pid')}" if exc.holder else ""
        raise MigrateRefused(f"the legacy store's write lock is held{held}; "
                             "quit the app and Claude Desktop, then retry") from exc
    _drop_holders(new_dir)
    left_behind: list[str] = []
    if legacy_dir.is_dir():
        _drop_holders(legacy_dir)
        left_behind = _remove_legacy(legacy_dir)
    report = {"moved": True, "message": f"moved {legacy_label} to {new_label}", "from": legacy_label,
              "to": new_label, "before": before, "after": _listing(new_dir)}
    if left_behind:
        report["left_behind"] = left_behind
        report["message"] += f"; {legacy_label} is a link whose target still holds {', '.join(left_behind)}"
    return report
