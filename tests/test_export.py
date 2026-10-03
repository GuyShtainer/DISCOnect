"""Exports: blanks for missing, filters honoured, one column per metric and scope."""

import csv
import io

import pytest

from disconect import export, storage


def _seed(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES(?,?,?,?)", [
            ("2025-06-15", "sleep_score", 80, "device"), ("2025-06-15", "sleep_score", 80, "vendor_cloud"),
            ("2025-06-16", "sleep_score", 70, "device"), ("2025-06-16", "steps", 5000, "local"),
            ("2025-06-17", "steps", 6000.123456, "local")])
        conn.execute("INSERT INTO daily_labels(date, metric, label, source_scope) "
                     "VALUES('2025-06-16','hrv_status','balanced','device')")
        conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope) VALUES(?,?,?,?)", [
            ("stress", "2025-06-15T10:00:00Z", 20, "device"), ("stress", "2025-06-16T10:00:00Z", 30, "device")])


def test_wide_csv_leaves_missing_cells_blank(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    rows = list(csv.reader(io.StringIO(export.daily_wide_csv(conn))))
    assert rows[0] == ["date", "hrv_status[device]", "sleep_score[device]", "sleep_score[vendor_cloud]", "steps[local]"]
    assert rows[1] == ["2025-06-15", "", "80", "80", ""]
    assert rows[2] == ["2025-06-16", "balanced", "70", "", "5000"]
    assert rows[3] == ["2025-06-17", "", "", "", "6000.123"]
    conn.close()


def test_long_rows_and_filters(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    rows = export.daily_long(conn, metrics=["sleep_score"], source_scope="device", start="2025-06-16")
    assert rows == [{"date": "2025-06-16", "metric": "sleep_score", "unit": "score", "source_scope": "device",
                     "value": 70}]
    text = export.daily_long_csv(conn, end="2025-06-15")
    assert text.splitlines()[0] == "date,metric,unit,source_scope,value" and len(text.splitlines()) == 3
    samples = export.samples_csv(conn, "stress", start="2025-06-16")
    assert samples.splitlines() == ["ts_utc,value,source_scope,unit", "2025-06-16T10:00:00Z,30,device,score"]
    with pytest.raises(ValueError):
        export.samples_csv(conn, "steps")
    conn.close()
