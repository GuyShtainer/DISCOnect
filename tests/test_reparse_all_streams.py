"""Reparse must cover every stream the importer writes, and must refuse to lose rows."""

import datetime
import json
import zlib

from test_import import _build_export

from disconect import storage
from disconect.ingest import connect_export, sources

EPOCH_MS = int(datetime.datetime(2025, 6, 15, 12, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _add_metric_sections(root):
    metrics = root / "DI_CONNECT" / "DI-Connect-Metrics"
    (metrics / "MetricsAcuteTrainingLoad_1_2_3.json").write_text(json.dumps([
        {"calendarDate": EPOCH_MS, "timestamp": EPOCH_MS, "dailyTrainingLoadAcute": 120,
         "dailyTrainingLoadChronic": 100, "dailyAcuteChronicWorkloadRatio": 1.2, "acwrStatus": "OPTIMAL"}]))
    (metrics / "EnduranceScore_1_2_3.json").write_text(json.dumps([
        {"calendarDate": EPOCH_MS, "timestamp": EPOCH_MS, "overallScore": 5000, "classification": 3}]))
    (metrics / "HillScore_1_2_3.json").write_text(json.dumps([
        {"calendarDate": EPOCH_MS, "timestamp": EPOCH_MS, "hillScoreClassificationId": 2,
         "hillScoreFeedbackPhraseId": 7}]))
    (metrics / "MetricsMaxMetData_1_2_3.json").write_text(json.dumps([
        {"calendarDate": "2025-06-15", "updateTimestamp": "2025-06-15T10:00:00.0", "vo2MaxValue": 45.3,
         "sport": "RUNNING"}]))
    wellness = root / "DI_CONNECT" / "DI-Connect-Wellness"
    (wellness / "111_fitnessAgeData.json").write_text(json.dumps([
        {"createTimestamp": "2025-06-15T05:00:00.0", "asOfDateGmt": "2025-06-15T00:00:00.0",
         "currentBioAge": 30.5, "chronologicalAge": 33}]))
    (wellness / "111_userBioMetrics.json").write_text(json.dumps([
        {"version": 3, "metaData": {"calendarDate": "2025-06-15", "sequence": 1},
         "weight": {"weight": 75000.0, "sourceType": "MANUAL", "timestampGMT": "2025-06-15T06:00:00.0"}}]))


def _counts(db_path):
    conn = storage.open_read_only(db_path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("raw_records", "metric_samples", "daily_metrics", "daily_labels", "sleep_sessions",
                        "sleep_stages", "monitoring_intervals", "clock_offsets")}
    checksum = conn.execute("SELECT ROUND(SUM(value), 3) FROM daily_metrics").fetchone()[0]
    streams = {r[0] for r in conn.execute("SELECT DISTINCT stream FROM raw_records")}
    conn.close()
    return counts, checksum, streams


def test_every_imported_stream_has_a_reparse_path(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    _add_metric_sections(root)
    with storage.open_for_write(db_path, "test") as conn:
        stats = sources.import_path(root, conn)
    assert stats.files_failed == 0
    before = _counts(db_path)
    json_streams = {s for s in before[2] if s.startswith("json:")}
    assert json_streams == set(connect_export.RECORD_DECODERS) | set(connect_export.BATCH_STREAMS), \
        "a stream the importer writes must be registered for reparse"

    with storage.open_for_write(db_path, "test") as conn:
        replay = sources.reparse_all(conn)
    assert replay.files_failed == 0 and replay.status() == "ok"
    assert _counts(db_path) == before, "a replay with an unchanged decoder reproduces the store exactly"


def test_reparse_refuses_to_clear_when_a_record_fails(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn)
    before = _counts(db_path)
    with storage.open_for_write(db_path, "test") as conn:
        raw_id = conn.execute("SELECT id FROM raw_records WHERE stream='json:uds' AND source_key='2025-06-15'").fetchone()[0]
        conn.execute("UPDATE raw_records SET payload=? WHERE id=?", (zlib.compress(b"{not json"), raw_id))
        refused = sources.reparse_all(conn)
    assert refused.files_failed == 1 and refused.files_imported == 0 and refused.status() == "failed"
    assert refused.failures[0]["stream"] == "json:uds"
    assert _counts(db_path) == before, "nothing may change when the dry pass fails"
    conn = storage.open_read_only(db_path)
    run = conn.execute("SELECT status, error FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert run[0] == "failed" and "nothing was changed" in run[1]

    with storage.open_for_write(db_path, "test") as conn:
        forced = sources.reparse_all(conn, force=True)
    assert forced.files_failed == 1 and forced.status() == "partial"
    after = _counts(db_path)
    assert after[0]["raw_records"] == before[0]["raw_records"]
    assert after[0]["daily_metrics"] < before[0]["daily_metrics"], "forcing past a failure loses that record's rows"
