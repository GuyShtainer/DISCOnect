"""Reparse: replay decoding from retained raw bytes, without re-pulling from the watch.

Covers the seam ``test_import.py`` cannot: that corrupted or lost canonical
rows come back exactly from the raw bytes already on disk, that reparse never
creates new raw records (a decoder fix is a replay, not a new import), that
the readiness "morning wins" selection is re-derived correctly across the
whole batch, and that one raw record's garbage payload fails on its own
without aborting the rest of the run.
"""

import datetime
import zlib

from test_import import _build_export, _import, _monitoring_day

from disconect import storage
from disconect.ingest import sources

UTC = datetime.timezone.utc


def test_reparse_restores_corrupted_canonical_rows(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    _import(root, db_path)

    conn = storage.open_read_only(db_path)
    before_stages = [tuple(r) for r in conn.execute(
        "SELECT sleep_id, stage, start_utc, end_utc FROM sleep_stages ORDER BY start_utc")]
    before_rhr = conn.execute(
        "SELECT value FROM daily_metrics WHERE date='2025-06-15' AND metric='resting_heart_rate' "
        "AND source_scope='device'").fetchone()[0]
    conn.close()
    assert before_stages, "fixture must actually produce sleep stages, or this test proves nothing"
    assert before_rhr == 50

    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("DELETE FROM sleep_stages")
        conn.execute(
            "UPDATE daily_metrics SET value=-999 WHERE date='2025-06-15' AND metric='resting_heart_rate' "
            "AND source_scope='device'")
        conn.commit()
        stats = sources.reparse_all(conn)

    assert stats.status() == "ok" and stats.files_failed == 0
    assert stats.transport == "reparse"

    conn = storage.open_read_only(db_path)
    after_stages = [tuple(r) for r in conn.execute(
        "SELECT sleep_id, stage, start_utc, end_utc FROM sleep_stages ORDER BY start_utc")]
    after_rhr = conn.execute(
        "SELECT value FROM daily_metrics WHERE date='2025-06-15' AND metric='resting_heart_rate' "
        "AND source_scope='device'").fetchone()[0]
    conn.close()
    assert after_stages == before_stages
    assert after_rhr == 50


def test_reparse_writes_no_new_raw_records_and_reimport_stays_duplicate(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    first = _import(root, db_path)

    conn = storage.open_read_only(db_path)
    raw_before = conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
    conn.close()

    with storage.open_for_write(db_path, "test") as conn:
        stats = sources.reparse_all(conn)
    assert stats.status() == "ok" and stats.files_failed == 0

    conn = storage.open_read_only(db_path)
    raw_after = conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
    conn.close()
    assert raw_after == raw_before

    again = _import(root, db_path)
    assert again.files_imported == 0
    assert again.files_duplicate == first.files_imported
    assert again.records_written == 0


def test_reparse_readiness_batch_keeps_morning_wins(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    _import(root, db_path)

    with storage.open_for_write(db_path, "test") as conn:
        # Corrupt the daily value as if the afternoon update had wrongly won.
        conn.execute(
            "UPDATE daily_metrics SET value=60 WHERE date='2025-06-15' AND metric='training_readiness' "
            "AND source_scope='vendor_cloud'")
        conn.commit()
        stats = sources.reparse_all(conn)

    assert stats.status() == "ok" and stats.files_failed == 0
    conn = storage.open_read_only(db_path)
    morning = conn.execute(
        "SELECT value FROM daily_metrics WHERE date='2025-06-15' AND metric='training_readiness' "
        "AND source_scope='vendor_cloud'").fetchone()[0]
    latest_only = conn.execute(
        "SELECT value FROM daily_metrics WHERE date='2025-06-16' AND metric='training_readiness' "
        "AND source_scope='vendor_cloud'").fetchone()[0]
    conn.close()
    assert morning == 77, "the morning reset must win over the later realtime update, even after reparse"
    assert latest_only == 55


def test_reparse_garbage_payload_is_failed_not_fatal(tmp_path, db_path):
    folder = tmp_path / "GARMIN" / "Monitor"
    folder.mkdir(parents=True)
    day1 = datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC)
    day2 = day1 + datetime.timedelta(days=1)
    (folder / "A.FIT").write_bytes(_monitoring_day(day1, 8000))
    (folder / "B.FIT").write_bytes(_monitoring_day(day2, 5000))
    _import(tmp_path / "GARMIN", db_path)

    with storage.open_for_write(db_path, "test") as conn:
        target = conn.execute("SELECT id FROM raw_records ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE raw_records SET payload=? WHERE id=?",
                     (zlib.compress(b"not a fit file"), target))
        conn.commit()
        stats = sources.reparse_all(conn, force=True)

    assert stats.status() == "partial"
    assert stats.files_failed == 1
    assert len(stats.failures) == 1
    assert stats.failures[0]["file"] == f"raw_record:{target}"
    # the other raw record still reparsed fine -- one bad payload does not abort the run
    assert stats.files_imported == 1
