"""The coverage ledger: present > failed > source_empty > not_covered, derived from retained files."""

import datetime
import json
import pathlib

import pytest

from disconect import contract, coverage, health, queries, storage
from disconect.ingest import connect_export, sources
from disconect.storage import migrations
from test_import import _build_export

UTC = datetime.timezone.utc


def _raw(conn, stream, start, end, scope="device"):
    cursor = conn.execute(
        "INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
        "payload_hash, payload_bytes, start_utc, end_utc, imported_at) VALUES(?,?,?,'usb','fit',x'00','h',1,?,?,"
        "'2025-07-01T00:00:00Z')", (stream, f"{stream}-{start}", scope, start, end))
    return cursor.lastrowid


def _seed_scenario(db_path):
    """One stream (fit:sleep -> sleep_score[device]) over 10 days, every status represented.

    day 01-02: file spans them, rows exist              -> present
    day 03:    file spans it, no row                    -> source_empty
    day 04-05: nothing at all                            -> not_covered
    day 06:    a failure recorded with its span          -> failed
    day 07:    file spans it AND a failure spans it,
               and a row exists                          -> present (precedence)
    day 08:    failure and file span, no row             -> failed (over source_empty)
    day 09-10: export window claimed (json:sleep,
               vendor_cloud), no rows                    -> source_empty for vendor_cloud only
    """
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO import_runs(id, started_at, transport, status) VALUES(1,'2025-07-01T00:00:00Z','usb','ok')")
        raw = _raw(conn, "fit:sleep", "2025-06-01T00:00:00Z", "2025-06-03T12:00:00Z")
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, raw_record_id) "
                         "VALUES(?,?,?,?,?)", [("2025-06-01", "sleep_score", 80, "device", raw),
                                               ("2025-06-02", "sleep_score", 70, "device", raw)])
        conn.execute("INSERT INTO import_failures(run_id, stream, start_utc, end_utc, kind, recorded_at) "
                     "VALUES(1,'fit:sleep','2025-06-06T01:00:00Z','2025-06-06T06:00:00Z','x','2025-07-01T00:00:00Z')")
        raw2 = _raw(conn, "fit:sleep", "2025-06-07T00:00:00Z", "2025-06-08T12:00:00Z")
        conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope, raw_record_id) "
                     "VALUES('2025-06-07','sleep_score',75,'device',?)", (raw2,))
        conn.execute("INSERT INTO import_failures(run_id, stream, start_utc, end_utc, kind, recorded_at) "
                     "VALUES(1,'fit:sleep','2025-06-07T01:00:00Z','2025-06-08T06:00:00Z','x','2025-07-01T00:00:00Z')")
        conn.execute("INSERT INTO export_ranges(run_id, stream, from_day, to_day) VALUES(1,'json:sleep','2025-06-09','2025-06-10')")
        # an unattributed failure (no stream) and an undatable raw file
        conn.execute("INSERT INTO import_failures(run_id, kind, recorded_at) VALUES(1,'x','2025-07-01T00:00:00Z')")
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                     "payload_hash, payload_bytes, imported_at) VALUES('fit:41','k','device','usb','fit',x'00','h',1,"
                     "'2025-07-01T00:00:00Z')")


def _row(ledger, metric, scope):
    return next(r for r in ledger["ledger"] if r["metric"] == metric and r["source_scope"] == scope)


def test_four_statuses_and_precedence(db_path):
    _seed_scenario(db_path)
    conn = storage.open_read_only(db_path)
    ledger = coverage.ledger(conn, "2025-06-10", 10)
    device = _row(ledger, "sleep_score", "device")
    assert (device["present"], device["failed"], device["source_empty"], device["not_covered"]) == (3, 2, 1, 4)
    assert device["gaps"] == [
        {"from": "2025-06-03", "to": "2025-06-03", "status": "source_empty"},
        {"from": "2025-06-04", "to": "2025-06-05", "status": "not_covered"},
        {"from": "2025-06-06", "to": "2025-06-06", "status": "failed"},
        {"from": "2025-06-08", "to": "2025-06-08", "status": "failed"},
        {"from": "2025-06-09", "to": "2025-06-10", "status": "not_covered"},
    ]
    cloud = _row(ledger, "sleep_score", "vendor_cloud")
    assert cloud["source_empty"] == 2 and cloud["not_covered"] == 8 and cloud["present"] == 0
    assert ledger["unattributed_failures"] == 1 and ledger["undatable_files"] == 1
    assert ledger["refinements_available"] is True and ledger["map_drift"] == []
    assert ledger["window"] == {"from": "2025-06-01", "to": "2025-06-10", "days": 10}
    conn.close()


def test_window_is_capped_and_gaps_truncated(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        raw = _raw(conn, "fit:sleep", "2024-01-01T00:00:00Z", "2025-12-31T00:00:00Z")
        # a row every other day -> more than MAX_GAPS single-day source_empty gaps
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, raw_record_id) VALUES(?,?,?,?,?)",
                         [((datetime.date(2025, 1, 1) + datetime.timedelta(days=d)).isoformat(), "sleep_score", 1, "device", raw)
                          for d in range(0, 120, 2)])
    conn = storage.open_read_only(db_path)
    ledger = coverage.ledger(conn, "2025-12-31", 5000)
    assert ledger["window"]["days"] == coverage.MAX_WINDOW_DAYS
    row = _row(ledger, "sleep_score", "device")
    assert row["gaps_truncated"] is True and len(row["gaps"]) == coverage.MAX_GAPS
    conn.close()


def test_sample_metrics_use_local_dates(db_path):
    """A sample at 22:00 UTC on a UTC+3 watch belongs to the next local day."""
    from disconect.ingest.clock import ClockOffsets
    from disconect.ingest.model import ClockOffset
    with storage.open_for_write(db_path, "test") as conn:
        raw = _raw(conn, "fit:monitoring_b", "2025-06-01T22:00:00Z", "2025-06-01T23:00:00Z")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 12, tzinfo=UTC), 10800)], None, raw)
        conn.execute("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, raw_record_id) "
                     "VALUES('heart_rate','2025-06-01T22:00:00Z',60,'device',?)", (raw,))
    conn = storage.open_read_only(db_path)
    ledger = coverage.ledger(conn, "2025-06-02", 2)
    row = _row(ledger, "heart_rate", "device")
    assert row["gaps"] == [{"from": "2025-06-01", "to": "2025-06-01", "status": "not_covered"}]
    assert row["present"] == 1, "the 22:00Z sample is local 2025-06-02; the file span (also local 06-02) covers it"
    conn.close()


def test_v1_database_still_answers(db_path):
    """A store never upgraded past v1 (the read-only MCP path never migrates) degrades, not errors."""
    conn = storage.sqlite.connect(str(db_path))
    conn.executescript(migrations.MIGRATIONS[0][1])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                 "payload_hash, payload_bytes, start_utc, end_utc, imported_at) VALUES('fit:sleep','k','device','usb','fit',"
                 "x'00','h',1,'2025-06-01T00:00:00Z','2025-06-02T00:00:00Z','2025-07-01T00:00:00Z')")
    conn.commit()
    conn.close()
    conn = storage.open_read_only(db_path)
    assert migrations.current_version(conn) == 1
    report = health.data_health(conn, 30)
    ledger = report["coverage"]
    assert ledger["refinements_available"] is False and "schema v1" in ledger["refinements"]
    row = _row(coverage.ledger(conn, "2025-06-02", 2), "sleep_score", "device")
    assert (row["source_empty"], row["not_covered"]) == (1, 1), "a span ending at 00:00 does not cover the next day"
    text = health.summarize_for_humans(report)
    assert "coverage, last 30 days" in text and "note:" in text
    conn.close()


def test_declared_map_covers_the_contract_and_observed_is_subset(tmp_path, db_path):
    names = set(contract.metric_names()) | set(contract.label_names())
    assert {metric for metric, _scope in contract.STREAMS_FOR} == names
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn)
    conn = storage.open_read_only(db_path)
    observed = coverage._observed_map(conn)
    assert observed, "the fixture import produced rows"
    undeclared = {t for t in observed if t[2] not in contract.STREAMS_FOR.get((t[0], t[1]), ())}
    assert undeclared == set(), f"decoders emit (metric, scope, stream) triples the contract does not declare: {undeclared}"
    assert coverage.ledger(conn, "2025-06-16", 3)["map_drift"] == []
    conn.close()


@pytest.mark.parametrize("name,expected", [
    ("UDSFile_2025-03-24_2025-07-02.json", ("2025-03-24", "2025-07-02")),
    ("2024-08-24_2025-07-03_12345678_sleepData.json", ("2024-08-24", "2025-07-03")),
    ("TrainingReadinessDTO_20250605_20250701_123456789.json", ("2025-06-05", "2025-07-01")),
    ("12345678_fitnessAgeData.json", None),
    ("UDSFile_2025-07-02_2025-03-24.json", None),      # reversed bounds: refuse rather than guess
    ("X_20251399_20251401.json", None),                 # not dates
])
def test_date_range_in_name(name, expected):
    assert connect_export.date_range_in_name(name) == expected


def test_export_import_records_windows_and_bad_json(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    (root / "DI_CONNECT" / "DI-Connect-Metrics" / "EnduranceScore_20250610_20250620_111.json").write_text("{not json")
    with storage.open_for_write(db_path, "test") as conn:
        stats = sources.import_path(root, conn)
    assert stats.files_failed == 1 and stats.failures[0]["kind"] == "bad_json"
    conn = storage.open_read_only(db_path)
    ranges = {tuple(r) for r in conn.execute("SELECT stream, from_day, to_day FROM export_ranges")}
    assert ("json:uds", "2025-06-15", "2025-06-16") in ranges
    assert ("json:sleep", "2025-06-15", "2025-06-16") in ranges
    assert ("json:endurance", "2025-06-10", "2025-06-20") in ranges
    failure = conn.execute("SELECT stream, start_utc, end_utc, raw_record_id FROM import_failures").fetchone()
    assert tuple(failure) == ("json:endurance", "2025-06-10T12:00:00Z", "2025-06-20T12:00:00Z", None)
    row = _row(coverage.ledger(conn, "2025-06-20", 20), "endurance_score", "vendor_cloud")
    assert row["failed"] == 11 and row["present"] == 0
    # the day after the UDS window but inside nothing else is not_covered; 06-17 (stub without wellness) is source_empty
    uds = _row(coverage.ledger(conn, "2025-06-18", 4), "steps", "vendor_cloud")
    assert [g["status"] for g in uds["gaps"]] == ["source_empty", "not_covered"]
    conn.close()


def test_reparse_force_failure_is_placed_then_cleared(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn)
        raw_id = conn.execute("SELECT id FROM raw_records WHERE stream='fit:sleep' ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE raw_records SET payload=x'789c0300000001' WHERE id=?", (raw_id,))  # zlib of b''
        stats = sources.reparse_all(conn, streams=["fit:sleep"], force=True)
        assert stats.files_failed == 1
        placed = conn.execute("SELECT raw_record_id FROM import_failures").fetchall()
        assert [r[0] for r in placed] == [raw_id]
        row = _row(coverage.ledger(conn, "2025-06-17", 5), "sleep_score", "device")
        assert row["failed"] >= 1 and row["present"] == 1, "the broken night is failed, the other night present"
        sources.reparse_all(conn, streams=["fit:sleep"], force=True)
        assert conn.execute("SELECT COUNT(*) FROM import_failures").fetchone()[0] == 1, "cleared, then re-recorded once"


def test_health_json_is_serialisable_and_human_lines_present(db_path):
    _seed_scenario(db_path)
    conn = storage.open_read_only(db_path)
    report = health.data_health(conn, 3650)
    json.dumps(report)
    text = health.summarize_for_humans(report)
    assert "present / failed / source_empty / not_covered" in text
    assert "failures not placeable on a day: 1" in text
    conn.close()


def test_truncated_fit_failure_is_placed_once(db_path):
    """An import-time FIT decode failure keeps the header's stream and span, and the same bytes fail once."""
    from test_import import _monitoring_day
    from disconect.ingest.clock import ClockOffsets
    from disconect.ingest.writer import Writer
    good = _monitoring_day(datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC), 8000)
    broken = good[: len(good) // 2] + b"\xff" * 64
    with storage.open_for_write(db_path, "test") as conn:
        for _ in range(2):
            writer = Writer(conn, ClockOffsets.load(conn), "usb")
            writer.begin_run()
            assert writer.write_fit(broken, "x.fit") == "failed"
            writer.finish_run()
        rows = conn.execute("SELECT stream, start_utc, end_utc, payload_hash FROM import_failures").fetchall()
        assert len(rows) == 1 and rows[0][0] == "fit:monitoring_b" and rows[0][1] is not None and rows[0][3]
        ledger = coverage.ledger(conn, "2025-06-16", 3)
        assert _row(ledger, "heart_rate", "device")["failed"] >= 1
        assert ledger["unattributed_failures"] == 0


def test_claimed_window_is_clipped_to_the_file_date(tmp_path, db_path):
    import os
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    uds = root / "DI_CONNECT" / "DI-Connect-Aggregator" / "UDSFile_2025-06-15_2025-06-16.json"
    stamp = datetime.datetime(2025, 6, 15, 12, tzinfo=UTC).timestamp()
    os.utime(uds, (stamp, stamp))
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn)
        sources.import_path(root, conn)  # re-import: ranges recorded once
    conn = storage.open_read_only(db_path)
    ranges = [tuple(r) for r in conn.execute("SELECT stream, from_day, to_day FROM export_ranges WHERE stream='json:uds'")]
    assert ranges == [("json:uds", "2025-06-15", "2025-06-15")]
    conn.close()


def test_half_hour_zone_samples_land_on_their_local_day(db_path):
    from disconect.ingest.clock import ClockOffsets
    from disconect.ingest.model import ClockOffset
    with storage.open_for_write(db_path, "test") as conn:
        raw = _raw(conn, "fit:monitoring_b", "2025-06-01T00:00:00Z", "2025-06-03T00:00:00Z")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 12, tzinfo=UTC), 19800)], None, raw)  # +5:30
        # 18:10Z = 23:40 local 06-01; 18:40Z = 00:10 local 06-02 -- the same UTC hour bucket
        conn.execute("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, raw_record_id) "
                     "VALUES('stress','2025-06-01T18:10:00Z',30,'device',?)", (raw,))
        conn.execute("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, raw_record_id) "
                     "VALUES('heart_rate','2025-06-01T18:40:00Z',60,'device',?)", (raw,))
    conn = storage.open_read_only(db_path)
    ledger = coverage.ledger(conn, "2025-06-02", 2)
    assert _row(ledger, "stress", "device")["gaps"] == [{"from": "2025-06-02", "to": "2025-06-02", "status": "source_empty"}]
    assert _row(ledger, "heart_rate", "device")["gaps"] == [{"from": "2025-06-01", "to": "2025-06-01", "status": "source_empty"}]
    conn.close()


# ---- completeness (7b-12) --------------------------------------------------------------------

def _minutes(conn, raw, metric, start, end, step_s=60, scope="device"):
    """One reading of ``metric`` every ``step_s`` from ``start`` up to (excluding) ``end``."""
    moment = start
    rows = []
    while moment < end:
        rows.append((metric, moment.strftime("%Y-%m-%dT%H:%M:%SZ"), 60, scope, raw))
        moment += datetime.timedelta(seconds=step_s)
    conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, raw_record_id) VALUES(?,?,?,?,?)", rows)


def _utc(day, hour=0, minute=0):
    return datetime.datetime(2025, 6, day, hour, minute, tzinfo=UTC)


def test_completeness_full_day_gap_sparse_and_partial(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        full = _raw(conn, "fit:monitoring_b", "2025-06-01T00:00:00Z", "2025-06-02T00:00:00Z")
        _minutes(conn, full, "heart_rate", _utc(1), _utc(2))                    # 06-01: every minute -> 100
        twenty = _raw(conn, "fit:monitoring_b", "2025-06-02T00:00:00Z", "2025-06-02T20:00:00Z")
        _minutes(conn, twenty, "heart_rate", _utc(2), _utc(2, 8))              # 06-02: 8 h, 4 h off, 8 h of 20 h -> 80
        _minutes(conn, twenty, "heart_rate", _utc(2, 12), _utc(2, 20))
        hour = _raw(conn, "fit:monitoring_b", "2025-06-03T00:00:00Z", "2025-06-03T01:00:00Z")
        _minutes(conn, hour, "heart_rate", _utc(3), _utc(3, 1), step_s=360)    # 06-03: ten readings 6 min apart -> 16
        today = _raw(conn, "fit:monitoring_b", "2025-06-04T00:00:00Z", "2025-06-04T10:00:00Z")
        _minutes(conn, today, "heart_rate", _utc(4), _utc(4, 10))               # 06-04: a partial file, worn throughout -> 100
    conn = storage.open_read_only(db_path)
    got = coverage.day_completeness(conn, "heart_rate", "device", "2025-05-31", "2025-06-05")
    assert got == {"2025-05-31": None, "2025-06-01": 100, "2025-06-02": 80, "2025-06-03": 16, "2025-06-04": 100,
                   "2025-06-05": None}
    # the calendar carries it next to the status, and a day no file spans has neither
    calendar = {row["day"]: (row["status"], row["completeness"])
                for row in queries.metric_calendar(conn, "heart_rate", "device", "2025-05-31", "2025-06-02")}
    assert calendar == {"2025-05-31": ("not_covered", None), "2025-06-01": ("present", 100), "2025-06-02": ("present", 80)}
    conn.close()


def test_completeness_counts_overlapping_files_once_and_is_null_off_the_per_minute_set(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        first = _raw(conn, "fit:monitoring_b", "2025-06-01T00:00:00Z", "2025-06-01T13:00:00Z")
        _raw(conn, "fit:monitoring_b", "2025-06-01T11:00:00Z", "2025-06-02T00:00:00Z")
        _minutes(conn, first, "heart_rate", _utc(1), _utc(1, 12))               # 12 h of 24 (not of 26) -> 50
        _minutes(conn, first, "hrv_rmssd", _utc(1), _utc(1, 12), step_s=300)
        conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope, raw_record_id) "
                     "VALUES('2025-06-01','steps',100,'device',?)", (first,))
    conn = storage.open_read_only(db_path)
    assert coverage.day_completeness(conn, "heart_rate", "device", "2025-06-01", "2025-06-01") == {"2025-06-01": 50}
    assert coverage.day_completeness(conn, "hrv_rmssd", "device", "2025-06-01", "2025-06-01") == {"2025-06-01": None}
    assert coverage.day_completeness(conn, "steps", "device", "2025-06-01", "2025-06-01") == {"2025-06-01": None}
    assert coverage.day_completeness(conn, "heart_rate", "live", "2025-06-01", "2025-06-01") == {"2025-06-01": None}
    with pytest.raises(ValueError, match="last_day must not be before first_day"):
        coverage.day_completeness(conn, "heart_rate", "device", "2025-06-02", "2025-06-01")
    conn.close()


def test_completeness_claimed_export_window_covers_the_whole_day(db_path):
    """A stream the ledger sees only through an export window (no datable file) covers its days whole."""
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO import_runs(id, started_at, transport, status) VALUES(1,'2025-07-01T00:00:00Z','connect_export','ok')")
        raw = conn.execute(
            "INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, payload_hash, "
            "payload_bytes, imported_at) VALUES('json:hr','k','vendor_cloud','connect_export','json',x'00','h',1,"
            "'2025-07-01T00:00:00Z')").lastrowid
        conn.execute("INSERT INTO export_ranges(run_id, stream, from_day, to_day) VALUES(1,'json:hr','2025-06-01','2025-06-01')")
        _minutes(conn, raw, "heart_rate", _utc(1), _utc(1, 6), scope="vendor_cloud")   # 6 h of a claimed day -> 25
    conn = storage.open_read_only(db_path)
    assert coverage.day_completeness(conn, "heart_rate", "vendor_cloud", "2025-06-01", "2025-06-02") == {
        "2025-06-01": 25, "2025-06-02": None}
    conn.close()


def test_completeness_follows_the_watch_clock(db_path):
    """On a +5:30 watch the local day 06-01 is 05-31T18:30Z..06-01T18:30Z; a file and readings over exactly that are 100."""
    from disconect.ingest.clock import ClockOffsets
    from disconect.ingest.model import ClockOffset
    with storage.open_for_write(db_path, "test") as conn:
        raw = _raw(conn, "fit:monitoring_b", "2025-05-31T18:30:00Z", "2025-06-01T18:30:00Z")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 6, tzinfo=UTC), 19800)], None, raw)
        _minutes(conn, raw, "stress", datetime.datetime(2025, 5, 31, 18, 30, tzinfo=UTC), _utc(1, 18, 30))
    conn = storage.open_read_only(db_path)
    assert coverage.day_completeness(conn, "stress", "device", "2025-05-31", "2025-06-02") == {
        "2025-05-31": None, "2025-06-01": 100, "2025-06-02": None}
    conn.close()
