"""The PII allowlist, enforced in code: no tool output may carry identifiers.

The feasibility review's rule is that stripping happens in the tool
implementations, never in a prompt. This test seeds a store with the kinds of
identifiers the sources contain and walks every MCP tool's structured output.
"""

import asyncio
import json
import re

import pytest

from disconect import storage
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import ClockOffset

SERIAL = "3489012345"          # a FIT file_id.serial_number shape
FORBIDDEN_KEYS = {"device_id", "serial", "serial_number", "path", "email", "raw_record_id", "sleep_id",
                  "payload", "payload_hash", "source_key", "user_profile", "userProfilePK", "uuid",
                  "position_lat", "position_long", "latitude", "longitude"}
FORBIDDEN_TEXT = (SERIAL, "/Users/", "/tmp/", "@example.com")


def _seed(db_path, live=True):
    """The privacy corpus. ``live`` adds a ``json:live`` raw record (its ``source_key`` is a hash, no stamp, no device);
    ``gen_serve_fixtures`` passes False so the committed serve stores keep their original rows."""
    import datetime
    utc = datetime.timezone.utc
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, device_id, payload_kind, "
                     "payload, payload_hash, payload_bytes, imported_at) VALUES('fit:sleep','deadbeef','device','usb',"
                     f"'{SERIAL}','fit',x'00','h',1,'2025-07-01T00:00:00Z')")
        ClockOffsets.persist(conn, [ClockOffset(datetime.datetime(2025, 6, 15, 12, tzinfo=utc), 10800)], SERIAL, 1)
        conn.executemany("INSERT INTO daily_metrics(date, metric, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,?,?,1)", [(f"2025-06-{d:02d}", "sleep_score", 70 + d, "device", SERIAL)
                                                for d in range(1, 31)]
                         + [("2025-06-15", "energy_reserve_high", 88, "local", SERIAL),
                            ("2025-06-15", "body_battery_high", 88, "vendor_cloud", None)])
        conn.execute("INSERT INTO daily_labels(date, metric, label, source_scope, device_id, raw_record_id) "
                     f"VALUES('2025-06-30','hrv_status','balanced','device','{SERIAL}',1)")
        conn.executemany("INSERT INTO metric_samples(metric, ts_utc, value, source_scope, device_id, raw_record_id) "
                         "VALUES(?,?,?,'device',?,1)", [("stress", f"2025-06-{d:02d}T10:00:00Z", 30, SERIAL)
                                                       for d in range(1, 31)])
        conn.execute("INSERT INTO sleep_sessions(sleep_id, date, start_utc, end_utc, deep_s, overall_score, "
                     f"source_scope, device_id, raw_record_id) VALUES('2025-06-30|device|{SERIAL}','2025-06-30',"
                     f"'2025-06-29T21:00:00Z','2025-06-30T04:00:00Z',3600,77,'device','{SERIAL}',1)")
        conn.execute(f"INSERT INTO sleep_stages(sleep_id, stage, start_utc, end_utc) VALUES('2025-06-30|device|{SERIAL}',"
                     "'deep','2025-06-29T21:00:00Z','2025-06-29T22:00:00Z')")
        conn.execute("INSERT INTO activities(activity_id, start_utc, sport, source_scope, device_id, raw_record_id) "
                     f"VALUES('2025-06-29T16:00:00Z|device|{SERIAL}','2025-06-29T16:00:00Z','running','device','{SERIAL}',1)")
        conn.execute("INSERT INTO import_runs(started_at, transport, status, error) VALUES('2025-07-01T00:00:00Z','usb',"
                     "'failed','OSError: /Users/someone/GARMIN/Monitor/X.FIT unreadable')")
        # v2 ledger tables: a failure and a claimed window; the only free-text column is `kind`.
        conn.execute("INSERT INTO import_failures(run_id, stream, start_utc, end_utc, kind, recorded_at) "
                     f"VALUES(1,'fit:sleep','2025-06-20T20:00:00Z','2025-06-21T05:00:00Z','bad /Users/x {SERIAL}',"
                     "'2025-07-01T00:00:00Z')")
        conn.execute("INSERT INTO export_ranges(run_id, stream, from_day, to_day) VALUES(1,'json:uds','2025-06-01','2025-06-30')")
        if live:
            # real bytes (a live-link-shaped record), so the live fold produces 'live' rows the corpus walks
            import zlib
            from disconect.ingest import live as live_module
            base = int(datetime.datetime(2025, 6, 29, 10, tzinfo=utc).timestamp())
            readings = [[base + 60 * i, "heart_rate", 70 + i] for i in range(30)] + [[base + 5.5, "stress", 33]]
            _record, data = live_module.canonical_payload(readings)
            conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, device_id, start_utc, "
                         "end_utc, payload_kind, payload, payload_hash, payload_bytes, imported_at) "
                         "VALUES('json:live','c0ffee','device','ble',NULL,'2025-06-29T10:00:00Z','2025-06-29T10:30:00Z',"
                         "'json',?,'c0ffee',?,'2025-07-01T00:00:00Z')", (zlib.compress(data), len(data)))
            from disconect.ingest.writer import Writer
            Writer(conn, ClockOffsets.load(conn), "test").derive_live_samples()


def _walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            assert key not in FORBIDDEN_KEYS, f"forbidden key {key!r} at {path}"
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


CALLS = [
    ("get_data_health", {"window_days": 3650}),
    ("get_metric_series", {"metrics": ["sleep_score", "stress", "hrv_status"], "days": 60, "end_date": "2025-06-30"}),
    ("get_sleep_detail", {}),
    ("get_sleep_detail", {"date": "2025-06-30"}),
    ("list_activities", {"limit": 5}),
    ("get_period_facts", {"include_points": True}),
    ("get_contract", {}),
]


@pytest.mark.parametrize("tool,args", CALLS, ids=[c[0] for c in CALLS])
def test_tool_output_carries_no_identifiers(db_path, monkeypatch, tool, args):
    _seed(db_path)
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    from disconect import mcp_server
    result = asyncio.run(mcp_server.server.call_tool(tool, args))
    payload = result.structured_content
    assert payload is not None and not result.is_error
    for path, text in _walk(payload):
        for needle in FORBIDDEN_TEXT:
            assert needle not in text, f"{needle!r} leaked at {path}"
    assert SERIAL not in json.dumps(payload)


@pytest.mark.parametrize("tool,args", CALLS, ids=[c[0] for c in CALLS])
def test_tool_output_names_no_manufacturer(db_path, monkeypatch, tool, args):
    """``identity.neutral`` runs over every MCP tool result, as it does over ``serve``'s."""
    _seed(db_path)
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    from disconect import mcp_server
    result = asyncio.run(mcp_server.server.call_tool(tool, args))
    assert not re.search("garmin", json.dumps(result.structured_content), re.IGNORECASE)
    assert not re.search("garmin", result.content[0].text, re.IGNORECASE)


def test_the_manufacturer_is_scrubbed_where_the_contract_names_it(db_path, monkeypatch):
    """The scrub is not vacuous: the contract's own text names the vendor and comes back as the placeholder."""
    from disconect import contract, identity, mcp_server
    assert re.search("garmin", contract.SOURCE_CONVENTION, re.IGNORECASE)
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    _seed(db_path)
    result = asyncio.run(mcp_server.server.call_tool("get_contract", {}))
    assert identity.VENDOR_PLACEHOLDER in json.dumps(result.structured_content)


def test_instructions_and_tool_listing_name_no_manufacturer():
    from disconect import identity, mcp_server
    assert identity.VENDOR_PLACEHOLDER in mcp_server.INSTRUCTIONS
    assert not re.search("garmin", mcp_server.INSTRUCTIONS, re.IGNORECASE)
    for tool in asyncio.run(mcp_server.server.list_tools()):
        assert not re.search("garmin", json.dumps(tool.model_dump(mode="json")), re.IGNORECASE), tool.name


def test_every_registered_tool_is_covered():
    from disconect import mcp_server
    registered = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert registered == {name for name, _ in CALLS}, "add a privacy call for every new tool"
