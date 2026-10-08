"""Bet 9b-2: the live fold -- ``json:live`` records thinned into ``live``-scope samples, a pure function of the raw set."""

import datetime
import json
import pathlib
import zlib

import pytest

from disconect import chart, contract, coverage, health, insight, queries, storage
from disconect.ingest import live, sources
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import ClockOffset
from disconect.ingest.writer import Writer

UTC = datetime.timezone.utc
BASE = int(datetime.datetime(2025, 6, 15, 10, tzinfo=UTC).timestamp())


def _lines(path: pathlib.Path, lines: list[dict]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _live_rows(db) -> list[tuple]:
    with storage.open_read_only(db) as conn:
        return conn.execute("SELECT metric, ts_utc, value, source_scope, device_id, "
                            "(SELECT source_key FROM raw_records r WHERE r.id = raw_record_id) "
                            "FROM metric_samples WHERE source_scope='live' ORDER BY metric, ts_utc").fetchall()


def _import(db, source) -> None:
    with storage.open_for_write(db, "test") as conn:
        sources.import_path(source, conn)


# ---- the pure function ----

def test_one_sample_per_metric_and_utc_minute_with_the_lower_median_and_the_minute_floor():
    readings = [[BASE + 1.5, "heart_rate", 70], [BASE + 30, "heart_rate", 90], [BASE + 59.999, "heart_rate", 80],
                [BASE + 60, "heart_rate", 100], [BASE + 61, "heart_rate", 99]]
    rows, dropped = live.fold_records([(7, readings)])
    assert rows == [("heart_rate", "2025-06-15T10:00:00Z", 80, 7), ("heart_rate", "2025-06-15T10:01:00Z", 99, 7)]
    assert dropped == {}
    assert live.lower_median([3, 1, 2, 4]) == 2 and live.lower_median([5]) == 5
    assert live.minute_floor(BASE + 119.9) == BASE + 60


def test_readings_are_de_duplicated_across_records_by_number_metric_and_value():
    a = [[BASE + 1, "heart_rate", 70], [BASE + 2.0, "heart_rate", 72], [BASE + 10, "stress", 30]]
    b = [[BASE + 1.0, "heart_rate", 70], [BASE + 2, "heart_rate", 72], [BASE + 2, "heart_rate", 74], [BASE + 3, "heart_rate", 76]]
    rows, _ = live.fold_records([(1, a), (2, b)])
    # the minute holds 70, 72, 74, 76 once each (lower median 72); the record that contributed the first reading owns it
    assert rows == [("heart_rate", "2025-06-15T10:00:00Z", 72, 1), ("stress", "2025-06-15T10:00:00Z", 30, 1)]
    rows_reversed, _ = live.fold_records([(2, b), (1, a)])
    assert [r[:3] for r in rows_reversed] == [r[:3] for r in rows]
    assert rows_reversed[0][3] == 2


def test_sentinels_follow_the_fit_decoder_and_steps_and_unknown_metrics_are_ignored():
    readings = [[BASE, "heart_rate", 0], [BASE, "stress", -1], [BASE, "respiration_rate", -2], [BASE, "spo2", 0],
                [BASE, "energy_reserve", 101], [BASE, "energy_reserve", -1], [BASE, "steps", 500], [BASE, "unknown", 1],
                [BASE, "energy_reserve", 0], [BASE, "stress", 0],
                # the upper bounds: a FIT uint8 never carries 0xFF, a percentage never 101 (review S3)
                [BASE, "heart_rate", 255], [BASE, "stress", 101], [BASE, "respiration_rate", 255], [BASE, "spo2", 101],
                [BASE, "heart_rate", 254], [BASE, "respiration_rate", 254], [BASE, "spo2", 100], [BASE, "stress", 100]]
    rows, dropped = live.fold_records([(1, readings)])
    assert rows == [("energy_reserve", "2025-06-15T10:00:00Z", 0, 1), ("heart_rate", "2025-06-15T10:00:00Z", 254, 1),
                    ("respiration_rate", "2025-06-15T10:00:00Z", 254, 1), ("spo2", "2025-06-15T10:00:00Z", 100, 1),
                    ("stress", "2025-06-15T10:00:00Z", 0, 1)]
    assert dropped == {"live_heart_rate_zero": 2, "live_stress_sentinel": 2, "live_respiration_sentinel": 2,
                       "live_spo2_off_wrist_or_zero": 2, "live_energy_reserve_out_of_range": 2}


def test_a_value_past_i64_makes_the_file_not_live_on_this_core_too(tmp_path, sessions):
    """Rust's JSON parser refuses integers past i64; the oracle must agree, or one such file would
    fold here and not there -- and, before the 9b-2 review (M1), crash every later import."""
    a_dir, _b_dir = sessions
    huge = tmp_path / "huge"
    huge.mkdir()
    for value in (2 ** 63, -(2 ** 63) - 1, 10 ** 400):
        _lines(huge / f"live-{abs(value) % 97}-{len(str(value))}.jsonl",
               [{"t": BASE + 1, "metric": "heart_rate", "value": value}, {"status": "stopped", "stop": "x"}])
        assert live.parse_live_file((huge / f"live-{abs(value) % 97}-{len(str(value))}.jsonl").read_bytes()) is None
    _lines(huge / "live-edge.jsonl", [{"t": BASE + 1, "metric": "heart_rate", "value": 2 ** 63 - 1},
                                      {"t": BASE + 1, "metric": "stress", "value": -(2 ** 63)}])
    assert live.parse_live_file((huge / "live-edge.jsonl").read_bytes()) == [[BASE + 1, "heart_rate", 2 ** 63 - 1],
                                                                              [BASE + 1, "stress", -(2 ** 63)]]
    db = tmp_path / "h.db"
    _import(db, huge)
    with storage.open_read_only(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_records WHERE stream='json:live'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_runs WHERE status='running'").fetchone()[0] == 0
    assert _live_rows(db) == []     # both edge readings are sentinels (out of range)
    _import(db, a_dir)              # the next import still runs and folds
    reference = tmp_path / "ref.db"
    _import(reference, a_dir)
    assert _live_rows(db) == _live_rows(reference) != []


# ---- the writer post-pass ----

@pytest.fixture
def sessions(tmp_path):
    """Two overlapping session files (``b`` holds all of ``a`` and more) in two folders, importable in any order."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    a = [{"t": BASE + 60 * i + 0.5, "metric": "heart_rate", "value": 70 + i} for i in range(5)]
    a += [{"t": BASE + 1, "metric": "steps", "value": 100}, {"t": BASE + 2, "metric": "stress", "value": -1}]
    b = a + [{"t": BASE + 60 * i, "metric": "heart_rate", "value": 70 + i} for i in range(5, 9)]
    b += [{"t": BASE + 60 * 2 + 30, "metric": "heart_rate", "value": 99}, {"t": BASE + 3, "metric": "spo2", "value": 96}]
    _lines(a_dir / "live-a.jsonl", [{"status": "scanning"}] + a + [{"status": "stopped", "stop": "LinkClosed"}])
    _lines(b_dir / "live-b.jsonl", [{"status": "scanning"}] + b + [{"status": "stopped", "stop": "LinkClosed"}])
    return a_dir, b_dir


EXPECTED = [("heart_rate", f"2025-06-15T10:0{i}:00Z", float(70 + i), "live", None) for i in range(9)] + [
    ("spo2", "2025-06-15T10:00:00Z", 96.0, "live", None)]


def test_the_fold_is_the_same_in_every_order_and_after_a_reparse(tmp_path, sessions):
    a_dir, b_dir = sessions
    both = tmp_path / "both"
    both.mkdir()
    (both / "live-a.jsonl").write_bytes((a_dir / "live-a.jsonl").read_bytes())
    (both / "live-b.jsonl").write_bytes((b_dir / "live-b.jsonl").read_bytes())
    results = {}
    for name, steps in (("a_then_b", [a_dir, b_dir]), ("b_then_a", [b_dir, a_dir]), ("one_sweep", [both])):
        db = tmp_path / f"{name}.db"
        for step in steps:
            _import(db, step)
        results[name] = _live_rows(db)
    reparsed = tmp_path / "reparsed.db"
    _import(reparsed, both)
    with storage.open_for_write(reparsed, "test") as conn:
        conn.execute("DELETE FROM metric_samples")
        stats = sources.reparse_all(conn)
        assert stats.files_failed == 0
    results["reparsed"] = _live_rows(reparsed)
    assert len(set(map(tuple, results.values()))) == 1, results
    rows = results["a_then_b"]
    assert [r[:5] for r in rows] == EXPECTED
    # the minute with two values (72 from a, 99 from b) keeps the lower median; every minute names the record
    # that contributed its first reading in content order, never the import order
    assert {r[5] for r in rows} <= {k for (k,) in storage.open_read_only(tmp_path / "a_then_b.db").execute(
        "SELECT source_key FROM raw_records")}


def test_live_rows_feed_no_daily_and_leave_fit_rows_and_the_sample_span_alone(tmp_path, sessions):
    a_dir, _b_dir = sessions
    db = tmp_path / "s.db"
    _import(db, a_dir)
    with storage.open_read_only(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM metric_samples WHERE source_scope != 'live'").fetchone()[0] == 0
    with storage.open_for_write(db, "test") as conn:
        writer = Writer(conn, ClockOffsets.load(conn), "test")
        assert writer.derive_live_samples() == 5
        assert writer._sample_span is None and writer.stats.dropped == {}


def test_sentinel_drops_are_counted_for_the_records_this_run_stored_only(tmp_path, sessions):
    a_dir, b_dir = sessions
    db = tmp_path / "s.db"
    with storage.open_for_write(db, "test") as conn:
        first = sources.import_path(a_dir, conn)
        second = sources.import_path(b_dir, conn)   # b carries the same sentinel reading: its own record, counted once
        third = sources.import_path(a_dir, conn)    # a again is DUPLICATE: nothing stored, nothing counted
    assert first.dropped == {"live_stress_sentinel": 1}
    assert second.dropped == {"live_stress_sentinel": 1}
    assert third.dropped == {}


def test_a_record_the_decoder_refuses_is_skipped_and_the_rest_still_folds(tmp_path, sessions):
    a_dir, _b_dir = sessions
    db = tmp_path / "s.db"
    _import(db, a_dir)
    with storage.open_for_write(db, "test") as conn:
        bad = zlib.compress(json.dumps({"readings": [[BASE, "heart_rate", "x"]]}).encode())
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, device_id, start_utc, end_utc, "
                     "payload_kind, payload, payload_hash, payload_bytes, imported_at) VALUES('json:live','bad','device','ble',"
                     "NULL,'2025-06-15T09:00:00Z','2025-06-15T09:00:00Z','json',?,'bad',1,'2025-07-01T00:00:00Z')", (bad,))
        Writer(conn, ClockOffsets.load(conn), "test").derive_live_samples()
    assert [r[:5] for r in _live_rows(db)] == EXPECTED[:5]


# ---- the read path ----

def test_as_of_health_counts_and_coverage_keep_live_apart(tmp_path, sessions):
    a_dir, _b_dir = sessions
    db = tmp_path / "s.db"
    _import(db, a_dir)
    with storage.open_read_only(db) as conn:
        assert insight._latest_stored_date(conn) is None
        # asked for the session scope alone, the facts anchor on its newest day (review S1)
        assert insight._latest_stored_date(conn, ("live",)) == "2025-06-15"
        assert insight.period_facts(conn, 7, 28)["reason"] == "nothing stored yet"
        live_only = insight.period_facts(conn, 7, 28, source_scope="live")
        assert live_only["as_of"] == "2025-06-15"
        assert [(f["metric"], f["window_days_with_data"]) for f in live_only["facts"]] == [("heart_rate", 1)]
        report = health.data_health(conn)
        assert report["live"] == {"records": 1, "samples": 5, "first_day": "2025-06-15", "last_day": "2025-06-15"}
        heart = next(m for m in report["metrics"] if m["metric"] == "heart_rate")
        assert heart["total"] is None and heart["days_in_window"] == 0
        assert "live link: 1 session records, 5 minute samples [live]" in health.summarize_for_humans(report)
        ledger = coverage.ledger(conn, "2025-06-16", 3)
        rows = {(r["metric"], r["source_scope"]): r for r in ledger["ledger"]}
        assert rows[("heart_rate", "live")]["streams"] == ["json:live"]
        assert (rows[("heart_rate", "live")]["present"], rows[("heart_rate", "live")]["not_covered"]) == (1, 2)
        assert ("spo2", "live") not in rows and ("stress", "live") not in rows
        assert all(r["source_empty"] == 0 and r["failed"] == 0 for r in ledger["ledger"] if r["source_scope"] == "live")
        assert ledger["map_drift"] == []
        assert coverage.day_statuses(conn, "heart_rate", "live", "2025-06-14", "2025-06-16") == {
            "2025-06-14": "not_covered", "2025-06-15": "present", "2025-06-16": "not_covered"}
        assert ("heart_rate", "live") in contract.SESSION_STREAMS_FOR
        assert ("heart_rate", "live") not in contract.STREAMS_FOR
        # the default facts never see a session; naming the scope is the one way to its facts
        assert contract.DEFAULT_FACT_SCOPES == ("device", "vendor_cloud", "local")
        assert insight.period_facts(conn, 7, 28, end_date="2025-06-16")["facts"] == []
        asked = insight.period_facts(conn, 7, 28, end_date="2025-06-16", source_scope="live")["facts"]
        assert [(f["metric"], f["source_scope"], f["window_days_with_data"]) for f in asked] == [("heart_rate", "live", 1)]


def test_the_live_block_days_are_the_watchs_local_days(tmp_path, sessions):
    a_dir, _b_dir = sessions
    db = tmp_path / "s.db"
    _import(db, a_dir)
    with storage.open_read_only(db) as conn:
        first, last = conn.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM metric_samples WHERE source_scope='live'").fetchone()
        assert health.data_health(conn)["live"]["first_day"] == first[:10]
    with storage.open_for_write(db, "test") as conn:
        conn.execute("UPDATE metric_samples SET ts_utc = '2025-06-15T02:30:00Z' WHERE source_scope='live' AND ts_utc = ?", (first,))
        raw = conn.execute("SELECT id FROM raw_records WHERE stream='json:live'").fetchone()[0]
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 12, tzinfo=UTC), -18000)], None, raw)
    with storage.open_read_only(db) as conn:
        live = health.data_health(conn)["live"]
    assert live["first_day"] == "2025-06-14"  # 02:30Z is the previous evening at -5 h
    assert live["last_day"] == "2025-06-15"  # the newest sample is 10:xx UTC the same day, -5 h keeps the date
    with storage.open_for_write(db, "test") as conn:
        # +3 h: 23:30Z is already the next day on the watch, so last_day moves too
        conn.execute("UPDATE metric_samples SET ts_utc = '2025-06-21T23:30:00Z' WHERE source_scope='live' AND ts_utc = ?", (last,))
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 12, tzinfo=UTC), 10800)], None, raw)
    with storage.open_read_only(db) as conn:
        live = health.data_health(conn)["live"]
    assert (live["first_day"], live["last_day"]) == ("2025-06-15", "2025-06-22")
    with storage.open_for_write(db, "test") as conn:
        # the calendar ends at 9999: the UTC prefix is kept instead of an OverflowError (the Rust core does the same)
        conn.execute("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, raw_record_id) "
                     "VALUES('heart_rate', '9999-12-31T23:46:00Z', 60, 'live', ?)", (raw,))
    with storage.open_read_only(db) as conn:
        assert health.data_health(conn)["live"]["last_day"] == "9999-12-31"


def test_a_reading_at_the_last_accepted_stamp_reads_back_at_a_plus_14_hour_offset(tmp_path):
    """T_LIMIT - 1 is 9998-12-31T23:59:59Z; at +14 h its local day is 9999-01-01 and no read overflows."""
    assert live.T_LIMIT == int(datetime.datetime(9999, 1, 1, tzinfo=UTC).timestamp()) == 253370764800
    folder = tmp_path / "late"
    folder.mkdir()
    _lines(folder / "live-late.jsonl", [{"t": live.T_LIMIT - 1, "metric": "heart_rate", "value": 61}])
    db = tmp_path / "s.db"
    _import(db, folder)
    with storage.open_for_write(db, "test") as conn:
        raw = conn.execute("SELECT id FROM raw_records WHERE stream='json:live'").fetchone()[0]
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 1, 12, tzinfo=UTC), 50400)], None, raw)
    with storage.open_read_only(db) as conn:
        day = queries.live_day(conn, "9999-01-01")
        assert [(s["start_utc"], s["end_local"]) for s in day["sessions"]] == [
            ("9998-12-31T23:59:59Z", "9999-01-01T13:59")]
        assert [(m["metric"], m["minutes"]) for m in day["metrics"] if m["minutes"]] == [("heart_rate", 1)]
        assert len(day["sessions"]) == 1
        assert health.data_health(conn)["live"]["last_day"] == "9999-01-01"
    # a store written before the bound was lowered may hold a span past it: data.live skips it instead of failing
    with storage.open_for_write(db, "test") as conn:
        conn.execute("UPDATE raw_records SET start_utc='9999-12-31T23:59:59Z', end_utc='9999-12-31T23:59:59Z' "
                     "WHERE stream='json:live'")
    with storage.open_read_only(db) as conn:
        assert queries.live_day(conn, "9999-01-01")["sessions"] == []
        assert queries.live_day(conn, "2025-06-15")["sessions"] == []


def test_the_contract_names_the_scope_and_every_scope_has_a_chart_colour():
    assert contract.SOURCE_SCOPES == ("device", "vendor_cloud", "local", "live")
    assert contract.CONTRACT_VERSION == "2"
    assert "'live'" in contract.SOURCE_CONVENTION and "session" in contract.COVERAGE_CONVENTION
    assert set(contract.SOURCE_SCOPES) <= set(chart.SCOPE_COLORS)
    assert "live" in chart.SCOPE_LEGEND
    assert {metric for metric, _ in contract.SESSION_STREAMS_FOR} == {"heart_rate", "stress", "respiration_rate", "spo2", "energy_reserve"}


def test_reads_at_the_calendar_ends_answer_empty_instead_of_overflowing(tmp_path):
    """The over-fetch windows (-1 / +2 days) are clamped to 0001-01-01 .. 9999-12-31 (Rust twin: live_fold_test.rs)."""
    db = tmp_path / "s.db"
    with storage.open_for_write(db, "test"):
        pass
    with storage.open_read_only(db) as conn:
        offsets = ClockOffsets.load(conn)
        for edge in ("0001-01-01", "9999-12-31"):
            day = queries.live_day(conn, edge)
            assert day["sessions"] == [] and all(m["minutes"] == 0 for m in day["metrics"]), edge
            assert queries.sample_day_aggregates(conn, "heart_rate", edge, edge, offsets, ("device",)) == {}, edge
            assert queries.intraday_samples(conn, "heart_rate", edge)["series"] == [], edge
        assert coverage.fetch_window(datetime.date.min, datetime.date.min) == ("0001-01-01", "0001-01-03")
        assert coverage.fetch_window(datetime.date(1, 1, 2), datetime.date(1, 1, 2)) == ("0001-01-01", "0001-01-04")
        assert coverage.fetch_window(datetime.date.max, datetime.date.max) == ("9999-12-30", "9999-12-31")
        assert coverage.fetch_window(datetime.date(9999, 12, 30), datetime.date(9999, 12, 30)) == (
            "9999-12-29", "9999-12-31")
        assert coverage.fetch_window(datetime.date(9999, 12, 29), datetime.date(9999, 12, 29)) == (
            "9999-12-28", "9999-12-31")
    # the completeness day bounds: the midnight after 9999-12-31, and a local midnight before year 1 under a
    # positive offset, are plain integers (the opus review of 1e2d688 found `data.metric` at 9999-12-31 still internal)
    with storage.open_for_write(db, "test") as conn:
        conn.execute("INSERT INTO clock_offsets(ts_utc, offset_s) VALUES('2025-06-01T12:00:00Z', 45900)")
    with storage.open_read_only(db) as conn:
        for edge in ("0001-01-01", "9999-12-31"):
            assert coverage.calendar(conn, "heart_rate", "device", edge, edge) == [(edge, coverage.NOT_COVERED, None)]
