"""Bets 9b/9b-2: the committed ``synthetic-live.hbdb`` (four live files imported on top of the synthetic store)
reads exactly like the same store without them, except at source scope ``live`` -- the fold's rows -- and in
``data.health``'s stream list and ``live`` block."""

import datetime
import json
import zlib

import pytest

import gen_serve_fixtures
from disconect import coverage, storage
from test_serve import Rig

FIXTURE = gen_serve_fixtures.STORE_LIVE
METRICS = [("steps", "local"), ("heart_rate", "device"), ("stress", "device"), ("respiration_rate", "device"),
           ("spo2", "device"), ("energy_reserve", "device"), ("steps", "vendor_cloud"), ("stress_avg", "local")]
LIVE_METRICS = ["heart_rate", "stress", "respiration_rate", "spo2", "energy_reserve"]
#: The overlap session's day, which already has monitoring rows, and the day pair the midnight session spans.
OVERLAP_DAY, MIDNIGHT_DAYS = "2025-06-15", ("2025-06-20", "2025-06-21")
LAST_DAYS = (OVERLAP_DAY, MIDNIGHT_DAYS[1], "2025-06-30")


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    """(with live, without live): the committed store, and the generator's build of the same store minus the live files."""
    mp = pytest.MonkeyPatch()
    mp.setenv("DISCONECT_NOW", gen_serve_fixtures.PINNED_NOW)
    folder = tmp_path_factory.mktemp("live-fixture")
    with_live, without = folder / "with.hbdb", folder / "without.hbdb"
    with_live.write_bytes(FIXTURE.read_bytes())
    gen_serve_fixtures.build(without)
    yield with_live, without
    mp.undo()


def _count(store, sql, *args):
    conn = storage.open_read_only(store)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def _dump(store) -> str:
    """The store's SQL text: row order and content, not the file's page layout."""
    conn = storage.open_read_only(store)   # this driver's connection has no iterdump: the same text, by hand
    try:
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        lines = [f"{kind} {name}: {sql}" for kind, name, sql in schema]
        for name in (n for kind, n, _ in schema if kind == "table"):
            lines += [f"{name} {tuple(row)!r}" for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        return "\n".join(lines)
    finally:
        conn.close()


def test_the_committed_store_is_what_the_generator_builds_today(stores, tmp_path):
    again = tmp_path / "again.hbdb"
    gen_serve_fixtures.build(again, live=True)
    assert _dump(again) == _dump(FIXTURE), "regenerate: python tests/gen_serve_fixtures.py --live-only"


def test_the_health_report_lists_the_live_stream_and_the_live_block(stores):
    with_live, without = stores
    health = Rig(with_live).result("data.health")
    assert health["streams"]["json:live"] == {"records": 4, "first": "2025-03-06T01:00:00Z",
                                              "last": "2025-06-21T00:05:00Z"}
    assert health["live"] == {"records": 4, "samples": 37, "first_day": "2025-03-05", "last_day": "2025-06-21"}
    plain = Rig(without).result("data.health")
    assert "json:live" not in plain["streams"]
    assert plain["live"] == {"records": 0, "samples": 0, "first_day": None, "last_day": None}


def test_the_live_files_fold_only_into_live_scope_samples(stores):
    with_live, without = stores
    assert _count(with_live, "SELECT COUNT(*) FROM raw_records WHERE stream='json:live'") == 4
    for table in ("daily_metrics", "daily_labels", "monitoring_intervals", "activities"):
        sql = f"SELECT COUNT(*) FROM {table}"
        assert _count(with_live, sql) == _count(without, sql), table
    sql = "SELECT COUNT(*) FROM metric_samples WHERE source_scope != 'live'"
    assert _count(with_live, sql) == _count(without, sql)
    assert _count(without, "SELECT COUNT(*) FROM metric_samples WHERE source_scope = 'live'") == 0
    assert _count(with_live, "SELECT COUNT(*) FROM metric_samples WHERE source_scope = 'live'") == 37
    # one row per metric and UTC minute, no device, the lower median where a minute held two values
    assert _count(with_live, "SELECT COUNT(*) FROM metric_samples WHERE source_scope='live' AND "
                             "(device_id IS NOT NULL OR substr(ts_utc, 18, 2) != '00')") == 0
    assert _count(with_live, "SELECT value FROM metric_samples WHERE source_scope='live' AND metric='heart_rate' "
                             "AND ts_utc='2025-06-15T10:11:00Z'") == 111.0


def test_the_fixture_holds_the_cases_it_was_built_for(stores):
    with_live, without = stores
    conn = storage.open_read_only(with_live)
    try:
        readings = [r for (blob,) in conn.execute("SELECT payload FROM raw_records WHERE stream='json:live'")
                    for r in json.loads(zlib.decompress(blob))["readings"]]
    finally:
        conn.close()
    assert {type(t) for t, _, _ in readings} == {int, float}
    assert {m for _, m, _ in readings} == {"heart_rate", "steps", "stress", "respiration_rate", "spo2", "energy_reserve"}
    assert [r for r in readings if r[1] == "stress" and r[2] < 0] and [r for r in readings if r[1] == "spo2" and r[2] == 0]
    days = {datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat() for t, _, _ in readings}
    assert set(MIDNIGHT_DAYS) | {OVERLAP_DAY} <= days
    # the overlap day already has monitoring (FIT) samples and dailies for the metrics the live file also carries
    assert _count(with_live, "SELECT COUNT(*) FROM metric_samples WHERE source_scope='device' AND ts_utc LIKE ?",
                  OVERLAP_DAY + "%") > 0
    assert _count(with_live, "SELECT COUNT(*) FROM daily_metrics WHERE date=? AND metric='steps'", OVERLAP_DAY) > 0


@pytest.mark.parametrize("metric, scope", METRICS)
def test_data_metric_outside_the_live_scope_ignores_the_live_records(stores, metric, scope):
    with_live, without = stores
    for last_day in LAST_DAYS:
        args = {"metric": metric, "scope": scope, "days": 30, "last_day": last_day}
        assert Rig(with_live).result("data.metric", **args) == Rig(without).result("data.metric", **args)


@pytest.mark.parametrize("metric", LIVE_METRICS)
def test_data_metric_at_the_live_scope_holds_the_session_minutes(stores, metric):
    with_live, without = stores
    args = {"metric": metric, "scope": "live", "days": 30, "last_day": MIDNIGHT_DAYS[1]}
    folded = Rig(with_live).result("data.metric", **args)
    # the calendar carries the day's mean of the folded minutes, and the ledger calls the day present;
    # the midnight session's minutes land on the local day the clock offsets give, so only the days are pinned
    held = [(d["day"], d["status"]) for d in folded["days"] if d["value"] is not None]
    assert held and all(status == "present" for _, status in held), folded
    assert {day for day, _ in held} <= {OVERLAP_DAY, *MIDNIGHT_DAYS}
    assert not [d for d in Rig(without).result("data.metric", **args)["days"] if d["value"] is not None]


def test_data_today_data_facts_and_the_declared_coverage_rows_ignore_the_live_records(stores):
    with_live, without = stores
    for method in ("data.today", "data.facts"):
        assert Rig(with_live).result(method) == Rig(without).result(method), method
    for last_day in LAST_DAYS:
        ledgers = []
        for store in stores:
            conn = storage.open_read_only(store)
            try:
                ledgers.append(coverage.ledger(conn, last_day, 30))
            finally:
                conn.close()
        with_rows, without_rows = ([r for r in ledger["ledger"] if r["source_scope"] != "live"] for ledger in ledgers)
        assert with_rows == without_rows, last_day
        assert not [r for r in ledgers[1]["ledger"] if r["source_scope"] == "live"]
        live_rows = [r for r in ledgers[0]["ledger"] if r["source_scope"] == "live"]
        assert {r["metric"] for r in live_rows} == set(LIVE_METRICS)
        assert all(r["source_empty"] == 0 and r["failed"] == 0 for r in live_rows)
        # the synthetic store's own drift rows stay as they are; a session never adds one
        assert ledgers[0]["map_drift"] == ledgers[1]["map_drift"]
        assert not [r for r in ledgers[0]["map_drift"] if r["source_scope"] == "live" or r["stream"] == "json:live"]


def test_data_health_differs_only_by_the_live_stream_block_and_the_import_bookkeeping(stores):
    with_live, without = stores
    a, b = Rig(with_live).result("data.health"), Rig(without).result("data.health")
    for key in ("metrics", "sleep", "activities", "clock_offsets_known", "source_agreement", "window"):
        assert a[key] == b[key], key
    assert {k: v for k, v in a["streams"].items() if k != "json:live"} == b["streams"]
    assert [r for r in a["coverage"]["ledger"] if r["source_scope"] != "live"] == b["coverage"]["ledger"]
