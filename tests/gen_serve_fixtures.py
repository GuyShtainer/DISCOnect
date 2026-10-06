#!/usr/bin/env python3
"""Build the committed synthetic store the serve differential and the Rust oracle replay run on.

    python tests/gen_serve_fixtures.py            # rewrite synthetic.hbdb, synthetic-v1.hbdb, empty.hbdb and the generated script entries
    python tests/gen_serve_fixtures.py --oracle   # also rewrite oracle-synthetic*.jsonl.gz (the Python responses)
    python tests/gen_serve_fixtures.py --keep-stores --oracle   # rewrite the script and the oracles only
    python tests/gen_serve_fixtures.py --live-only [--oracle]   # (re)build only synthetic-live.hbdb (and its oracle)

The store is entirely synthetic (the privacy test's seed rows plus the synthetic Connect export the
import tests build: serial and e-mail shapes are fake, plus the rows ``_extend_for_facts`` adds so that
``data.facts`` meets every branch: enough baseline days, ties, a zero-variance baseline, thin and missing
baselines, every confidence band, sparse metrics, a cancelling series, half-hour and tied clock offsets) and the
rows ``_extend_for_coverage`` adds so that the coverage ledger meets every branch (see its docstring) and the
rows ``_extend_for_health`` adds (runs and provenance messages to redact). The v1 store
is the same data under the schema-v1 migration alone. ``synthetic-live.hbdb`` is the synthetic store plus three
live-link session files imported through ``sources.import_path`` (``live_files``: one spans midnight, two
overlap a day that has monitoring rows), so the fold's ``live`` rows are in the differential. The clock is pinned with ``DISCONECT_NOW`` so the
``imported_at`` stamps are the same every time. ``tools/serve_diff.py`` produces the oracle file.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "serve"
STORE = FIXTURES / "synthetic.hbdb"
STORE_V1 = FIXTURES / "synthetic-v1.hbdb"
STORE_EMPTY = FIXTURES / "empty.hbdb"
STORE_LIVE = FIXTURES / "synthetic-live.hbdb"
SCRIPT = FIXTURES / "script.json"
PINNED_NOW = "2025-07-02T09:30:00Z"
#: Further clock pins the live gate is run under (``serve_diff.py --now``): the watch ahead of UTC,
#: the watch behind UTC (a negative offset), and years later.
OTHER_NOWS = ("2025-07-02T21:00:00Z", "2025-03-06T02:00:00Z", "2031-01-01T00:00:00Z")


AS_OF = datetime.date(2025, 6, 30)   # the store's latest date: ``data.facts`` windows end here


def _day(back: int) -> str:
    """The date ``back`` days before the store's latest date."""
    return (AS_OF - datetime.timedelta(days=back)).isoformat()


def _series(metric: str, scope: str, values_by_back: dict[int, float]) -> list[tuple]:
    """``daily_metrics`` rows (date, metric, value, scope, device) for ``back`` days before ``AS_OF``."""
    return [(_day(back), metric, value, scope, "7") for back, value in sorted(values_by_back.items(), reverse=True)]


def _daily_rows() -> tuple[list[tuple], list[tuple[str, str]]]:
    """The synthetic daily series and the (metric, scope) pairs they replace. Window = back 0..6, baseline = 7..34."""
    rows: list[tuple] = []
    # ordinary: 42 days of two-decimal-free floats; the window sits 1.7 above the baseline
    rows += _series("resting_heart_rate", "device",
                    {back: round(50 + ((back * 37) % 11) / 3 + (1.7 if back < 7 else 0.0), 6) for back in range(42)})
    # a tie in the means: baseline alternates 9000/11000 (mean exactly 10000), the window is 10000 each day
    rows += _series("steps", "local", {back: 10000.0 if back < 7 else (9000.0 if back % 2 else 11000.0)
                                       for back in range(35)})
    # a delta below 1e-9 that is negative: fmean of three 0.1 is one ulp above 0.1, of seven exactly 0.1
    rows += _series("calories_active", "vendor_cloud", {back: 0.1 for back in [*range(7), 7, 8, 9]})
    # a zero-variance baseline with a real change
    rows += _series("heart_rate_max", "vendor_cloud", {back: 160.0 if back < 7 else 150.0 for back in range(35)})
    # every confidence band: baseline days with data 8 (high), 7 (medium), 5 (medium), 4 (low), 3 (low)
    rows += _series("stress_avg", "local", {back: 31.5 + back % 3 for back in [0, 1, *range(7, 15)]})
    rows += _series("training_load_acute", "device", {back: 200.5 + back for back in [2, *range(10, 17)]})
    rows += _series("sleep_duration", "device", {back: 27000.0 + 60 * back for back in [0, 3, 9, 11, 14, 20, 21]})
    rows += _series("hydration_ml", "device", {back: 1800.0 - back for back in [1, 8, 12, 20, 30]})
    # sparse metrics: thin baseline (2 days), no baseline (window only), one baseline day short of the minimum
    rows += _series("vo2max", "device", {5: 49.5, 13: 49.0, 22: 48.5})
    rows += _series("weight_kg", "device", {3: 71.2, 9: 71.6, 18: 71.4})
    rows += _series("endurance_score", "device", {1: 6100.0, 4: 6150.0})
    rows += _series("fitness_age", "device", {2: 31.0, 40: 33.0})
    # the same series in two scopes: equal facts side by side
    rows += _series("sleep_score", "vendor_cloud", {back: 70 + (30 - back) for back in range(30)})
    # sums that differ from a plain loop: many-decimal values, then a cancelling series
    rows += _series("calories_total", "vendor_cloud", {back: round(2400 + (back * 7919 % 1000) / 7.0, 9)
                                                       for back in range(35)})
    rows += _series("recovery_time", "vendor_cloud", {0: 1.0, 1: 2.0, 8: 1e16, 9: 1.0, 10: -1e16, 11: 1.0, 12: 0.25})
    replaced = sorted({(row[1], row[3]) for row in rows})
    return rows, replaced


def _sample_rows() -> list[tuple]:
    """Sample rows (metric, ts_utc, value, scope, device). The watch runs UTC+3, so 22:30Z is the next local day."""
    rows: list[tuple] = []
    for back in range(40):
        day = AS_OF - datetime.timedelta(days=back)
        for hour, minute in ((10, 0), (12, 0), (22, 30)):
            value = round(60 + ((back * 13 + hour) % 17) * 0.37 + (4.1 if back < 7 else 0.0), 6)
            rows.append(("heart_rate", f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z", value, "device", "7"))
    # a day whose mean is a tie at two decimals: 60.125 rounds half to even
    rows += [("spo2", f"{_day(3)}T08:00:00Z", 60.0, "device", "7"), ("spo2", f"{_day(3)}T09:00:00Z", 60.25, "device", "7")]
    rows += [("spo2", f"{_day(back)}T08:00:00Z", 95.0 + back % 4, "device", "7") for back in (9, 12, 15, 18, 21)]
    # respiration around the midpoint between two clock offsets (a tie goes to the earlier offset), in two scopes
    for back in range(8, 16):
        rows.append(("respiration_rate", f"{_day(back)}T10:30:00Z", 14.0 + back * 0.1, "device", "7"))
        rows.append(("respiration_rate", f"{_day(back)}T20:30:00Z", 15.5 - back * 0.05, "device", "7"))
    rows += [("respiration_rate", f"{_day(back)}T09:00:00Z", 13.0 + back, "local", "7") for back in (0, 1, 8, 9, 10, 11)]
    rows += [("respiration_rate", f"{_day(back)}T09:00:00Z", 16.0 - back * 0.5, "vendor_cloud", "7") for back in (2, 9, 10, 11)]
    return rows


def _extend_for_facts(conn) -> None:
    """Replace the series the facts differential needs; everything else the import wrote stays as it was."""
    daily, replaced = _daily_rows()
    for metric, scope in replaced:
        conn.execute("DELETE FROM daily_metrics WHERE metric=? AND source_scope=?", (metric, scope))
    conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                     "VALUES(?,?,?,?,?,1)", daily)
    conn.execute("DELETE FROM metric_samples WHERE metric IN ('heart_rate', 'spo2', 'respiration_rate')")
    conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                     "VALUES(?,?,?,?,?,1)", _sample_rows())
    # a half-hour zone from the 20th on: local days after the midpoint (17 June, 10:30Z) follow it, and the
    # respiration samples at exactly 10:30Z on that day sit on the tie between the two offsets
    conn.execute("INSERT OR REPLACE INTO clock_offsets(ts_utc, offset_s, device_id, raw_record_id) "
                 "VALUES('2025-06-20T00:00:00Z', 12600, '7', 1)")


# ---- the coverage scenarios ----

_STAMP = "2025-07-01T00:00:00Z"


def _raw(conn, stream: str, start: str | None, end: str | None, scope: str = "device") -> int:
    """One retained record with a span (``source_key`` is unique per stream and start)."""
    cursor = conn.execute(
        "INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
        "payload_hash, payload_bytes, start_utc, end_utc, imported_at) VALUES(?,?,?,'usb','fit',x'00','h',1,?,?,?)",
        (stream, f"{stream}|{start}|{end}", scope, start, end, _STAMP))
    return cursor.lastrowid


def _daily(conn, raw: int, rows: list[tuple]) -> None:
    """``daily_metrics`` rows (date, metric, value, scope, device) that point at ``raw``."""
    conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                     "VALUES(?,?,?,?,?,?)", [(*row, raw) for row in rows])


def _samples(conn, raw: int, rows: list[tuple]) -> None:
    """``metric_samples`` rows (metric, ts_utc, value, scope, device) that point at ``raw``."""
    conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                     "VALUES(?,?,?,?,?,?)", [(*row, raw) for row in rows])


def _extend_for_coverage(conn) -> None:
    """Rows that make every branch of the coverage ledger answer something different.

    Three clock regions, far from the June data (the nearest stated offset decides): until mid-February
    a half-hour zone (+05:30, local midnight at 18:30Z, so an hour of samples holds the midnight and the
    per-sample path runs), until 21 March a negative offset (-05:00, midnight at 05:00Z), then a positive
    one (+02:00, midnight at 22:00Z). Files end exactly at a local midnight, so the exclusive end (minus one
    microsecond) decides whether the next day is covered. Also here: export ranges with and without rows
    (``source_empty`` beside ``present``), failures with their own span, with the span of a retained file,
    crossing midnight, and one on a day that has a row (``present`` wins), nightly streams that inherit
    the all-day files' coverage, a sparse metric, a record whose end precedes its start, an empty span,
    two devices on one day, an undeclared stream (map drift) and a series with more than ``MAX_GAPS`` gaps.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO clock_offsets(ts_utc, offset_s, device_id, raw_record_id) VALUES(?,?,?,1)",
        [("2025-02-01T00:00:00Z", 19800, "9"), ("2025-03-01T00:00:00Z", -18000, "8"),
         ("2025-04-10T00:00:00Z", 7200, "7")])
    conn.execute("INSERT OR IGNORE INTO import_runs(id, started_at, transport, status) VALUES(3, ?, 'usb', 'ok')",
                 (_STAMP,))

    # half-hour region: files end at local midnight (18:30Z); samples straddle it in one UTC hour
    h1 = _raw(conn, "fit:monitoring_b", "2025-02-02T18:30:00Z", "2025-02-05T18:30:00Z")
    h2 = _raw(conn, "fit:monitoring_b", "2025-02-08T18:30:00Z", "2025-02-09T18:30:00Z")
    _samples(conn, h1, [("heart_rate", "2025-02-05T18:29:59Z", 61.5, "device", "7"),
                        ("heart_rate", "2025-02-05T18:30:00Z", 62.5, "device", "7")])
    _samples(conn, h2, [("heart_rate", "2025-02-09T18:29:59Z", 63.0, "device", "7")])
    _daily(conn, h1, [("2025-02-03", "steps", 4100.0, "local", "7"), ("2025-02-04", "steps", 5200.0, "local", "7")])

    # negative region
    n1 = _raw(conn, "fit:monitoring_b", "2025-03-03T05:00:00Z", "2025-03-07T05:00:00Z")
    n2 = _raw(conn, "fit:monitoring_b", "2025-03-10T05:00:00Z", "2025-03-13T05:00:00Z")
    _samples(conn, n1, [("heart_rate", "2025-03-04T04:59:59Z", 58.0, "device", "7"),
                        ("heart_rate", "2025-03-04T05:00:00Z", 59.0, "device", "7"),
                        ("stress", "2025-03-05T10:00:00Z", 31.0, "device", "7"),
                        ("stress", "2025-03-05T11:00:00Z", 33.25, "device", "7")])
    _daily(conn, n1, [("2025-03-03", "steps", 7000.0, "local", "7"), ("2025-03-04", "steps", 8100.5, "local", "7"),
                      ("2025-03-05", "steps", 6400.0, "local", "7"),
                      ("2025-03-03", "resting_heart_rate", 52.0, "device", "7"),
                      ("2025-03-03", "resting_heart_rate", 55.5, "device", "8"),   # two devices, one day
                      ("2025-03-05", "resting_heart_rate", 51.0, "device", "7"),
                      ("2025-03-05", "steps", 1500.0, "device", "7")])             # fit:monitoring_b is not declared for it
    _daily(conn, n2, [("2025-03-10", "steps", 7700.0, "local", "7")])
    _samples(conn, n2, [("stress", "2025-03-11T12:00:00Z", 28.0, "local", "7")])    # an undeclared (metric, scope, stream)
    sleep = _raw(conn, "fit:sleep", "2025-03-04T03:00:00Z", "2025-03-04T11:00:00Z")
    _daily(conn, sleep, [("2025-03-04", "sleep_score", 77.0, "device", "7")])
    # failures: a span of its own; one crossing local midnight; a day that also has a row; a retained file's span
    conn.executemany(
        "INSERT INTO import_failures(run_id, stream, start_utc, end_utc, raw_record_id, payload_hash, kind, recorded_at) "
        "VALUES(3, ?, ?, ?, ?, ?, 'decode', ?)",
        [("fit:monitoring_b", "2025-03-08T14:00:00Z", "2025-03-08T20:00:00Z", None, "f1", _STAMP),
         ("fit:monitoring_b", "2025-03-14T04:00:00Z", "2025-03-14T06:00:00Z", None, "f2", _STAMP),
         ("fit:monitoring_b", "2025-03-04T10:00:00Z", "2025-03-04T12:00:00Z", None, "f3", _STAMP)])
    kept = _raw(conn, "fit:monitoring_b", "2025-03-16T12:00:00Z", "2025-03-17T03:00:00Z")
    conn.execute("INSERT INTO import_failures(run_id, stream, start_utc, end_utc, raw_record_id, payload_hash, kind, "
                 "recorded_at) VALUES(3, 'fit:monitoring_b', NULL, NULL, ?, 'f4', 'load', ?)", (kept, _STAMP))
    conn.execute("INSERT INTO import_failures(run_id, stream, start_utc, end_utc, kind, recorded_at) "
                 "VALUES(3, NULL, '2025-03-15T00:00:00Z', '2025-03-15T05:00:00Z', 'decode', ?)", (_STAMP,))
    _raw(conn, "fit:monitoring_b", "2025-03-18T05:00:00Z", "2025-03-17T05:00:00Z")   # end before start
    _raw(conn, "fit:monitoring_b", "2025-03-19T12:00:00Z", "2025-03-19T12:00:00Z")   # an empty span

    # positive region: export windows claimed with and without rows, files inside and across midnight, a sparse metric
    conn.executemany("INSERT OR IGNORE INTO export_ranges(run_id, stream, from_day, to_day) VALUES(3,?,?,?)",
                     [("json:uds", "2025-04-01", "2025-04-10"), ("json:sleep", "2025-04-05", "2025-04-06")])
    uds = _raw(conn, "json:uds", "2025-04-02T00:00:00Z", "2025-04-02T00:00:00Z", "vendor_cloud")
    _daily(conn, uds, [("2025-04-02", "steps", 9100.0, "vendor_cloud", None), ("2025-04-03", "steps", 8800.0, "vendor_cloud", None)])
    cloud_sleep = _raw(conn, "json:sleep", "2025-04-05T00:00:00Z", "2025-04-05T00:00:00Z", "vendor_cloud")
    _daily(conn, cloud_sleep, [("2025-04-05", "sleep_score", 81.0, "vendor_cloud", None)])
    p1 = _raw(conn, "fit:monitoring_b", "2025-04-14T22:00:00Z", "2025-04-15T22:00:00Z")
    p2 = _raw(conn, "fit:monitoring_b", "2025-04-20T10:00:00Z", "2025-04-21T10:00:00Z")
    _daily(conn, p1, [("2025-04-15", "steps", 6600.0, "local", "7")])
    _samples(conn, p2, [("heart_rate", "2025-04-20T23:30:00Z", 64.0, "device", "7")])
    metrics = _raw(conn, "fit:metrics", "2025-04-15T12:00:00Z", "2025-04-15T12:00:00Z")
    _daily(conn, metrics, [("2025-04-15", "vo2max", 50.5, "device", "7")])

    # more than MAX_GAPS gaps: a value every other day for 58 days, with only the first day's file retained
    gap_source = _raw(conn, "json:uds", "2025-01-02T00:00:00Z", "2025-01-02T00:00:00Z", "vendor_cloud")
    start = datetime.date(2025, 1, 2)
    _daily(conn, gap_source, [((start + datetime.timedelta(days=2 * k)).isoformat(), "intensity_minutes_moderate",
                               float(10 + k), "vendor_cloud", None) for k in range(29)])


def _extend_for_health(conn) -> None:
    """Rows that make ``data.health`` and ``import.last`` meet their redaction and limit branches: more
    runs than the five that are reported (newest first), error texts holding an e-mail address, a path and
    a long identifier, an empty error (not null), a running run, two ``ble`` sweeps (only the newest is
    listed, and it does not displace a run of another transport), and provenance messages to redact."""
    runs = [
        (4, "2025-07-01T01:00:00Z", "2025-07-01T01:00:05Z", "connect_export", "failed", 9, 0, 0, 9, 0,
         "OSError: cannot read /Users/someone/me@example.com/export.zip for account 12345678901"),
        (5, "2025-07-01T02:00:00Z", "2025-07-01T02:00:05Z", "usb", "partial", 4, 3, 0, 1, 1200, ""),
        (6, "2025-07-01T03:00:00Z", None, "drop", "running", 0, 0, 0, 0, 0, None),
        (7, "2025-07-01T04:00:00Z", "2025-07-01T04:00:01Z", "usb", "ok", 2, 2, 0, 0, 40, None),
        (8, "2025-07-01T05:00:00Z", "2025-07-01T05:00:09Z", "connect_export", "failed", 1, 0, 0, 1, 0,
         "C:\\Users\\someone\\export.zip: call someone@example.org about 9988776"),
        (9, "2025-07-01T06:00:00Z", "2025-07-01T06:00:01Z", "ble", "ok", 3, 0, 3, 0, 0, None),
        (10, "2025-07-01T07:00:00Z", "2025-07-01T07:00:01Z", "ble", "ok", 4, 1, 3, 0, 12, None),
    ]
    conn.executemany(
        "INSERT INTO import_runs(id, started_at, finished_at, transport, status, files_seen, files_imported, "
        "files_duplicate, files_failed, records_written, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)", runs)
    streams = [row[0] for row in conn.execute("SELECT stream FROM stream_provenance ORDER BY stream")]
    assert len(streams) >= 3, "the import left provenance rows"
    conn.execute("UPDATE stream_provenance SET last_parse_error_at=?, last_parse_error_kind='decode', "
                 "last_parse_error_message=?, files_failed=2 WHERE stream=?",
                 (_STAMP, "bad file /Users/someone/me@example.com/x.fit near byte 1234567", streams[0]))
    conn.execute("UPDATE stream_provenance SET last_write_error_at=?, last_write_error_kind='storage', "
                 "last_write_error_message=?, last_parse_error_message='' WHERE stream=?",
                 (_STAMP, "disk full at /var/data/hearthbeat/store.db (123456789 bytes)", streams[1]))


def live_files(folder: pathlib.Path) -> None:
    """Write three invented live-link session files into ``folder`` (the readings are made up, not real).

    ``live-a`` runs 2025-06-20T23:55Z..2025-06-21T00:05Z: readings on both sides of a UTC midnight, ``t`` an int
    and a float in turn. ``live-b`` runs on 2025-06-15 10:00Z.., a day that has monitoring rows for the same metrics.
    ``live-c`` is ``live-b`` again, longer (bet 9b-2: the fold's de-duplication, median and sentinel cases).
    """
    midnight = int(datetime.datetime(2025, 6, 21, tzinfo=datetime.timezone.utc).timestamp())
    first = [{"status": "scanning"}]
    for step, offset in enumerate(range(-300, 301, 60)):
        t = midnight + offset if step % 2 == 0 else float(midnight + offset) + 0.5
        first.append({"t": t, "metric": "heart_rate", "value": 60 + step})
        first.append({"t": t, "metric": "steps", "value": 10 * step})
    first.append({"t": midnight - 30, "metric": "respiration_rate", "value": 14})
    first.append({"t": midnight + 30, "metric": "stress", "value": 25})
    first.append({"status": "stopped", "stop": "LinkClosed"})
    start = int(datetime.datetime(2025, 6, 15, 10, tzinfo=datetime.timezone.utc).timestamp())
    second = [{"status": "scanning"}]
    for step in range(10):
        t = start + 60 * step
        second.append({"t": t, "metric": "heart_rate", "value": 90 + step})
        second.append({"t": float(t) + 0.25, "metric": "steps", "value": 5 * step})
    second.append({"t": start + 5, "metric": "stress", "value": 40})
    second.append({"t": start + 6, "metric": "respiration_rate", "value": 16})
    second.append({"t": start + 7, "metric": "spo2", "value": 97})
    second.append({"t": start + 8, "metric": "energy_reserve", "value": 55})
    second.append({"status": "stopped", "stop": "LinkClosed"})
    # live-c: the same session as live-b stored again in full plus four more minutes (the partial-then-full
    # case the fold de-duplicates), a minute holding two different heart-rate values (the lower median
    # decides), and two sentinels the fold drops.
    third = [line for line in second if "status" not in line]
    for step in range(10, 14):
        t = start + 60 * step
        third.append({"t": t, "metric": "heart_rate", "value": 100 + step})
        third.append({"t": float(t) + 0.25, "metric": "steps", "value": 5 * step})
    third.append({"t": start + 60 * 11 + 30, "metric": "heart_rate", "value": 120})
    third.append({"t": start + 60 * 12 + 1, "metric": "stress", "value": -1})
    third.append({"t": start + 60 * 12 + 2, "metric": "spo2", "value": 0})
    third.append({"status": "stopped", "stop": "LinkClosed"})
    for name, lines in (("live-20250620T235500Z.jsonl", first), ("live-20250615T100000Z.jsonl", second),
                        ("live-20250615T100001Z.jsonl", third)):
        (folder / name).write_text("".join(json.dumps(line) + "\n" for line in lines))


def build(target: pathlib.Path, live: bool = False) -> None:
    """Write the synthetic store at ``target`` as one plain file (WAL mode header, no -wal left over).

    ``live`` also imports the three ``live_files`` (the ``synthetic-live`` store)."""
    os.environ["DISCONECT_NOW"] = PINNED_NOW
    from disconect import storage
    from disconect.ingest import sources
    from test_import import _build_export
    from test_privacy import _seed

    with tempfile.TemporaryDirectory() as folder:
        work = pathlib.Path(folder)
        db_path = work / "synthetic.hbdb"
        _seed(db_path, live=False)
        export = work / "export"
        export.mkdir()
        _build_export(export)
        with storage.open_for_write(db_path, "fixture") as conn:
            sources.import_path(export, conn)
            _extend_for_facts(conn)
            _extend_for_coverage(conn)
            _extend_for_health(conn)
            if live:
                sessions = work / "live"
                sessions.mkdir()
                live_files(sessions)
                stats = sources.import_path(sessions, conn)
                assert (stats.files_imported, stats.files_failed) == (3, 0), stats
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for leftover in db_path.parent.glob("synthetic.hbdb-*"):
            assert leftover.stat().st_size == 0, f"{leftover.name} still holds data"
        shutil.copyfile(db_path, target)


def build_empty(target: pathlib.Path) -> None:
    """A migrated store that never imported anything (``never_imported``, no runs, no provenance)."""
    os.environ["DISCONECT_NOW"] = PINNED_NOW
    from disconect import storage

    with tempfile.TemporaryDirectory() as folder:
        db_path = pathlib.Path(folder) / "empty.hbdb"
        with storage.open_for_write(db_path, "fixture") as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for leftover in db_path.parent.glob("empty.hbdb-*"):
            assert leftover.stat().st_size == 0, f"{leftover.name} still holds data"
        shutil.copyfile(db_path, target)


def build_v1(source: pathlib.Path, target: pathlib.Path) -> None:
    """The same data under the schema-v1 migration alone: no ``export_ranges`` and no ``import_failures``,
    so the ledger reports ``refinements_available`` false. Plain SQLite in rollback-journal mode."""
    from disconect.storage import migrations, sqlite

    version, ddl = migrations.MIGRATIONS[0]
    assert version == 1
    with tempfile.TemporaryDirectory() as folder:
        work = pathlib.Path(folder)
        shutil.copyfile(source, work / "source.hbdb")
        conn = sqlite.connect(str(work / "v1.hbdb"))
        conn.executescript(ddl)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)", (PINNED_NOW,))
        conn.execute("PRAGMA user_version = 1")
        conn.execute("ATTACH DATABASE ? AS syn", (str(work / "source.hbdb"),))
        tables = [name for (name,) in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations'").fetchall()]
        for table in tables:
            mine = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
            theirs = [row[1] for row in conn.execute(f"PRAGMA syn.table_info({table})")]
            assert mine == theirs, f"{table}: v1 columns differ from the migrated store"
            conn.execute(f"INSERT INTO main.{table} SELECT * FROM syn.{table}")
        conn.commit()
        conn.execute("DETACH DATABASE syn")
        conn.close()
        assert not (work / "v1.hbdb-journal").exists()
        shutil.copyfile(work / "v1.hbdb", target)


# ---- the metric / today requests ----

#: No request is ``booked`` any more: the last named allowance (``kb23-fromisoformat-permissive``) was
#: retired on 2026-10-03 when ``serve`` ``last_day`` became strict ``YYYY-MM-DD`` on both cores; the compact
#: and week forms below are now plain bad parameters, answered alike.
FOCUS_METRICS = ("heart_rate", "stress", "spo2", "steps", "sleep_score", "resting_heart_rate",
                 "intensity_minutes_moderate", "vo2max")
BIG_METRICS = ("heart_rate", "steps", "sleep_score", "vo2max")
LAST_DAYS = (None, "$MID", "$FIRST", "$BEFORE", "2099-12-31")
BAD_LAST_DAYS = ("20261003", "2026-W40-6", "nonsense", "", "2025-13-40", "2025-02-30", "0000-01-01",
                 "9999-12-31", "0001-01-03", "2025-6-30", " 2025-06-30", "\uff12\uff10\uff12\uff15-06-30",
                 "2025-03-05T00:00", "2025-06-30\n")


def _request(request_id: int, name: str, **params) -> dict:
    return {"name": name, "send": {"id": request_id, "method": "data.metric", "params": params}}


def _health_entries() -> list[dict]:
    """``data.health`` over every window and every bad ``window_days`` (the clamp is 1 to 3650)."""
    entries: list[dict] = []

    def add(label: str, **params) -> None:
        entries.append({"name": f"gen: data.health {label}",
                        "send": {"id": 13000 + len(entries), "method": "data.health", "params": params}})

    entries.append({"name": "gen: data.health plain", "send": {"id": 13000, "method": "data.health"}})
    for window in (1, 2, 7, 30, 90, 180, 365, 3649, 3650, 3651, 10000, 0, -1, -3650, -99999):
        add(f"window_days={window}", window_days=window)
    for value in ("30", 30.5, 30.0, True, False, None, [30], {"a": 1}, "", [], {}):
        add(f"bad window_days: {value!r}", window_days=value)
    add("ignores unknown params", window_days=30, days=7, metric="steps")
    raw = {
        "window_days: 4300 digits (clamped)": '{"id":%d,"method":"data.health","params":{"window_days":$DIGITS4300}}',
        "window_days: negative 4300 digits": '{"id":%d,"method":"data.health","params":{"window_days":-$DIGITS4300}}',
        "window_days: beyond i64": '{"id":%d,"method":"data.health","params":{"window_days":9223372036854775808}}',
        "window_days: 4301 digits": '{"id":%d,"method":"data.health","params":{"window_days":$DIGITS4301}}',
        "window_days: exponent form": '{"id":%d,"method":"data.health","params":{"window_days":1e1}}',
        "window_days: NaN": '{"id":%d,"method":"data.health","params":{"window_days":NaN}}',
        "window_days: minus zero": '{"id":%d,"method":"data.health","params":{"window_days":-0}}',
        "params null": '{"id":%d,"method":"data.health","params":null}',
        "params array": '{"id":%d,"method":"data.health","params":[]}',
        "params missing": '{"id":%d,"method":"data.health"}',
        "duplicate window_days keeps the last": '{"id":%d,"method":"data.health","params":{"window_days":3,"window_days":40}}',
    }
    for label, template in raw.items():
        request_id = 13000 + len(entries)
        entries.append({"name": f"gen: data.health {label}", "raw": template % request_id})
    return entries


def _import_entries() -> list[dict]:
    """``import.last`` and the ``import.run`` requests that fail before the store is opened for writing
    (anything that reaches the worker changes the store, so ``serve_diff.py`` runs those on separate copies)."""
    entries = [
        {"name": "gen: import.last plain", "send": {"id": 14000, "method": "import.last"}},
        {"name": "gen: import.last ignores params", "send": {"id": 14001, "method": "import.last",
                                                              "params": {"limit": 1, "x": [1]}}},
        {"name": "gen: import.last params null", "raw": '{"id":14002,"method":"import.last","params":null}'},
        {"name": "gen: import.last params array", "raw": '{"id":14003,"method":"import.last","params":[]}'},
    ]

    def add(label: str, **params) -> None:
        entries.append({"name": f"gen: import.run {label}",
                        "send": {"id": 14100 + len(entries), "method": "import.run", "params": params}})

    for value in (None, "", 5, True, False, ["x"], {"a": 1}, 1.5):
        add(f"bad path: {value!r}", path=value, transport="usb")
    add("missing path, bad transport", transport="carrier pigeon")
    for value in (5, True, False, "", "Export", "usb ", "carrier pigeon", "gadgetbridge", ["export"], {"a": 1}, 1.5, 0):
        add(f"bad transport: {value!r}", path="x", transport=value)
    add("bad path before bad transport", path=5, transport=5)
    entries.append({"name": "gen: import.run params null", "raw": '{"id":14300,"method":"import.run","params":null}'})
    entries.append({"name": "gen: import.run params array", "raw": '{"id":14301,"method":"import.run","params":[]}'})
    entries.append({"name": "gen: import.run duplicate transport keeps the last",
                    "raw": '{"id":14302,"method":"import.run","params":{"path":"x","transport":"usb","transport":"nope"}}'})
    return entries


def _sync_entries() -> list[dict]:
    """``sync.status`` and ``sync.run`` on the plaintext oracle stores: no ``relay.json`` exists beside them, so
    ``sync.run`` answers ``not_found`` on both passes and ``sync.status`` is read-only. Everything that needs a
    relay (a bundle to pull, a push, events, a held write lock) runs in the harness's sync leg on encrypted
    copies, because these stores cannot sync and the oracle replay has no hook between entries."""
    entries = [
        {"name": "gen: sync.status plain", "send": {"id": 16000, "method": "sync.status"}},
        {"name": "gen: sync.status ignores params", "send": {"id": 16001, "method": "sync.status",
                                                              "params": {"limit": 1, "x": [1]}}},
        {"name": "gen: sync.status params null", "raw": '{"id":16002,"method":"sync.status","params":null}'},
        {"name": "gen: sync.status params array", "raw": '{"id":16003,"method":"sync.status","params":[]}'},
        {"name": "gen: sync.run plain (no relay configured)", "send": {"id": 16100, "method": "sync.run"}},
        {"name": "gen: sync.run ignores params", "send": {"id": 16101, "method": "sync.run",
                                                           "params": {"relay": "x", "n": [1]}}},
        {"name": "gen: sync.run params null", "raw": '{"id":16102,"method":"sync.run","params":null}'},
        {"name": "gen: sync.run params array", "raw": '{"id":16103,"method":"sync.run","params":[]}'},
    ]
    return entries


#: The five relay/pair methods, with the params that reach their parameter checks; ids 16200 up.
_RELAY_METHODS = (("relay.addresses", None), ("relay.serve", {"on": False}),
                  ("pair.offer", {"listen": "192.168.1.20:24816"}), ("pair.confirm", {"digits": "123456"}),
                  ("pair.cancel", None))


def _relay_entries() -> list[dict]:
    """``relay.addresses``, ``relay.serve``, ``pair.offer``, ``pair.confirm`` and ``pair.cancel`` on the plaintext
    oracle stores: no ``relay.json`` exists beside them, so every entry answers ``not_found`` on both cores (the
    shared prefix's second check). The refusals that come after it run in the sync leg."""
    entries: list[dict] = []
    for index, (method, params) in enumerate(_RELAY_METHODS):
        base = 16200 + index * 10
        entries += [
            {"name": f"gen: {method} plain (no relay configured)",
             "send": {"id": base, "method": method, **({"params": params} if params else {})}},
            {"name": f"gen: {method} ignores params",
             "send": {"id": base + 1, "method": method, "params": {"limit": 1, "x": [1]}}},
            {"name": f"gen: {method} params null", "raw": '{"id":%d,"method":"%s","params":null}' % (base + 2, method)},
            {"name": f"gen: {method} params array", "raw": '{"id":%d,"method":"%s","params":[]}' % (base + 3, method)},
        ]
    return entries


def _tools_entries() -> list[dict]:
    """``tools.call``: each of the six tools with defaults and with every parameter, the day anchors, the clamps,
    every parameter badly typed (the cores' argument coercion must agree), the tool failures and the failures
    of the call itself. One entry per call; the ids are 17000 up."""
    from disconect import contract

    entries: list[dict] = []

    def add(label: str, name, arguments=None, **extra) -> None:
        params: dict = {} if name is _ABSENT else {"name": name}
        if arguments is not _ABSENT:
            params["arguments"] = arguments
        entries.append({"name": f"gen: tools.call {label}",
                        "send": {"id": 17000 + len(entries), "method": "tools.call", "params": params}, **extra})

    def raw(label: str, template: str) -> None:
        entries.append({"name": f"gen: tools.call {label}", "raw": template % (17000 + len(entries))})

    numeric = [item.metric for item in contract.METRICS]
    for tool in ("get_data_health", "get_metric_series", "get_sleep_detail", "list_activities", "get_period_facts",
                 "get_contract"):
        add(f"{tool} with its defaults", tool, {"metrics": ["steps"]} if tool == "get_metric_series" else {})
    add("get_data_health arguments absent", "get_data_health", _ABSENT)
    add("get_data_health arguments null", "get_data_health", None)
    for days in (1, 7, 30, 400, 3650, 3651, 0, -5, "7", 7.0, True, False):
        add(f"get_data_health window_days={days!r}", "get_data_health", {"window_days": days})
    add("get_metric_series two metrics and a scope", "get_metric_series",
        {"metrics": ["steps", "heart_rate"], "source_scope": "device", "days": 30, "end_date": "$MID"})
    add("get_metric_series every metric", "get_metric_series", {"metrics": numeric, "days": 60, "end_date": "$LAST"})
    for scope in (*contract.SOURCE_SCOPES, "cloud", ""):
        add(f"get_metric_series scope={scope!r}", "get_metric_series",
            {"metrics": ["steps", "heart_rate", "sleep_score", "resting_heart_rate"], "source_scope": scope,
             "days": 14, "end_date": "$MID"})
    for days in (1, 90, 366, 367, 1825, 1826, 0, -1, "30", 30.0):
        add(f"get_metric_series days={days!r}", "get_metric_series", {"metrics": ["steps", "heart_rate"], "days": days})
    add("get_metric_series unknown metric among known", "get_metric_series", {"metrics": ["steps", "nope"]})
    add("get_metric_series only unknown metrics", "get_metric_series", {"metrics": ["nope"]})
    add("get_metric_series no metrics", "get_metric_series", {"metrics": []})
    add("get_metric_series metrics as JSON text", "get_metric_series", {"metrics": '["steps", "heart_rate"]'})
    add("get_metric_series repeated metric", "get_metric_series", {"metrics": ["steps", "steps"]})
    for end_date in ("$FIRST", "$MID", "$LAST", "$BEFORE", "2025-6-1", "20250601", "2025-W23-1", "", "nonsense"):
        add(f"get_metric_series end_date={end_date!r}", "get_metric_series", {"metrics": ["steps"], "end_date": end_date})
    for day in (None, "$FIRST", "$MID", "$LAST", "$BEFORE", "2025-06-16", "2025-6-16", "20250616", "2025-W25-1",
                "nonsense", "", " 2025-06-16"):
        add(f"get_sleep_detail date={day!r}", "get_sleep_detail", {"date": day})
    for limit in (None, 1, 3, "3", 3.0, 200, 201, 0, -1, True, False, 2.5, "abc", [], {}):
        add(f"list_activities limit={limit!r}", "list_activities", {"limit": limit})
    add("get_period_facts include_points", "get_period_facts", {"include_points": True})
    add("get_period_facts include_points as text", "get_period_facts", {"include_points": "true"})
    add("get_period_facts unknown metric", "get_period_facts", {"metrics": ["nope"]})
    add("get_period_facts known and unknown metrics", "get_period_facts", {"metrics": ["steps", "nope"]})
    add("get_period_facts empty metrics", "get_period_facts", {"metrics": []})
    add("get_period_facts every metric, points", "get_period_facts",
        {"metrics": numeric, "include_points": True, "end_date": "$MID"})
    for window, baseline in ((7, 28), (1, 1), (31, 365), (32, 366), (0, 0), (-1, -1), (14, 7)):
        add(f"get_period_facts window={window} baseline={baseline}", "get_period_facts",
            {"window_days": window, "baseline_days": baseline})
    for scope in (*contract.SOURCE_SCOPES, "cloud"):
        add(f"get_period_facts scope={scope!r}", "get_period_facts", {"source_scope": scope, "end_date": "$MID"})
    for end_date in ("$FIRST", "$BEFORE", "2025-6-1", "20250601", "", "nonsense"):
        add(f"get_period_facts end_date={end_date!r}", "get_period_facts", {"end_date": end_date})
    add("get_contract ignores surplus arguments", "get_contract", {"x": 1, "y": [2]})
    add("get_sleep_detail ignores surplus arguments", "get_sleep_detail", {"x": 1})
    # every parameter, badly typed
    kinds = {
        "get_data_health": ("window_days",),
        "get_metric_series": ("metrics", "days", "source_scope", "end_date"),
        "get_sleep_detail": ("date",),
        "list_activities": ("limit",),
        "get_period_facts": ("window_days", "baseline_days", "end_date", "metrics", "source_scope", "include_points"),
    }
    good = {"metrics": ["steps"]}
    for tool, params in kinds.items():
        for param in params:
            for value in (None, 5, 2.5, "x", "5", [], ["x"], [1], {}, {"a": 1}, True, "true", "[]", "null"):
                add(f"{tool} {param}={value!r}", tool, {**(good if tool == "get_metric_series" else {}), param: value})
    add("get_metric_series metrics missing", "get_metric_series", {})
    add("several parameters rejected at once", "get_period_facts",
        {"window_days": "x", "baseline_days": [], "end_date": 5, "metrics": 7, "source_scope": [], "include_points": 3})
    add("rejected parameters and an unknown one", "get_metric_series", {"days": "x", "surplus": 1})
    # the call itself
    add("unknown tool", "no_such_tool", {})
    add("unknown tool, arguments absent", "no_such_tool", _ABSENT)
    add("unknown tool, arguments not an object", "no_such_tool", [])
    add("tool name differs in case", "Get_Contract", {})
    add("tool name with a trailing space", "get_contract ", {})
    add("an MCP method name is not a tool", "tools/list", {})
    add("a serve method name is not a tool", "data.health", {})
    add("tool name is a quote and an apostrophe", "it's \"x\"", {})
    for name in (_ABSENT, None, 5, True, "", ["get_contract"], {"a": 1}, 1.5):
        add(f"bad name: {'<absent>' if name is _ABSENT else repr(name)}", name, {})
    for arguments in ([], [1], "{}", "x", 5, 1.5, True, False):
        add(f"arguments not an object: {arguments!r}", "get_contract", arguments)
    add("name first, then arguments", 5, [])
    add("arguments checked before the tool name", "no_such_tool", 5)
    raw("params null", '{"id":%d,"method":"tools.call","params":null}')
    raw("params array", '{"id":%d,"method":"tools.call","params":[]}')
    raw("params missing", '{"id":%d,"method":"tools.call"}')
    raw("duplicate keys keep the last",
        '{"id":%d,"method":"tools.call","params":{"name":"nope","name":"get_contract","arguments":{"x":1},"arguments":{}}}')
    raw("limit: 4300 digits", '{"id":%d,"method":"tools.call","params":{"name":"list_activities","arguments":{"limit":$DIGITS4300}}}')
    raw("limit: beyond i64", '{"id":%d,"method":"tools.call","params":{"name":"list_activities","arguments":{"limit":9223372036854775808}}}')
    raw("window_days: negative beyond i64",
        '{"id":%d,"method":"tools.call","params":{"name":"get_data_health","arguments":{"window_days":-9223372036854775809}}}')
    raw("limit: exponent form", '{"id":%d,"method":"tools.call","params":{"name":"list_activities","arguments":{"limit":1e1}}}')
    raw("limit: NaN", '{"id":%d,"method":"tools.call","params":{"name":"list_activities","arguments":{"limit":NaN}}}')
    raw("metrics: escapes", '{"id":%d,"method":"tools.call","params":{"name":"get_metric_series","arguments":{"metrics":["\\u0073teps"],"days":3}}}')
    return entries


_ABSENT = object()


def _metric_entries() -> list[dict]:
    """Every contract metric and scope, the window edges, ``last_day`` forms and every bad parameter."""
    from disconect import contract

    numeric = [item.metric for item in contract.METRICS]
    entries: list[dict] = []

    def scopes_for(metric: str) -> tuple[str, ...]:
        """A session scope (``live``) only for the metrics the contract declares in it; every other
        (metric, session scope) pair answers "absent" and one probe below covers that path."""
        return tuple(scope for scope in contract.SOURCE_SCOPES
                     if scope not in contract.SESSION_SCOPES or (metric, scope) in contract.SESSION_STREAMS_FOR)

    def add(label: str, **params) -> None:
        entries.append(_request(10000 + len(entries), f"gen: data.metric {label}", **params))

    def tagged(name: str, last_day) -> str:
        return f"{name} last={last_day if last_day is not None else 'absent'}"

    for metric in numeric:
        for scope in scopes_for(metric):
            add(f"{metric}/{scope} days=30 mid", metric=metric, scope=scope, days=30, last_day="$MID")
    for metric in numeric:
        for scope in scopes_for(metric):
            add(f"{metric}/{scope} days=60 last stored", metric=metric, scope=scope, days=60, last_day="$LAST")
    for metric in numeric:
        for scope in scopes_for(metric):
            add(f"{metric}/{scope} days=7 today", metric=metric, scope=scope, days=7)
    for metric in FOCUS_METRICS:
        for scope in scopes_for(metric):
            for days in (1, 7, 90, 0, -1):
                for last_day in LAST_DAYS:
                    params = {"metric": metric, "scope": scope, "days": days}
                    if last_day is not None:
                        params["last_day"] = last_day
                    add(tagged(f"{metric}/{scope} days={days}", last_day), **params)
    for metric in BIG_METRICS:
        for scope in scopes_for(metric):
            for days in (1825, 1826):
                for last_day in (None, "$MID"):
                    params = {"metric": metric, "scope": scope, "days": days}
                    if last_day is not None:
                        params["last_day"] = last_day
                    add(tagged(f"{metric}/{scope} days={days}", last_day), **params)
    for scope in contract.SESSION_SCOPES:   # a session scope asked for a metric it never carries: absent
        add(f"steps/{scope} days=7 today (not a session metric)", metric="steps", scope=scope, days=7)
    for last_day in BAD_LAST_DAYS:
        for metric in ("heart_rate", "steps", "nope", "hrv_status"):
            add(f"last_day={last_day!r} {metric}", metric=metric, scope="device", days=7, last_day=last_day)
    for label in contract.label_names():
        add(f"{label} is a label, not a numeric metric", metric=label, scope="device", days=7, last_day="$MID")
    for scope in contract.SOURCE_SCOPES:
        add(f"unknown metric/{scope}", metric="nope", scope=scope, last_day="$MID")
    add("steps with a scope that is not one", metric="steps", scope="cloud", last_day="$MID")
    # every parameter, badly
    good = {"metric": "steps", "scope": "local", "days": 7, "last_day": "$MID"}
    bad_values = {
        "metric": [None, "", 5, True, ["steps"], {"a": 1}, "it's", "Steps", "steps ", "nope", "caf\u00e9 \u0001", "x" * 40],
        "scope": [None, "", 5, True, ["local"], {"a": 1}, "it's", "Local", "local ", "cloud"],
        "days": ["7", 7.5, 7.0, True, False, None, [7], {"a": 1}],
        "last_day": [5, True, False, [], {}, 1.5],
    }
    for name, values in bad_values.items():
        for value in values:
            add(f"bad {name}: {value!r}", **{**good, name: value})
        add(f"missing {name}", **{key: val for key, val in good.items() if key != name})
    add("null last_day is absent", **{**good, "last_day": None})
    add("both text params bad (metric first)", metric=5, scope=5, days="x", last_day=5)
    add("days bad, last_day bad (days first)", metric="steps", scope="local", days="x", last_day=5)
    add("unknown metric, bad last_day (last_day first)", metric="nope", scope="local", last_day="nonsense")
    add("unknown metric, unknown scope (metric first)", metric="nope", scope="cloud", last_day="$MID")
    add("known metric, bad scope, bad last_day (last_day first)", metric="steps", scope="cloud", last_day="nonsense")
    add("bad scope, bad days (days first)", metric="steps", scope="cloud", days="x")
    raw = {
        "days: 4300 digits (clamped)": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":$DIGITS4300,"last_day":"$MID"}}',
        "days: negative 4300 digits": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":-$DIGITS4300,"last_day":"$MID"}}',
        "days: beyond i64": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":9223372036854775808}}',
        "days: 4301 digits": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":$DIGITS4301}}',
        "days: exponent form": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":1e1}}',
        "days: NaN": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","days":NaN}}',
        "params null": '{"id":%d,"method":"data.metric","params":null}',
        "params array": '{"id":%d,"method":"data.metric","params":[]}',
        "params missing": '{"id":%d,"method":"data.metric"}',
        "duplicate keys keep the last": '{"id":%d,"method":"data.metric","params":{"metric":"nope","metric":"steps","scope":"local","days":3,"days":4}}',
        "metric with a lone surrogate": '{"id":%d,"method":"data.metric","params":{"metric":"st\\ud800","scope":"local"}}',
        "scope with a lone surrogate": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"lo\\ud800"}}',
        "last_day with a lone surrogate": '{"id":%d,"method":"data.metric","params":{"metric":"steps","scope":"local","last_day":"\\ud800"}}',
        "metric with an apostrophe and a quote": '{"id":%d,"method":"data.metric","params":{"metric":"it\'s \\"x\\"","scope":"local"}}',
        "metric and scope as escapes": '{"id":%d,"method":"data.metric","params":{"metric":"\\u0073teps","scope":"\\u006cocal","days":2,"last_day":"$MID"}}',
    }
    for label, template in raw.items():
        request_id = 10000 + len(entries)
        entries.append({"name": f"gen: data.metric {label}", "raw": template % request_id})
    return entries


def _today_entries() -> list[dict]:
    return [
        {"name": "gen: data.today plain", "send": {"id": 12000, "method": "data.today"}},
        {"name": "gen: data.today ignores params", "send": {"id": 12001, "method": "data.today",
                                                            "params": {"metric": "x", "days": "y"}}},
        {"name": "gen: data.today params null", "raw": '{"id":12002,"method":"data.today","params":null}'},
        {"name": "gen: data.today params array", "raw": '{"id":12003,"method":"data.today","params":[]}'},
    ]


def _live_entries() -> list[dict]:
    """``data.live``: today, an anchored day, the live store's session days (empty on the other stores), the
    bad days and the malformed params. Ids 12200 up."""
    return [
        {"name": "gen: data.live plain", "send": {"id": 12200, "method": "data.live"}},
        {"name": "gen: data.live day mid", "send": {"id": 12201, "method": "data.live", "params": {"day": "$MID"}}},
        {"name": "gen: data.live overlapping session files", "send": {"id": 12202, "method": "data.live",
                                                                       "params": {"day": "2025-06-15"}}},
        {"name": "gen: data.live session over midnight", "send": {"id": 12203, "method": "data.live",
                                                                   "params": {"day": "2025-06-21"}}},
        {"name": "gen: data.live ignores extra params", "send": {"id": 12204, "method": "data.live",
                                                                  "params": {"day": "$LAST", "metric": "x"}}},
        {"name": "gen: data.live compact day", "send": {"id": 12205, "method": "data.live", "params": {"day": "20250615"}}},
        {"name": "gen: data.live day not a string", "send": {"id": 12206, "method": "data.live", "params": {"day": 5}}},
        {"name": "gen: data.live day empty", "send": {"id": 12207, "method": "data.live", "params": {"day": ""}}},
        {"name": "gen: data.live params null", "raw": '{"id":12208,"method":"data.live","params":null}'},
        {"name": "gen: data.live params array", "raw": '{"id":12209,"method":"data.live","params":[]}'},
    ]


def build_script(anchors: dict[str, str]) -> None:
    """Regenerate the ``gen:`` entries of ``script.json`` (every other entry is hand-written and kept)."""
    script = json.loads(SCRIPT.read_text())
    kept = [entry for entry in script["entries"] if not entry["name"].startswith("gen: ")]
    locked = [{"name": "gen: data.metric while locked",
               "send": {"id": 12100, "method": "data.metric", "params": {"metric": "steps", "scope": "local"}}},
              {"name": "gen: data.today while locked", "send": {"id": 12101, "method": "data.today"}},
              {"name": "gen: data.health while locked", "send": {"id": 12102, "method": "data.health"}},
              {"name": "gen: data.live while locked", "send": {"id": 12104, "method": "data.live"}},
              {"name": "gen: import.last while locked", "send": {"id": 12103, "method": "import.last"}},
              # locked on an encrypted store; plaintext stores are open, so these answer as unlocked ones do
              {"name": "gen: sync.status while locked", "send": {"id": 12105, "method": "sync.status"}},
              {"name": "gen: sync.run while locked", "send": {"id": 12106, "method": "sync.run"}},
              {"name": "gen: relay.addresses while locked", "send": {"id": 12110, "method": "relay.addresses"}},
              {"name": "gen: relay.serve while locked", "send": {"id": 12111, "method": "relay.serve", "params": {"on": False}}},
              {"name": "gen: pair.offer while locked",
               "send": {"id": 12112, "method": "pair.offer", "params": {"listen": "192.168.1.20:24816"}}},
              {"name": "gen: pair.confirm while locked",
               "send": {"id": 12113, "method": "pair.confirm", "params": {"digits": "123456"}}},
              {"name": "gen: pair.cancel while locked", "send": {"id": 12114, "method": "pair.cancel"}},
              # locked on an encrypted store; the tool's answer on a plaintext one
              {"name": "gen: tools.call while locked",
               "send": {"id": 12107, "method": "tools.call", "params": {"name": "get_contract", "arguments": {}}}},
              {"name": "gen: tools.call unknown tool while locked",
               "send": {"id": 12108, "method": "tools.call", "params": {"name": "no_such_tool"}}},
              {"name": "gen: tools.call bad name while locked",
               "send": {"id": 12109, "method": "tools.call", "params": {"name": 5}}},
              # locked on an encrypted store; bad_params (never opened for writing) on a plaintext one
              {"name": "gen: import.run while locked",
               "send": {"id": 12104, "method": "import.run", "params": {"path": "x", "transport": "carrier pigeon"}}}]
    entries: list[dict] = []
    for entry in kept:
        if entry["name"] == "import.last":
            entries += _metric_entries() + _today_entries() + _live_entries() + _health_entries() + _import_entries() + _sync_entries() + _relay_entries() + _tools_entries()
        entries.append(entry)
        if entry["name"] == "data.facts while locked":
            entries += locked
    script["entries"] = entries
    script["anchors"] = anchors
    script["other_nows"] = list(OTHER_NOWS)
    SCRIPT.write_text(json.dumps(script, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ---- the coverage ledger, as data ----

def ledger_cases(anchors: dict[str, str]) -> list[tuple[str, int]]:
    """(last_day, window_days): the whole history, a window with every status, a window inside the
    half-hour region, a one-day window, a window before any data, and the three clipping edges."""
    return [(anchors["$LAST"], 3650), ("2025-04-30", 120), (anchors["$MID"], 40), ("2025-02-06", 12),
            ("2025-03-20", 1), (anchors["$BEFORE"], 10), ("2025-06-30", 0), ("2025-06-30", -5),
            ("2025-06-30", 99999)]


def build_ledger(store: pathlib.Path, target: pathlib.Path, anchors: dict[str, str]) -> None:
    """``coverage.ledger`` of the store for each case, as gzip JSON (``data.health`` ports it in a later slice;
    until then the Rust ledger is held to these answers by ``tests/coverage_test.rs``)."""
    import gzip
    from disconect import coverage, storage

    with tempfile.TemporaryDirectory() as folder:
        copy = pathlib.Path(folder) / "store.db"
        shutil.copyfile(store, copy)
        conn = storage.open_read_only(copy)
        try:
            cases = [{"last_day": last_day, "window_days": window_days,
                      "result": coverage.ledger(conn, last_day, window_days)}
                     for last_day, window_days in ledger_cases(anchors)]
        finally:
            conn.close()
    text = json.dumps({"cases": cases}, sort_keys=True, ensure_ascii=True) + "\n"
    with target.open("wb") as handle, gzip.GzipFile("", "wb", 9, handle, mtime=0) as packed:
        packed.write(text.encode("ascii"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oracle", action="store_true", help="also rewrite the Python oracle responses")
    parser.add_argument("--keep-stores", action="store_true",
                        help="leave the committed .hbdb stores (and the ledgers built from them) as they are: SQLite "
                             "files are not byte-reproducible, the script and the oracles are")
    parser.add_argument("--live-only", action="store_true",
                        help="build only synthetic-live.hbdb (and, with --oracle, its oracle): the six older stores, "
                             "their ledgers and oracles are left as they are")
    args = parser.parse_args()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if not (args.keep_stores or args.live_only):
        build(STORE)
        build_v1(STORE, STORE_V1)
        build_empty(STORE_EMPTY)
    if not args.keep_stores:
        build(STORE_LIVE, live=True)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "tools"))
    import serve_diff
    anchors = serve_diff.anchors_for(STORE)
    assert anchors == serve_diff.anchors_for(STORE_V1)
    build_script(anchors)
    if not (args.keep_stores or args.live_only):
        build_ledger(STORE, FIXTURES / "ledger-synthetic.json.gz", anchors)
        build_ledger(STORE_V1, FIXTURES / "ledger-synthetic-v1.json.gz", anchors)
    for store in (STORE, STORE_V1, STORE_EMPTY, STORE_LIVE):
        print(f"wrote {store.name}: {store.stat().st_size} bytes")
    print(f"wrote {SCRIPT.name}: {len(json.loads(SCRIPT.read_text())['entries'])} entries")
    if args.oracle:
        import subprocess
        tool = pathlib.Path(__file__).resolve().parents[3] / "tools" / "serve_diff.py"
        oracles = ((STORE, "oracle-synthetic.jsonl.gz"), (STORE_V1, "oracle-synthetic-v1.jsonl.gz"),
                   (STORE_EMPTY, "oracle-empty.jsonl.gz"), (STORE_LIVE, "oracle-synthetic-live.jsonl.gz"))
        for store, oracle in oracles[3:] if args.live_only else oracles:
            subprocess.run([sys.executable, str(tool), "--python-only", "--db", str(store),
                            "--anchors-from", str(STORE), "--oracle-out", str(FIXTURES / oracle)], check=True)
