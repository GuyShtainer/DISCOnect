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
        # a run begun after the open stays running: the UPDATE runs only at open time
        conn.execute("INSERT INTO import_runs(started_at, transport, status) VALUES('2026-01-02T00:00:00Z','export','running')")
        conn.commit()
        assert [r[0] for r in conn.execute("SELECT status FROM import_runs ORDER BY id")] == ["interrupted", "running"]
