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


def test_data_health_loads_the_clock_offsets_once(db_path, monkeypatch):
    """The live block, local_today and the coverage ledger share one load of clock_offsets (BL-9 review row;
    the Rust core passes the one load the same way)."""
    from disconect.ingest.clock import ClockOffsets
    loads = []
    real_load = ClockOffsets.load
    monkeypatch.setattr(ClockOffsets, "load", classmethod(lambda cls, conn: loads.append(1) or real_load(conn)))
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    report = health.data_health(conn, 30)
    conn.close()
    assert loads == [1]
    assert report["clock_offsets_known"] == 0 and report["coverage"]["ledger"]


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


def _runs(db_path, transports):
    with storage.open_for_write(db_path, "test") as conn:
        conn.executemany("INSERT INTO import_runs(id, started_at, transport, status) VALUES(?, '2025-07-01T00:00:00Z', ?, 'ok')",
                         list(enumerate(transports, start=1)))


def test_recent_imports_list_the_newest_sweep_beside_five_runs_of_the_other_transports(db_path):
    """A live link ends with a `ble` sweep of the readings folder; listed like any run, the sweeps would
    push the USB and export runs out of the five-row list within a day (9b review N8)."""
    _runs(db_path, ["usb", "connect_export", "ble", "usb", "ble", "drop", "usb", "ble", "usb", "ble", "ble"])
    conn = storage.open_read_only(db_path)
    runs = health.data_health(conn, 30)["recent_imports"]
    assert [run["id"] for run in runs] == [11, 9, 7, 6, 4, 2]
    assert [run["transport"] for run in runs] == ["ble", "usb", "usb", "drop", "usb", "connect_export"]
    conn.close()


def test_recent_imports_without_sweeps_are_the_newest_five_and_only_sweeps_are_one_row(db_path, tmp_path):
    _runs(db_path, ["usb"] * 7)
    conn = storage.open_read_only(db_path)
    assert [run["id"] for run in health.data_health(conn, 30)["recent_imports"]] == [7, 6, 5, 4, 3]
    conn.close()
    other = tmp_path / "sweeps.db"
    _runs(other, ["ble"] * 3)
    conn = storage.open_read_only(other)
    assert [run["id"] for run in health.data_health(conn, 30)["recent_imports"]] == [3]
    conn.close()
