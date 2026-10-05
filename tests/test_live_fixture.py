"""Bet 9b slice 3: the committed ``synthetic-live.hbdb`` (two live files imported on top of the synthetic store)
reads exactly like the same store without them, except that ``data.health`` lists the ``json:live`` stream."""

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


def test_the_committed_store_is_what_the_generator_builds_today(stores, tmp_path):
    again = tmp_path / "again.hbdb"
    gen_serve_fixtures.build(again, live=True)
    assert again.read_bytes() == FIXTURE.read_bytes(), "regenerate: python tests/gen_serve_fixtures.py --live-only"


def test_the_health_report_lists_the_live_stream_with_its_count_and_span(stores):
    with_live, without = stores
    health = Rig(with_live).result("data.health")
    assert health["streams"]["json:live"] == {"records": 2, "first": "2025-06-15T10:00:00Z",
                                              "last": "2025-06-21T00:05:00Z"}
    assert "json:live" not in Rig(without).result("data.health")["streams"]


def test_the_live_files_are_retained_and_derive_nothing(stores):
    with_live, without = stores
    assert _count(with_live, "SELECT COUNT(*) FROM raw_records WHERE stream='json:live'") == 2
    for table in ("metric_samples", "daily_metrics", "daily_labels", "monitoring_intervals", "activities"):
        sql = f"SELECT COUNT(*) FROM {table}"
        assert _count(with_live, sql) == _count(without, sql), table


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
    days = {datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat() for t, _, _ in readings}
    assert set(MIDNIGHT_DAYS) | {OVERLAP_DAY} <= days
    # the overlap day already has monitoring (FIT) samples and dailies for the metrics the live file also carries
    assert _count(with_live, "SELECT COUNT(*) FROM metric_samples WHERE ts_utc LIKE ?", OVERLAP_DAY + "%") > 0
    assert _count(with_live, "SELECT COUNT(*) FROM daily_metrics WHERE date=? AND metric='steps'", OVERLAP_DAY) > 0


@pytest.mark.parametrize("metric, scope", METRICS)
def test_data_metric_ignores_the_live_records(stores, metric, scope):
    with_live, without = stores
    for last_day in LAST_DAYS:
        args = {"metric": metric, "scope": scope, "days": 30, "last_day": last_day}
        assert Rig(with_live).result("data.metric", **args) == Rig(without).result("data.metric", **args)


def test_data_today_data_facts_and_coverage_ignore_the_live_records(stores):
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
        assert ledgers[0] == ledgers[1], last_day


def test_data_health_differs_only_by_the_live_stream_and_the_import_bookkeeping(stores):
    with_live, without = stores
    a, b = Rig(with_live).result("data.health"), Rig(without).result("data.health")
    for key in ("coverage", "metrics", "sleep", "activities", "clock_offsets_known", "source_agreement", "window"):
        assert a[key] == b[key], key
    assert {k: v for k, v in a["streams"].items() if k != "json:live"} == b["streams"]
