"""Unit and end-to-end coverage for the newer Connect-export JSON sections.

Covers: acute/chronic training load + ratio (epoch-ms dates), endurance score,
hill score (deliberately imports nothing -- see decode_hill_score_record),
fitness age, body weight (gram/kilogram magnitude conversion), and the UDS
active-minutes/hydration additions. All records below are synthetic.
"""

import datetime
import json
import pathlib

from disconect import contract, storage
from disconect.ingest import connect_export, model, sources

UTC = datetime.timezone.utc

NEW_METRIC_NAMES = ("training_load_chronic", "training_load_ratio", "endurance_score",
                     "fitness_age", "weight_kg", "active_minutes", "highly_active_minutes",
                     "hydration_ml", "sweat_loss_ml")
NEW_LABEL_NAMES = ("training_load_status",)


def _epoch_ms(day: datetime.datetime) -> int:
    return int(day.timestamp() * 1000)


# ---- contract ----

def test_new_metrics_and_label_are_in_the_contract():
    for name in NEW_METRIC_NAMES:
        assert name in contract.metric_names()
    for name in NEW_LABEL_NAMES:
        assert name in contract.label_names()


# ---- MetricsAcuteTrainingLoad ----

def test_decode_training_load_record_epoch_ms_dates():
    ts = _epoch_ms(datetime.datetime(2025, 6, 15, tzinfo=UTC))
    record = {"calendarDate": ts, "timestamp": ts, "dailyTrainingLoadAcute": 200,
              "dailyTrainingLoadChronic": 150, "dailyAcuteChronicWorkloadRatio": 1.33,
              "acwrStatus": "HIGH", "deviceId": 1}
    key, decoded = connect_export.decode_training_load_record(record)
    assert key == f"2025-06-15|{ts}"
    assert decoded.source_scope == "vendor_cloud"
    assert decoded.daily == [
        model.DailyFact("training_load_acute", 200.0, date="2025-06-15"),
        model.DailyFact("training_load_chronic", 150.0, date="2025-06-15"),
        model.DailyFact("training_load_ratio", 1.33, date="2025-06-15"),
    ]
    assert decoded.labels == [model.DailyLabel("training_load_status", "high", date="2025-06-15")]


def test_decode_training_load_record_missing_ratio_writes_no_fact_or_label():
    ts = _epoch_ms(datetime.datetime(2025, 6, 15, tzinfo=UTC))
    record = {"calendarDate": ts, "timestamp": ts, "dailyTrainingLoadAcute": 200,
              "dailyTrainingLoadChronic": 150}
    _, decoded = connect_export.decode_training_load_record(record)
    assert {f.metric for f in decoded.daily} == {"training_load_acute", "training_load_chronic"}
    assert decoded.labels == []


def test_decode_training_load_record_without_date_is_none():
    assert connect_export.decode_training_load_record({"dailyTrainingLoadAcute": 200}) is None


# ---- EnduranceScore ----

def test_decode_endurance_score_record():
    ts = _epoch_ms(datetime.datetime(2025, 6, 15, tzinfo=UTC))
    record = {"calendarDate": ts, "timestamp": ts, "overallScore": 55, "deviceId": 1}
    key, decoded = connect_export.decode_endurance_score_record(record)
    assert key == f"2025-06-15|{ts}"
    assert decoded.daily == [model.DailyFact("endurance_score", 55.0, date="2025-06-15")]


def test_decode_endurance_score_record_without_score_writes_no_fact():
    ts = _epoch_ms(datetime.datetime(2025, 6, 15, tzinfo=UTC))
    _, decoded = connect_export.decode_endurance_score_record({"calendarDate": ts, "timestamp": ts})
    assert decoded.daily == []


# ---- HillScore ----

def test_decode_hill_score_record_imports_nothing_but_is_counted():
    ts = _epoch_ms(datetime.datetime(2025, 6, 15, tzinfo=UTC))
    record = {"calendarDate": ts, "timestamp": ts, "hillScoreClassificationId": 4,
              "hillScoreFeedbackPhraseId": 12, "deviceId": 1}
    key, decoded = connect_export.decode_hill_score_record(record)
    assert key == f"2025-06-15|{ts}"
    assert decoded.daily == [] and decoded.labels == []
    assert decoded.dropped == {"hill_score_fields_ambiguous": 1}


# ---- fitnessAgeData ----

def test_decode_fitness_age_record():
    record = {"asOfDateGmt": "2025-06-15T04:00:00.0", "createTimestamp": "2025-06-15T04:05:00.0",
              "currentBioAge": 31.2, "chronologicalAge": 40, "bmi": 23.1}
    key, decoded = connect_export.decode_fitness_age_record(record)
    assert key == "2025-06-15|2025-06-15T04:05:00.0"
    assert decoded.daily == [model.DailyFact("fitness_age", 31.2, date="2025-06-15")]


def test_decode_fitness_age_record_without_date_is_none():
    assert connect_export.decode_fitness_age_record({"currentBioAge": 31.2}) is None


# ---- userBioMetrics ----

def test_decode_bio_metrics_record_converts_grams_to_kilograms():
    record = {"metaData": {"calendarDate": "2025-06-15T00:00:00.0"},
              "weight": {"weight": 72500.0, "timestampGMT": "2025-06-15T00:00:00.0"}, "height": 180.0}
    key, decoded = connect_export.decode_bio_metrics_record(record)
    assert key == "2025-06-15|"
    assert decoded.daily == [model.DailyFact("weight_kg", 72.5, date="2025-06-15")]


def test_decode_bio_metrics_record_keeps_kilograms_as_is():
    record = {"metaData": {"calendarDate": "2025-06-15T00:00:00.0"}, "weight": {"weight": 71.8}}
    _, decoded = connect_export.decode_bio_metrics_record(record)
    assert decoded.daily == [model.DailyFact("weight_kg", 71.8, date="2025-06-15")]


def test_decode_bio_metrics_record_without_weight_writes_no_fact():
    record = {"metaData": {"calendarDate": "2025-06-15T00:00:00.0"}, "height": 180.0, "version": 3}
    key, decoded = connect_export.decode_bio_metrics_record(record)
    assert key == "2025-06-15|3"
    assert decoded.daily == []


# ---- UDSFile additions ----

def test_decode_uds_record_active_minutes_and_hydration():
    record = {"calendarDate": "2025-06-15", "includesWellnessData": True,
              "activeSeconds": 1800, "highlyActiveSeconds": 600,
              "hydration": {"valueInML": 1500.0, "sweatLossInML": 400.0}}
    _, decoded = connect_export.decode_uds_record(record)
    facts = {f.metric: f.value for f in decoded.daily}
    assert facts["active_minutes"] == 30.0
    assert facts["highly_active_minutes"] == 10.0
    assert facts["hydration_ml"] == 1500.0
    assert facts["sweat_loss_ml"] == 400.0


def test_decode_uds_record_without_new_fields_writes_nothing_new():
    record = {"calendarDate": "2025-06-15", "includesWellnessData": True}
    _, decoded = connect_export.decode_uds_record(record)
    metrics = {f.metric for f in decoded.daily}
    assert metrics.isdisjoint({"active_minutes", "highly_active_minutes", "hydration_ml", "sweat_loss_ml"})


# ---- TrainingReadinessDTO no longer owns training_load_acute ----

def test_readiness_record_no_longer_writes_training_load_acute():
    records = [{"calendarDate": "2025-06-15", "timestamp": "2025-06-15T04:00:00.0",
                "inputContext": "AFTER_WAKEUP_RESET", "score": 70, "level": "HIGH", "acuteLoad": 120}]
    _, _, decoded = next(connect_export.decode_readiness_records(records))
    assert "training_load_acute" not in {f.metric for f in decoded.daily}


# ---- end to end: a minimal export with only the new JSON sections, no FIT ----

def _training_load(day: datetime.datetime, acute, chronic, ratio, status) -> dict:
    ts = _epoch_ms(day)
    return {"calendarDate": ts, "timestamp": ts, "dailyTrainingLoadAcute": acute,
            "dailyTrainingLoadChronic": chronic, "dailyAcuteChronicWorkloadRatio": ratio,
            "acwrStatus": status, "deviceId": 999}


def _endurance(day: datetime.datetime, score: int) -> dict:
    ts = _epoch_ms(day)
    return {"calendarDate": ts, "timestamp": ts, "overallScore": score, "deviceId": 999}


def _hill(day: datetime.datetime) -> dict:
    ts = _epoch_ms(day)
    return {"calendarDate": ts, "timestamp": ts, "hillScoreClassificationId": 3,
            "hillScoreFeedbackPhraseId": 42, "deviceId": 999}


def _fitness_age(date: str, bio_age: float) -> dict:
    return {"asOfDateGmt": f"{date}T00:00:00.0", "createTimestamp": f"{date}T00:05:00.0",
            "currentBioAge": bio_age, "chronologicalAge": 40, "bmi": 22.0}


def _bio_metrics(date: str, weight, version: int) -> dict:
    record = {"metaData": {"calendarDate": f"{date}T00:00:00.0"}, "height": 180.0, "version": version}
    if weight is not None:
        record["weight"] = {"weight": weight, "timestampGMT": f"{date}T00:00:00.0"}
    return record


def _build_export(root: pathlib.Path) -> None:
    metrics = root / "DI_CONNECT" / "DI-Connect-Metrics"
    metrics.mkdir(parents=True)
    (metrics / "MetricsAcuteTrainingLoad_111_111_111.json").write_text(json.dumps([
        _training_load(datetime.datetime(2025, 6, 15, tzinfo=UTC), 180, 140, 1.29, "OPTIMAL")]))
    (metrics / "EnduranceScore_111_111_111.json").write_text(json.dumps([
        _endurance(datetime.datetime(2025, 6, 15, tzinfo=UTC), 62)]))
    (metrics / "HillScore_111_111_111.json").write_text(json.dumps([
        _hill(datetime.datetime(2025, 6, 15, tzinfo=UTC))]))
    wellness = root / "DI_CONNECT" / "DI-Connect-Wellness"
    wellness.mkdir(parents=True)
    (wellness / "111_fitnessAgeData.json").write_text(json.dumps([_fitness_age("2025-06-15", 33.5)]))
    (wellness / "111_userBioMetrics.json").write_text(json.dumps([
        _bio_metrics("2025-06-15", 72500.0, 1),   # grams -> kg
        _bio_metrics("2025-06-16", 71.8, 2),      # already kg
        _bio_metrics("2025-06-17", None, 3),      # no weight -> no fact
    ]))


def test_metrics_export_end_to_end(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    assert connect_export.looks_like_export(root)

    with storage.open_for_write(db_path, "test") as conn:
        stats = sources.import_path(root, conn)
    assert stats.status() == "ok"
    assert stats.files_imported == 7  # load, endurance, hill, fitness age + 3 bio-metric records
    assert stats.dropped.get("hill_score_fields_ambiguous") == 1

    conn = storage.open_read_only(db_path)
    daily = {(r["date"], r["metric"], r["source_scope"]): r["value"] for r in conn.execute(
        "SELECT date, metric, source_scope, value FROM daily_metrics")}
    assert daily[("2025-06-15", "training_load_acute", "vendor_cloud")] == 180
    assert daily[("2025-06-15", "training_load_chronic", "vendor_cloud")] == 140
    assert daily[("2025-06-15", "training_load_ratio", "vendor_cloud")] == 1.29
    assert daily[("2025-06-15", "endurance_score", "vendor_cloud")] == 62
    assert daily[("2025-06-15", "fitness_age", "vendor_cloud")] == 33.5
    assert daily[("2025-06-15", "weight_kg", "vendor_cloud")] == 72.5
    assert daily[("2025-06-16", "weight_kg", "vendor_cloud")] == 71.8
    assert ("2025-06-17", "weight_kg", "vendor_cloud") not in daily

    labels = {(r[0], r[1]): r[2] for r in conn.execute("SELECT date, metric, label FROM daily_labels")}
    assert labels[("2025-06-15", "training_load_status")] == "optimal"

    provenance = {r["stream"]: r for r in conn.execute("SELECT * FROM stream_provenance")}
    assert provenance["json:hill"]["files_ok"] == 1
    assert provenance["json:hill"]["records_written"] == 0
    conn.close()
