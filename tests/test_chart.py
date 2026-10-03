"""Charts: the honesty rules, and that a PNG of the asked-for size comes out.

Pixels are not compared - what is pinned is the behaviour a reader depends on:
gaps stay gaps, a sparse window is not averaged as if it were full, an empty
window draws a panel instead of raising, and a label has no line to draw.
"""

import datetime
import struct

import pytest

from disconect import chart, storage
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import ClockOffset

UTC = datetime.timezone.utc


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def _seed(db_path):
    """Three weeks of steps in two scopes with a hole, a night, and a day of stress."""
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                     "payload_hash, payload_bytes, imported_at) VALUES('fit:monitoring_b','k','device','usb','fit',"
                     "x'00','h',1,'2025-06-30T00:00:00Z')")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 15, 12, tzinfo=UTC), 10800)], "7", 1)
        daily = []
        for day in range(1, 22):
            date = f"2025-06-{day:02d}"
            if day in (8, 9, 10):
                continue  # a hole the line must not bridge
            daily.append((date, "steps", 6000 + day * 120, "device", "7"))
            daily.append((date, "steps", 6000 + day * 120 + 40, "vendor_cloud", None))
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,?,?,1)", daily)
        conn.execute("INSERT INTO daily_labels(date, metric, label, source_scope, device_id, raw_record_id) "
                     "VALUES('2025-06-20','hrv_status','balanced','device','7',1)")
        samples = [("stress", f"2025-06-19T{hour:02d}:{minute:02d}:00Z", 20.0 + hour)
                   for hour in range(0, 22) for minute in (0, 30)]
        conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,'device','7',1)", samples)
        conn.execute("INSERT INTO sleep_sessions(sleep_id, date, start_utc, end_utc, deep_s, light_s, rem_s, awake_s, "
                     "overall_score, source_scope, device_id, raw_record_id) VALUES('2025-06-19|device|7',"
                     "'2025-06-19','2025-06-18T21:00:00Z','2025-06-19T04:00:00Z',3600,10800,5400,900,82,"
                     "'device','7',1)")
        conn.executemany("INSERT INTO sleep_stages(sleep_id, stage, start_utc, end_utc) VALUES('2025-06-19|device|7',?,?,?)",
                         [("light", "2025-06-18T21:00:00Z", "2025-06-18T22:00:00Z"),
                          ("deep", "2025-06-18T22:00:00Z", "2025-06-18T23:00:00Z"),
                          ("rem", "2025-06-18T23:00:00Z", "2025-06-19T01:30:00Z"),
                          ("awake", "2025-06-19T01:30:00Z", "2025-06-19T01:45:00Z"),
                          ("light", "2025-06-19T01:45:00Z", "2025-06-19T04:00:00Z")])


def test_each_chart_renders_a_png_of_the_requested_size(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    metric = chart.metric_chart(conn, "steps", days=30, end_date="2025-06-21", width=900, height=400)
    assert _png_size(metric.to_png()) == (900, 400)
    night = chart.sleep_chart(conn, "2025-06-19", width=800, height=420)
    assert _png_size(night.to_png()) == (800, 420)
    day = chart.samples_chart(conn, "stress", "2025-06-19", width=800, height=360)
    assert _png_size(day.to_png()) == (800, 360)
    conn.close()


def test_charts_draw_a_panel_instead_of_raising_when_there_is_nothing(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    # A metric never stored, a night never slept, a day never worn.
    assert _png_size(chart.metric_chart(conn, "vo2max", end_date="2025-06-21").to_png())[0] == 1000
    assert _png_size(chart.sleep_chart(conn, "2025-01-01").to_png())[0] == 1000
    assert _png_size(chart.samples_chart(conn, "stress", "2025-01-01").to_png())[0] == 1000
    conn.close()


def test_a_label_and_an_unknown_name_have_no_line_to_draw(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    with pytest.raises(ValueError, match="not a contract metric"):
        chart.metric_chart(conn, "nope")
    with pytest.raises(ValueError, match="label"):
        chart.metric_chart(conn, "hrv_status")
    with pytest.raises(ValueError, match="not a sample-cadence metric"):
        chart.samples_chart(conn, "steps")
    conn.close()


def test_a_run_breaks_at_a_gap_rather_than_bridging_it():
    points = [(0, 1.0), (1, 2.0), (5, 3.0), (6, 4.0)]
    assert chart._runs(points, 1.5) == [[(0, 1.0), (1, 2.0)], [(5, 3.0), (6, 4.0)]]
    assert chart._runs(points, 10) == [points], "a wide tolerance keeps one run"
    assert chart._runs([], 1.5) == []


def test_the_trailing_mean_refuses_a_window_that_is_mostly_missing():
    day = datetime.date(2025, 6, 1)
    full = [(day + datetime.timedelta(days=n), float(n)) for n in range(10)]
    rolled = chart._trailing_mean(full, 5)
    assert rolled[0] == (day + datetime.timedelta(days=4), 2.0)
    assert len(rolled) == 6, "no mean before a full window has passed"
    sparse = [full[0], full[1], full[8], full[9]]
    dates = [date for date, _ in chart._trailing_mean(sparse, 5)]
    assert day + datetime.timedelta(days=8) not in dates, "2 of 5 days present is not a week"


def test_stage_rows_skip_sessions_that_stored_no_stages():
    rows = chart._stage_rows([{"source_scope": "device", "stages": [{"stage": "deep"}]},
                              {"source_scope": "vendor_cloud"}])
    assert [row["source_scope"] for row in rows] == ["device"]
