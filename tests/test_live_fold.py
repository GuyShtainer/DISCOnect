"""Bet 9b-2: the live fold -- ``json:live`` records thinned into ``live``-scope samples, a pure function of the raw set."""

import datetime
import json
import pathlib
import zlib

import pytest

from disconect import chart, contract, coverage, health, insight, storage
from disconect.ingest import live, sources
from disconect.ingest.clock import ClockOffsets
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
                [BASE, "energy_reserve", 0], [BASE, "stress", 0]]
    rows, dropped = live.fold_records([(1, readings)])
    assert rows == [("energy_reserve", "2025-06-15T10:00:00Z", 0, 1), ("stress", "2025-06-15T10:00:00Z", 0, 1)]
    assert dropped == {"live_heart_rate_zero": 1, "live_stress_sentinel": 1, "live_respiration_sentinel": 1,
                       "live_spo2_off_wrist_or_zero": 1, "live_energy_reserve_out_of_range": 2}


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


def test_the_contract_names_the_scope_and_every_scope_has_a_chart_colour():
    assert contract.SOURCE_SCOPES == ("device", "vendor_cloud", "local", "live")
    assert contract.CONTRACT_VERSION == "2"
    assert "'live'" in contract.SOURCE_CONVENTION and "session" in contract.COVERAGE_CONVENTION
    assert set(contract.SOURCE_SCOPES) <= set(chart.SCOPE_COLORS)
    assert "live" in chart.SCOPE_LEGEND
    assert {metric for metric, _ in contract.SESSION_STREAMS_FOR} == {"heart_rate", "stress", "respiration_rate", "spo2", "energy_reserve"}
