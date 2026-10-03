"""Read side: series aggregation by local day, sleep detail, activities, MCP tool surface."""

import asyncio
import datetime
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from disconect import contract, queries, storage
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import ClockOffset

UTC = datetime.timezone.utc


def _seed(db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, "
                     "payload_hash, payload_bytes, imported_at) VALUES('fit:monitoring_b','k','device','usb','fit',"
                     "x'00','h',1,'2025-06-16T00:00:00Z')")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 15, 12, tzinfo=UTC), 10800)], "7", 1)
        # 22:30 UTC on 06-15 is 01:30 local on 06-16: must aggregate into the 16th
        rows = [("stress", "2025-06-15T10:00:00Z", 20.0), ("stress", "2025-06-15T11:00:00Z", 40.0),
                ("stress", "2025-06-15T22:30:00Z", 60.0), ("heart_rate", "2025-06-15T10:00:00Z", 60.0)]
        conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,'device','7',1)", rows)
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,?,?,1)",
                         [("2025-06-15", "sleep_score", 80, "device", "7"), ("2025-06-15", "sleep_score", 80, "vendor_cloud", None),
                          ("2025-06-16", "sleep_score", 70, "device", "7"), ("2025-06-16", "steps", 5000, "local", "7")])
        conn.execute("INSERT INTO daily_labels(date, metric, label, source_scope, device_id, raw_record_id) "
                     "VALUES('2025-06-16','hrv_status','balanced','device','7',1)")
        conn.execute("INSERT INTO sleep_sessions(sleep_id, date, start_utc, end_utc, deep_s, light_s, overall_score, "
                     "source_scope, device_id, raw_record_id) VALUES('2025-06-16|device|7','2025-06-16',"
                     "'2025-06-15T21:00:00Z','2025-06-16T04:00:00Z',3600,7200,77,'device','7',1)")
        conn.execute("INSERT INTO sleep_stages(sleep_id, stage, start_utc, end_utc) VALUES('2025-06-16|device|7',"
                     "'deep','2025-06-15T21:00:00Z','2025-06-15T22:00:00Z')")
        conn.execute("INSERT INTO activities(activity_id, start_utc, end_utc, sport, distance_m, avg_hr, source_scope, "
                     "device_id, raw_record_id) VALUES('a','2025-06-15T16:00:00Z','2025-06-15T16:30:00Z','running',"
                     "5000,150,'device','7',1)")


def test_metric_series_daily_sample_labels_and_unknown(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    result = queries.metric_series(conn, ["sleep_score", "stress", "steps", "hrv_status", "nope"], days=3,
                                   end_date="2025-06-16")
    by_key = {(s["metric"], s["source_scope"]): s for s in result["series"]}
    assert [p["value"] for p in by_key[("sleep_score", "device")]["points"]] == [80, 70]
    assert [p["value"] for p in by_key[("sleep_score", "vendor_cloud")]["points"]] == [80]
    stress = by_key[("stress", "device")]
    assert stress["unit"] == "score" and stress["cadence"] == "sample"
    assert [(p["date"], p["min"], p["max"], p["samples"]) for p in stress["points"]] == [
        ("2025-06-15", 20.0, 40.0, 2), ("2025-06-16", 60.0, 60.0, 1)], "late-evening UTC sample lands on the local next day"
    assert by_key[("steps", "local")]["points"] == [{"date": "2025-06-16", "value": 5000}]
    assert result["ignored_metrics"] == ["nope"]
    assert result["labels"] == [{"metric": "hrv_status", "from": "2025-06-14", "to": "2025-06-16",
                                 "points": [{"date": "2025-06-16", "label": "balanced", "source_scope": "device"}]}]
    assert "never filled with 0" in result["missing_values"]
    only_vendor = queries.metric_series(conn, ["sleep_score"], days=3, source_scope="vendor_cloud", end_date="2025-06-16")
    assert [s["source_scope"] for s in only_vendor["series"]] == ["vendor_cloud"]
    empty = queries.metric_series(conn, ["vo2max"], days=3, end_date="2025-06-16")
    assert empty["series"][0]["points"] == [] and empty["series"][0]["source_scope"] is None
    with pytest.raises(ValueError):
        queries.metric_series(conn, ["steps"], source_scope="cloud")
    conn.close()


def test_sleep_detail_and_activities_hide_identifiers(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    latest = queries.sleep_detail(conn)
    assert latest["date"] == "2025-06-16"
    [session] = latest["sessions"]
    assert session["stage_minutes"] == {"deep": 60.0, "light": 120.0}
    assert session["overall_score"] == 77 and "quality_score" not in session
    assert session["stages"] == [{"stage": "deep", "start_utc": "2025-06-15T21:00:00Z",
                                  "end_utc": "2025-06-15T22:00:00Z", "minutes": 60.0}]
    assert "device_id" not in json.dumps(latest) and "sleep_id" not in json.dumps(latest)
    assert queries.sleep_detail(conn, "2024-01-01")["sessions"] == []
    activities = queries.list_activities(conn, 5)
    assert activities["activities"][0]["sport"] == "running"
    assert "device_id" not in json.dumps(activities)
    conn.close()


def test_mcp_tools_are_read_only_and_answer_through_the_store(db_path, monkeypatch):
    _seed(db_path)
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    from disconect import mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    names = {t.name for t in tools}
    assert names == {"get_data_health", "get_metric_series", "get_sleep_detail", "list_activities",
                     "get_contract", "get_period_facts"}
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        for verb in ("import", "delete", "write", "sync", "update", "set"):
            assert verb not in tool.name
    assert "never filled with 0" in mcp_server.server.instructions
    assert "no port" in mcp_server.server.instructions

    result = asyncio.run(mcp_server.server.call_tool("get_sleep_detail", {}))
    payload = result[1] if isinstance(result, tuple) else result
    text = json.dumps(payload, default=str)
    assert "2025-06-16" in text and "device_id" not in text


def test_mcp_without_database_explains_itself(db_path, monkeypatch):
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path / "missing.db"))
    from mcp.server.mcpserver.exceptions import ToolError

    from disconect import mcp_server
    with pytest.raises(ToolError, match="import"):
        asyncio.run(mcp_server.server.call_tool("get_data_health", {"window_days": 7}))


def test_contract_metric_enum_matches_queries():
    for name in contract.metric_names():
        assert contract.cadence_for(name) in ("sample", "daily")


def test_intraday_samples_uses_the_local_day_not_the_utc_one(db_path):
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    # The watch runs UTC+3, so 22:30 UTC on the 15th is 01:30 on the 16th.
    day15 = queries.intraday_samples(conn, "stress", "2025-06-15")
    assert [(p["hour"], p["value"]) for p in day15["series"][0]["points"]] == [(13.0, 20.0), (14.0, 40.0)]
    day16 = queries.intraday_samples(conn, "stress", "2025-06-16")
    assert [(p["hour"], p["value"]) for p in day16["series"][0]["points"]] == [(1.5, 60.0)]
    assert day15["series"][0]["source_scope"] == "device" and day15["unit"] == "score"
    assert queries.intraday_samples(conn, "stress")["date"] == "2025-06-16", "defaults to the newest day with samples"
    quiet = queries.intraday_samples(conn, "stress", "2025-06-14")
    assert quiet["series"] == [] and "no samples" in quiet["reason"]
    unseen = queries.intraday_samples(conn, "spo2")
    assert unseen["date"] is None and unseen["series"] == [] and "time" in unseen
    with pytest.raises(ValueError):
        queries.intraday_samples(conn, "sleep_score")
    with pytest.raises(ValueError):
        queries.intraday_samples(conn, "stress", "2025-06-15", source_scope="cloud")
    conn.close()


BAD_DAYS = ("20261003", "2026-W40-6", "2025-6-30", " 2025-06-30", "2025-06-30\n", "2025-02-30", "2025-13-40",
            "２０２５-06-30", "2025-03-05T00:00", "nonsense")


@pytest.mark.parametrize("bad", BAD_DAYS)
def test_a_date_argument_is_exactly_yyyy_mm_dd(db_path, monkeypatch, bad):
    """``date.fromisoformat`` alone takes compact and week forms; a model could reach them through MCP."""
    from disconect import insight
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    with pytest.raises(ValueError, match="^date must be YYYY-MM-DD$"):
        queries.sleep_detail(conn, bad)
    with pytest.raises(ValueError, match="^end_date must be YYYY-MM-DD$"):
        insight.period_facts(conn, end_date=bad)
    with pytest.raises(ValueError, match="^end_date must be YYYY-MM-DD$"):
        queries.metric_series(conn, ["steps"], end_date=bad)
    conn.close()
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    from disconect import mcp_server
    for tool, args, text in (("get_sleep_detail", {"date": bad}, "date must be YYYY-MM-DD"),
                             ("get_period_facts", {"end_date": bad}, "end_date must be YYYY-MM-DD")):
        with pytest.raises(ToolError) as caught:
            asyncio.run(mcp_server.server.call_tool(tool, args))
        assert str(caught.value) == f"Error executing tool {tool}: {text}", (tool, bad)


def test_the_strict_date_still_takes_a_good_day_and_an_empty_end_date_means_omitted(db_path):
    from disconect import insight
    _seed(db_path)
    conn = storage.open_read_only(db_path)
    assert queries.sleep_detail(conn, "2025-06-16")["date"] == "2025-06-16"
    assert insight.period_facts(conn, end_date="2025-06-16")["as_of"] == "2025-06-16"
    assert insight.period_facts(conn, end_date="")["as_of"] == insight.period_facts(conn)["as_of"]
    conn.close()
