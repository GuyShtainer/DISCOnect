"""Opening, migrating, locking: the guarantees every outlet relies on."""

import multiprocessing
import time

import pytest

from disconect import storage
from disconect.storage import migrations


def test_open_for_write_creates_and_migrates(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        assert migrations.current_version(conn) == migrations.SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"raw_records", "metric_samples", "daily_metrics", "sleep_sessions", "stream_provenance"} <= tables


def test_migrate_is_idempotent(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        first = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    with storage.open_for_write(db_path, "test") as conn:
        assert conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall() == first


def test_read_only_refuses_writes_and_missing_db(db_path):
    with pytest.raises(storage.NotConfigured):
        storage.open_read_only(db_path)
    with storage.open_for_write(db_path, "test"):
        pass
    conn = storage.open_read_only(db_path)
    with pytest.raises(storage.sqlite.OperationalError):
        conn.execute("INSERT INTO import_runs(started_at, transport, status) VALUES('x','y','z')")
    conn.close()


def test_schema_too_new_is_refused(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 5}")
    with pytest.raises(storage.SchemaTooNew):
        storage.open_read_only(db_path)
    with pytest.raises(storage.SchemaTooNew):
        with storage.open_for_write(db_path, "test"):
            pass


def _hold_lock(path, seconds):
    with storage.write_lock(path, "holder", timeout_s=1):
        time.sleep(seconds)


def test_second_writer_is_busy_but_readers_are_not(db_path):
    with storage.open_for_write(db_path, "test"):
        pass
    holder = multiprocessing.get_context("spawn").Process(target=_hold_lock, args=(db_path, 2.0))
    holder.start()
    try:
        time.sleep(0.6)
        with pytest.raises(storage.WriteLockBusy):
            with storage.open_for_write(db_path, "second", timeout_s=0.3):
                pass
        reader = storage.open_read_only(db_path)  # readers never queue behind the writer
        assert reader.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 0
        reader.close()
    finally:
        holder.join()
    with storage.open_for_write(db_path, "after", timeout_s=1):
        pass  # lock released with the process


def test_a_write_open_marks_a_dead_writers_running_run_interrupted(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO import_runs(started_at, transport, status) VALUES('2026-01-01T00:00:00Z','export','running')")
        conn.commit()
    ro = storage.open_read_only(db_path)
    assert [r[0] for r in ro.execute("SELECT status FROM import_runs")] == ["running"]
    ro.close()
    with storage.open_for_write(db_path, "test") as conn:
        row = [tuple(r) for r in conn.execute("SELECT status, finished_at, error FROM import_runs")]
        assert row == [("interrupted", None, None)]
        # committed before the connection is handed out: a reader opened meanwhile sees it
        peek = storage.open_read_only(db_path)
        assert [r[0] for r in peek.execute("SELECT status FROM import_runs")] == ["interrupted"]
        peek.close()
        # a run begun after the open stays running: the UPDATE runs only at open time
        conn.execute("INSERT INTO import_runs(started_at, transport, status) VALUES('2026-01-02T00:00:00Z','export','running')")
        conn.commit()
        assert [r[0] for r in conn.execute("SELECT status FROM import_runs ORDER BY id")] == ["interrupted", "running"]


def test_a_symlinked_store_name_shares_the_write_lock(tmp_path):
    """Review: a second name for the store (a symlink) meets the same lock, not a lock of its own."""
    import importlib

    wl = importlib.import_module("disconect.storage.write_lock")   # the module, not the re-exported context manager

    db = tmp_path / "real.hbdb"
    db.write_bytes(b"")
    link = tmp_path / "link.hbdb"
    link.symlink_to(db)
    assert wl.lock_path_for(link) == wl.lock_path_for(db)
    with storage.write_lock(db, "first", timeout_s=1):
        with pytest.raises(wl.WriteLockBusy) as caught:
            with storage.write_lock(link, "second", timeout_s=0.1):
                pass
        assert caught.value.holder["purpose"] == "first"
    with storage.write_lock(link, "after", timeout_s=0.1):
        pass
    missing = tmp_path / "not-yet.hbdb"
    real_dir = tmp_path.resolve()
    assert wl.lock_path_for(missing) == real_dir / "not-yet.hbdb.write-lock"   # as spelled (folder resolved) until it exists
    dangling = tmp_path / "dangling.hbdb"                                       # SQLite would create the target
    dangling.symlink_to(tmp_path / "target.hbdb")
    assert wl.lock_path_for(dangling) == real_dir / "target.hbdb.write-lock"
    with storage.write_lock(dangling, "via the link", timeout_s=1):
        with pytest.raises(wl.WriteLockBusy):
            with storage.write_lock(tmp_path / "target.hbdb", "via the target", timeout_s=0.1):
                pass
