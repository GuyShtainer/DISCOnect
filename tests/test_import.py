"""End to end: a synthetic Connect export (JSON + nested FIT zip) through import_path.

Covers the seams the unit tests cannot: raw retention and duplicate detection,
local-date resolution from watch clock offsets, the midnight-closing counter
rule for steps, device and vendor rows side by side, readiness selection, and
that a second import of the same bytes changes nothing.
"""

import datetime
import io
import json
import pathlib
import zipfile

from fit_builder import FitBuilder

from disconect import contract, health, storage
from disconect.ingest import connect_export, sources

UTC = datetime.timezone.utc
OFFSET = datetime.timedelta(hours=3)


def _monitoring_day(day_start_utc: datetime.datetime, steps_close: int, serial: int = 7) -> bytes:
    """One local day of counters: reset at local midnight, closing record at the next midnight."""
    b = FitBuilder("monitoring_b", serial=serial, created=day_start_utc)
    b.add("monitoring_info", timestamp=day_start_utc, local_timestamp=day_start_utc + OFFSET,
          resting_metabolic_rate=1600)
    # 30 steps a minute after midnight, more through the day, closing total at next midnight
    b.add("monitoring", timestamp=day_start_utc + datetime.timedelta(minutes=1), activity_type="walking",
          steps=30, active_time=60.0, distance=20.0)
    b.add("monitoring", timestamp=day_start_utc + datetime.timedelta(hours=12), activity_type="walking",
          steps=steps_close - 500, active_time=3000.0, distance=float(steps_close - 500) * 0.7)
    b.add("monitoring", timestamp=day_start_utc + datetime.timedelta(hours=12), activity_type="running",
          steps=400, active_time=600.0, distance=500.0)
    b.add("monitoring", timestamp=day_start_utc + datetime.timedelta(hours=24), activity_type="walking",
          steps=steps_close - 400, active_time=4000.0, distance=float(steps_close - 400) * 0.7)
    b.add("monitoring", timestamp=day_start_utc + datetime.timedelta(hours=24), activity_type="running",
          steps=400, active_time=600.0, distance=500.0)
    b.add("monitoring_hr_data", timestamp=day_start_utc + datetime.timedelta(hours=20),
          resting_heart_rate=50, current_day_resting_heart_rate=51)
    b.add("stress_level", stress_level_time=day_start_utc + datetime.timedelta(hours=10), stress_level_value=30)
    b.add("stress_level", stress_level_time=day_start_utc + datetime.timedelta(hours=11), stress_level_value=-1)
    # the energy gauge, every 10 minutes from 01:00 to 03:00 local: 60 up to 80, down to 20, up to 35
    # (charged 20 + 15 = 35, drained 60); frames written out of order to prove decode/insert do not
    # depend on file order (the reducers' own ordering is tested in test_writer_reducers)
    curve = [60, 70, 80, 60, 40, 20, 25, 30, 35]
    frames = [(datetime.timedelta(hours=1, minutes=10 * i), gauge) for i, gauge in enumerate(curve)]
    for offset, gauge in reversed(frames):
        b.add("stress_level", stress_level_time=day_start_utc + offset, stress_level_value=-1, raw_uint8={3: gauge})
    return b.build()


def _sleep_night(end_utc: datetime.datetime, score: int, serial: int = 7) -> bytes:
    start = end_utc - datetime.timedelta(hours=7)
    b = FitBuilder("49", serial=serial, created=end_utc)
    b.add("event", timestamp=start, event=74, event_type="start")
    b.add("sleep_level", timestamp=start + datetime.timedelta(hours=1), sleep_level="light")
    b.add("sleep_level", timestamp=start + datetime.timedelta(hours=3), sleep_level="deep")
    b.add("sleep_level", timestamp=end_utc, sleep_level="rem")
    b.add("event", timestamp=end_utc, event=74, event_type="stop")
    b.add("sleep_assessment", overall_sleep_score=score, awakenings_count=1)
    return b.build()


def _uds(date: str, steps: int, rhr: int) -> dict:
    return {
        "userProfilePK": 111, "calendarDate": date, "totalSteps": steps, "totalDistanceMeters": 3000,
        "restingHeartRate": rhr, "currentDayRestingHeartRate": rhr + 1, "minHeartRate": 45, "maxHeartRate": 140,
        "includesWellnessData": True, "totalKilocalories": 2100.0, "activeKilocalories": 400.0,
        "bmrKilocalories": 1700.0, "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 5,
        "averageSpo2Value": 96.0, "lowestSpo2Value": 91,
        "wellnessStartTimeGmt": f"{date}T21:00:00.0", "wellnessEndTimeGmt": f"{date}T21:00:00.0",
        "allDayStress": {"aggregatorList": [{"type": "TOTAL", "averageStressLevel": 27},
                                            {"type": "AWAKE", "averageStressLevel": 33}]},
        "respiration": {"avgWakingRespirationValue": 15.0, "highestRespirationValue": 20.0,
                        "lowestRespirationValue": 11.0},
        "bodyBattery": {"chargedValue": 60, "drainedValue": 55,
                        "bodyBatteryStatList": [{"bodyBatteryStatType": "HIGHEST", "statsValue": 80},
                                                {"bodyBatteryStatType": "LOWEST", "statsValue": 20}]},
    }


def _sleep_json(date: str, end_utc: datetime.datetime, score: int) -> dict:
    start = end_utc - datetime.timedelta(hours=7)
    fmt = "%Y-%m-%dT%H:%M:%S.0"
    return {"retro": False, "calendarDate": date, "sleepStartTimestampGMT": start.strftime(fmt),
            "sleepEndTimestampGMT": end_utc.strftime(fmt), "deepSleepSeconds": 7200, "lightSleepSeconds": 3600,
            "remSleepSeconds": 14400, "awakeSleepSeconds": 0, "unmeasurableSeconds": 0, "awakeCount": 1,
            "avgSleepStress": 14.0, "sleepScores": {"overallScore": score, "qualityScore": 70,
                                                    "restfulnessScore": 88, "feedback": "x", "insight": "y"},
            "spo2SleepSummary": {"userProfilePk": 111, "averageSPO2": 95.5, "lowestSPO2": 90, "averageHR": 49.0},
            "averageRespiration": 14.0, "lowestRespiration": 12.0, "highestRespiration": 17.0}


def _readiness(date: str, ts: str, context: str, score: int) -> dict:
    return {"calendarDate": date, "timestamp": ts, "inputContext": context, "score": score, "level": "HIGH",
            "sleepScoreFactorPercent": 80, "recoveryTimeFactorPercent": 100, "acwrFactorPercent": 90,
            "stressHistoryFactorPercent": 70, "hrvFactorPercent": 85, "sleepHistoryFactorPercent": 75,
            "hrvWeeklyAverage": 52.0, "recoveryTime": 6, "acuteLoad": 120, "validSleep": True}


def _build_export(root: pathlib.Path) -> None:
    """Two local days (UTC+3): 2025-06-15 and 2025-06-16; FIT and JSON for both."""
    day1 = datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC)   # 00:00 local on 06-15
    day2 = day1 + datetime.timedelta(days=1)
    fits = {
        "user@example.com_1.fit": _monitoring_day(day1, 8000),
        "user@example.com_2.fit": _monitoring_day(day2, 5000),
        "user@example.com_3.fit": _sleep_night(day1 + datetime.timedelta(hours=6), 81),   # ends 06:00 local 06-15
        "user@example.com_4.fit": _sleep_night(day2 + datetime.timedelta(hours=6), 74),
    }
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        for name, data in fits.items():
            z.writestr(name, data)
    up = root / "DI_CONNECT" / "DI-Connect-Uploaded-Files"
    up.mkdir(parents=True)
    (up / "UploadedFiles_0-_Part1.zip").write_bytes(inner.getvalue())
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir()
    (agg / "UDSFile_2025-06-15_2025-06-16.json").write_text(json.dumps([
        _uds("2025-06-15", 8000, 50), _uds("2025-06-16", 5000, 49),
        {"calendarDate": "2025-06-17", "includesWellnessData": False, "totalSteps": 0}]))
    wellness = root / "DI_CONNECT" / "DI-Connect-Wellness"
    wellness.mkdir()
    (wellness / "2025-06-15_2025-06-16_111_sleepData.json").write_text(json.dumps([
        _sleep_json("2025-06-15", day1 + datetime.timedelta(hours=6), 81),
        _sleep_json("2025-06-16", day2 + datetime.timedelta(hours=6), 74), {"retro": False}]))
    metrics = root / "DI_CONNECT" / "DI-Connect-Metrics"
    metrics.mkdir()
    (metrics / "TrainingReadinessDTO_111_111_111.json").write_text(json.dumps([
        _readiness("2025-06-15", "2025-06-15T04:00:00.0", "AFTER_WAKEUP_RESET", 77),
        _readiness("2025-06-15", "2025-06-15T15:00:00.0", "UPDATE_REALTIME_VARIABLES", 60),
        _readiness("2025-06-15", "2025-06-15T15:00:00.0", "UPDATE_REALTIME_VARIABLES", 60),  # duplicate
        _readiness("2025-06-16", "2025-06-16T15:00:00.0", "UPDATE_REALTIME_VARIABLES", 55)]))
    (root / "IT_ORDERS").mkdir()
    (root / "IT_ORDERS" / "orders.json").write_text("[]")


def _import(root, db_path):
    with storage.open_for_write(db_path, "test") as conn:
        return sources.import_path(root, conn)


def test_export_import_end_to_end(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    assert connect_export.looks_like_export(root)

    stats = _import(root, db_path)
    assert stats.status() == "ok" and stats.files_failed == 0
    assert stats.transport == "connect_export"
    assert stats.files_imported == 4 + 2 + 2 + 4  # fits, uds days, sleep nights, readiness records
    assert stats.dropped["uds_day_without_wellness_data"] == 1
    assert stats.dropped["sleep_stub_without_date"] == 1
    assert stats.dropped["stress_sentinel"] == 2 + 2 * 9, "gauge-only frames still carry the stress sentinel"
    assert stats.ignored >= 1
    assert stats.dates_assumed_utc == 0

    conn = storage.open_read_only(db_path)
    daily = {(r["date"], r["metric"], r["source_scope"]): r["value"] for r in conn.execute(
        "SELECT date, metric, source_scope, value FROM daily_metrics")}
    # steps: closing record at local midnight belongs to the day it closes; local == vendor
    assert daily[("2025-06-15", "steps", "local")] == 8000 == daily[("2025-06-15", "steps", "vendor_cloud")]
    assert daily[("2025-06-16", "steps", "local")] == 5000 == daily[("2025-06-16", "steps", "vendor_cloud")]
    assert ("2025-06-17", "steps", "local") not in daily
    assert ("2025-06-17", "steps", "vendor_cloud") not in daily, "a day without wellness data is not zeros"
    # daily facts resolve to the watch's local date (20:00 UTC on 06-15 is still 06-15 locally)
    assert daily[("2025-06-15", "resting_heart_rate", "device")] == 50
    assert daily[("2025-06-15", "resting_heart_rate", "vendor_cloud")] == 50
    assert daily[("2025-06-15", "sleep_score", "device")] == 81 == daily[("2025-06-15", "sleep_score", "vendor_cloud")]
    assert daily[("2025-06-15", "stress_avg", "vendor_cloud")] == 27
    assert daily[("2025-06-15", "stress_avg", "local")] == 30, "sentinel dropped, valid reading averaged"
    assert daily[("2025-06-15", "body_battery_high", "vendor_cloud")] == 80
    # the gauge decoded from the watch's own files, reduced per local day in time order
    assert daily[("2025-06-15", "energy_reserve_high", "local")] == 80
    assert daily[("2025-06-15", "energy_reserve_low", "local")] == 20
    assert daily[("2025-06-15", "energy_reserve_charged", "local")] == 35
    assert daily[("2025-06-15", "energy_reserve_drained", "local")] == 60
    agreement = {(r["metric"], r["compared_with"]): r for r in health.source_agreement(conn)}
    pair = agreement[("energy_reserve_high", "body_battery_high")]
    assert pair["scope_a"] == "local" and pair["scope_b"] == "vendor_cloud" and pair["days_compared"] >= 1
    assert all(r["metric"] in contract.metric_names() for r in agreement.values()), "metric stays a contract id"
    assert pair["days_matching"] == pair["days_compared"], "synthetic export agrees with the synthetic watch"
    # readiness: morning reset wins over later updates; a day without one takes the latest
    assert daily[("2025-06-15", "training_readiness", "vendor_cloud")] == 77
    assert daily[("2025-06-16", "training_readiness", "vendor_cloud")] == 55
    labels = {(r[0], r[1]): r[2] for r in conn.execute("SELECT date, metric, label FROM daily_labels")}
    assert labels[("2025-06-15", "training_readiness_level")] == "high"
    assert labels[("2025-06-15", "training_readiness_context")] == "after_wakeup_reset"

    sleeps = {(r["date"], r["source_scope"]): r for r in conn.execute("SELECT * FROM sleep_sessions")}
    device, vendor = sleeps[("2025-06-15", "device")], sleeps[("2025-06-15", "vendor_cloud")]
    assert device["start_utc"] == vendor["start_utc"] and device["end_utc"] == vendor["end_utc"]
    assert (device["light_s"], device["deep_s"], device["rem_s"]) == (3600, 7200, 14400)
    assert (vendor["light_s"], vendor["deep_s"], vendor["rem_s"]) == (3600, 7200, 14400)
    assert vendor["restlessness_score"] == 88 and vendor["avg_hr"] == 49.0
    stages = conn.execute("SELECT COUNT(*) FROM sleep_stages WHERE sleep_id=?", (device["sleep_id"],)).fetchone()[0]
    assert stages == 3

    raw = conn.execute("SELECT stream, payload_kind, COUNT(*) FROM raw_records GROUP BY stream, payload_kind").fetchall()
    assert {(r[0], r[1]): r[2] for r in raw} == {("fit:monitoring_b", "fit"): 2, ("fit:sleep", "fit"): 2,
                                                  ("json:uds", "json"): 3, ("json:sleep", "json"): 2,
                                                  ("json:readiness", "json"): 3}
    assert conn.execute("SELECT COUNT(*) FROM metric_samples WHERE metric='stress'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM clock_offsets").fetchone()[0] == 2
    provenance = {r["stream"]: r for r in conn.execute("SELECT * FROM stream_provenance")}
    assert provenance["fit:monitoring_b"]["files_ok"] == 2 and provenance["fit:monitoring_b"]["files_failed"] == 0

    report = health.data_health(conn, 30)
    assert not report["never_imported"]
    assert report["sleep"]["device"]["nights"] == 2
    assert report["recent_imports"][0]["status"] == "ok"
    assert "user@example.com" not in json.dumps(report), "reports must never carry the account email"
    conn.close()

    # importing the same export again writes nothing new
    again = _import(root, db_path)
    assert again.files_imported == 0 and again.files_duplicate == stats.files_imported
    assert again.records_written == 0


def _gauge_file(created_utc: datetime.datetime, readings: list[tuple[int, int]], serial: int = 7) -> bytes:
    """A monitoring file holding only gauge frames at (minutes after created, value)."""
    b = FitBuilder("monitoring_b", serial=serial, created=created_utc)
    b.add("monitoring_info", timestamp=created_utc, local_timestamp=created_utc + OFFSET)
    for minutes, gauge in readings:
        b.add("stress_level", stress_level_time=created_utc + datetime.timedelta(minutes=minutes),
              stress_level_value=-1, raw_uint8={3: gauge})
    return b.build()


def _energy_rows(db_path, date: str) -> dict[str, float]:
    conn = storage.open_read_only(db_path)
    try:
        return {m: v for m, v in conn.execute(
            "SELECT metric, value FROM daily_metrics WHERE date=? AND source_scope='local' AND metric LIKE 'energy_reserve_%'",
            (date,))}
    finally:
        conn.close()


def test_second_import_starting_midday_keeps_the_previous_days_dailies(tmp_path, db_path):
    """Review blocker: the derive window is span ± 1 day, so a file starting at noon on
    D+1 sees only the afternoon of D. D's figures must not be rewritten from that tail."""
    midnight = datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC)  # local midnight of 2025-06-15
    first = tmp_path / "first"
    first.mkdir()
    (first / "d.fit").write_bytes(_gauge_file(midnight, [(60, 60), (70, 80), (80, 20), (90, 35), (23 * 60, 30)]))
    _import(first, db_path)
    before = _energy_rows(db_path, "2025-06-15")
    assert before == {"energy_reserve_high": 80, "energy_reserve_low": 20, "energy_reserve_charged": 35,
                      "energy_reserve_drained": 60}
    second = tmp_path / "second"
    second.mkdir()
    noon_next = midnight + datetime.timedelta(hours=36)
    (second / "e.fit").write_bytes(_gauge_file(noon_next, [(0, 50), (10, 55), (20, 45)]))
    _import(second, db_path)
    assert _energy_rows(db_path, "2025-06-15") == before, "the earlier, complete day keeps its figures"
    assert _energy_rows(db_path, "2025-06-16") == {"energy_reserve_high": 55, "energy_reserve_low": 45,
                                                   "energy_reserve_charged": 5, "energy_reserve_drained": 10}


def test_writer_reducers_order_by_time_and_skip_gaps():
    from disconect.ingest import writer  # noqa: PLC0415
    t = datetime.datetime(2025, 6, 15, 8, 0, tzinfo=UTC)
    m = datetime.timedelta(minutes=1)
    # out of order on purpose: sorted it reads 50, 60, 40, 45  => charged 15, drained 20
    readings = [(t + 2 * m, 40), (t, 50), (t + 3 * m, 45), (t + 1 * m, 60)]
    assert writer.charged(readings) == 15 and writer.drained(readings) == 20
    assert writer.REDUCERS["max"](readings) == 60 and writer.REDUCERS["min"](readings) == 40
    # a 4-hour hole: the change across it is not a step of the day
    with_gap = readings + [(t + 4 * datetime.timedelta(hours=1), 90), (t + 4 * datetime.timedelta(hours=1) + m, 92)]
    assert writer.charged(with_gap) == 17 and writer.drained(with_gap) == 20
    assert writer.charged([(t, 70)]) == 0 and writer.drained([(t, 70)]) == 0


def test_fit_folder_import_and_duplicate_across_transports(tmp_path, db_path):
    day = datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC)
    folder = tmp_path / "GARMIN" / "Monitor"
    folder.mkdir(parents=True)
    (folder / "A.FIT").write_bytes(_monitoring_day(day, 3000))
    stats = _import(tmp_path / "GARMIN", db_path)
    assert stats.transport == "drop" and stats.files_imported == 1
    with storage.open_for_write(db_path, "test") as conn:
        again = sources.import_path(folder / "A.FIT", conn, transport="usb")
    assert again.files_duplicate == 1 and again.files_imported == 0


def test_decode_failure_is_recorded_not_fatal(tmp_path, db_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "bad.fit").write_bytes(b"\x00" * 40)
    (folder / "good.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC), 1000))
    stats = _import(folder, db_path)
    assert stats.status() == "partial"
    assert stats.files_failed == 1 and stats.files_imported == 1
    assert stats.failures[0]["kind"] == "unrecognized_payload"
    conn = storage.open_read_only(db_path)
    row = conn.execute("SELECT files_failed, last_parse_error_kind FROM stream_provenance "
                       "WHERE stream='fit:undecodable'").fetchone()
    assert tuple(row) == (1, "unrecognized_payload")
    conn.close()


def test_mask_label_hides_email_and_ids():
    assert connect_export.mask_label("someone.real@gmail.com_123456789.fit") == "{email}_{id}.fit"
    assert connect_export.mask_label("UDSFile_2025-03-24_2025-07-02.json") == "UDSFile_2025-03-24_2025-07-02.json"
