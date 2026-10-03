"""Data health: never-imported vs no-data, provenance, and the cross-source agreement check."""

from disconect import health, storage


def _seed(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                     "payload_hash, payload_bytes, imported_at) VALUES('fit:sleep','k','device','usb','fit',"
                     "x'00','h',1,'2025-07-01T00:00:00Z')")
        rows = [("2025-06-1%d" % d, "sleep_score", 70 + d, "device", "7") for d in range(5)]
        rows += [("2025-06-1%d" % d, "sleep_score", 70 + d + (3 if d == 4 else 0), "vendor_cloud", None) for d in range(5)]
        rows += [("2025-06-10", "steps", 8000, "local", "7"), ("2025-06-10", "steps", 8040, "vendor_cloud", None)]
        rows += [("2025-06-11", "vo2max", 45.0, "device", "7")]
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,?,?,1)", rows)


def test_empty_store_reports_never_imported(db_path):
    with storage.open_for_write(db_path, "test"):
        pass
    conn = storage.open_read_only(db_path)
    report = health.data_health(conn, 30)
    assert report["never_imported"] is True and report["source_agreement"] == []
    assert "Nothing imported yet" in health.summarize_for_humans(report)
    conn.close()


def test_source_agreement_counts_matches_within_tolerance(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    rows = {(r["metric"], r["scope_a"], r["scope_b"]): r for r in health.source_agreement(conn)}
    sleep = rows[("sleep_score", "device", "vendor_cloud")]
    assert sleep["days_compared"] == 5 and sleep["days_matching"] == 4
    assert sleep["max_abs_diff"] == 3 and sleep["median_abs_diff"] == 0 and sleep["mean_diff_a_minus_b"] == -0.6
    steps = rows[("steps", "local", "vendor_cloud")]
    assert steps["days_matching"] == 1, "40 steps on 8040 is within the 0.5 % tolerance"
    assert ("vo2max", "device", "vendor_cloud") not in rows, "a single-scope metric has nothing to compare"
    report = health.data_health(conn, 3650)
    assert report["source_agreement"] == health.source_agreement(conn)
    text = health.summarize_for_humans(report)
    assert "source agreement" in text and "4/5 days match" in text
    conn.close()
