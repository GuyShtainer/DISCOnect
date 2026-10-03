"""The feature engine: own-history baselines, thin-evidence honesty, evidence dates."""

import datetime

import pytest

from disconect import contract, insight, storage
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import ClockOffset

UTC = datetime.timezone.utc
AS_OF = datetime.date(2025, 7, 1)


def _seed(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                     "payload_hash, payload_bytes, imported_at) VALUES('fit:monitoring_b','k','device','usb','fit',"
                     "x'00','h',1,'2025-07-01T00:00:00Z')")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 15, 12, tzinfo=UTC), 10800)], "7", 1)
        rows = []
        # sleep_score: 28 baseline days alternating 68/72 (mean 70), then 7 window days at 80
        for back in range(35):
            date = (AS_OF - datetime.timedelta(days=back)).isoformat()
            value = 80 if back < 7 else (68 if back % 2 else 72)
            rows.append((date, "sleep_score", value, "device", "7"))
            rows.append((date, "sleep_score", value, "vendor_cloud", None))
        # steps: only 2 baseline days -> too thin
        rows += [((AS_OF - datetime.timedelta(days=b)).isoformat(), "steps", 5000 + b, "local", "7") for b in (0, 8, 9)]
        # vo2max: one point in the window, nothing before -> no_baseline
        rows.append((AS_OF.isoformat(), "vo2max", 45.0, "device", "7"))
        # resting_heart_rate: baseline only, nothing in window
        rows += [((AS_OF - datetime.timedelta(days=b)).isoformat(), "resting_heart_rate", 50, "device", "7")
                 for b in range(10, 20)]
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,?,?,1)", rows)
        # stress samples: 5 baseline days at 30, window days at 30/50 (per-day mean 40); 22:30Z lands on next local day
        samples = []
        for back in range(12):
            day = AS_OF - datetime.timedelta(days=back)
            for hour, value in ((10, 30), (11, 50 if back < 7 else 30)):
                samples.append(("stress", f"{day.isoformat()}T{hour:02d}:00:00Z", value))
        conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,'device','7',1)", samples)


def test_confidence_bands_are_pinned():
    assert insight.MIN_BASELINE_DAYS == 3
    assert [insight.confidence_for(n) for n in (0, 2, 3, 4, 5, 7, 8, 30)] == [
        "insufficient", "insufficient", "low", "low", "medium", "medium", "high", "high"]
    assert insight.DEFAULT_WINDOW_DAYS == 7 and insight.DEFAULT_BASELINE_DAYS == 28


def test_period_facts_compare_against_own_baseline(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    result = insight.period_facts(conn)
    assert result["as_of"] == "2025-07-01", "defaults to the latest stored date"
    assert result["window"] == {"from": "2025-06-25", "to": "2025-07-01", "days": 7}
    assert result["baseline"]["from"] == "2025-05-28" and result["baseline"]["to"] == "2025-06-24"
    facts = {(f["metric"], f["source_scope"]): f for f in result["facts"]}

    sleep = facts[("sleep_score", "device")]
    assert sleep["value"] == 80 and sleep["window_days_with_data"] == 7
    assert sleep["baseline_days_with_data"] == 28 and sleep["confidence"] == "high"
    assert sleep["comparison"]["baseline_mean"] == 70 and sleep["comparison"]["delta"] == 10
    assert sleep["comparison"]["direction"] == "higher" and sleep["comparison"]["delta_percent"] == pytest.approx(14.29)
    assert 4 < sleep["comparison"]["z_score"] < 6
    assert sleep["reason_code"] == "ok"
    assert sleep["evidence"]["window_dates"][0] == "2025-06-25" and "window_points" not in sleep["evidence"]
    with_points = insight.period_facts(conn, metrics=["sleep_score"], source_scope="device", include_points=True)
    assert with_points["facts"][0]["evidence"]["window_points"][0] == {"date": "2025-06-25", "value": 80}
    assert ("sleep_score", "vendor_cloud") in facts, "scopes stay side by side"

    steps = facts[("steps", "local")]
    assert steps["confidence"] == "insufficient" and steps["comparison"] is None
    assert steps["reason_code"] == "baseline_too_thin" and steps["baseline_days_with_data"] == 2
    assert steps["value"] == 5000

    vo2 = facts[("vo2max", "device")]
    assert vo2["reason_code"] == "no_baseline" and vo2["value"] == 45.0

    rhr = facts[("resting_heart_rate", "device")]
    assert rhr["reason_code"] == "no_data_in_window" and rhr["value"] is None
    assert rhr["baseline_days_with_data"] == 10

    stress = facts[("stress", "device")]
    assert stress["cadence"] == "sample" and stress["value"] == 40 and stress["comparison"]["baseline_mean"] == 30
    assert stress["baseline_days_with_data"] == 5 and stress["confidence"] == "medium"
    assert stress["evidence"]["aggregation"].startswith("mean of per-day means")

    assert "population" in " ".join(result["rules"])
    assert "heart_rate" not in {m for m, _ in facts}, "metrics with no data at all are omitted"
    conn.close()


def test_requested_metrics_window_and_scope_filters(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    result = insight.period_facts(conn, window_days=1, baseline_days=6, end_date="2025-07-01",
                                  metrics=["sleep_score", "heart_rate", "bogus"], source_scope="device")
    facts = {f["metric"]: f for f in result["facts"]}
    assert set(facts) == {"sleep_score", "heart_rate"}
    assert facts["sleep_score"]["source_scope"] == "device" and facts["sleep_score"]["value"] == 80
    assert facts["sleep_score"]["comparison"]["baseline_mean"] == 80, "a 1-day window vs the 6 days before it"
    assert facts["sleep_score"]["comparison"]["direction"] == "unchanged"
    assert facts["sleep_score"]["comparison"]["z_score"] is None, "no spread in the baseline means no z-score"
    assert facts["heart_rate"]["reason_code"] == "no_data" and facts["heart_rate"]["confidence"] == "insufficient"
    assert result["ignored_metrics"] == ["bogus"]
    with pytest.raises(ValueError):
        insight.period_facts(conn, source_scope="cloud")
    conn.close()


def test_empty_store_says_so(db_path):
    with storage.open_for_write(db_path, "test"):
        pass
    conn = storage.open_read_only(db_path)
    result = insight.period_facts(conn)
    assert result["as_of"] is None and result["facts"] == [] and "nothing stored" in result["reason"]
    conn.close()


def test_every_fact_metric_is_in_the_contract(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    names = set(contract.metric_names())
    for fact in insight.period_facts(conn)["facts"]:
        assert fact["metric"] in names and fact["unit"] == contract.unit_for(fact["metric"])
    conn.close()
