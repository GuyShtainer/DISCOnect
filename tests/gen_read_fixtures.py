#!/usr/bin/env python3
"""Build what the Rust read paths are held to: ``disconect-core/tests/fixtures/read_paths.json``.

    python tests/gen_read_fixtures.py             # rewrite the fixture
    python tests/gen_read_fixtures.py --check     # exit 1 when the committed file is not what this makes today
    python tests/gen_read_fixtures.py --build-store   # (re)build tests/fixtures/read/wide.hbdb (committed once)

Bet 11e, slice 1. The MCP server answers four read paths: ``queries.metric_series``, ``queries.sleep_detail``,
``queries.list_activities`` and ``insight.period_facts`` with its options (``end_date``, ``metrics``,
``source_scope``, ``include_points``). This calls the Python functions **directly** (no MCP) over the committed
stores and records, per case, ``{store, fn, args, result}``: the value after ``identity.neutral``, exactly what
``mcp_server._neutral_result`` hands the transport, so a result equals a transcript's ``structuredContent``.
A call that raises ``ValueError`` or ``OverflowError`` is ``result = {"error": <text>, "class": <name>}``: the
``ValueError`` text becomes a ``ToolError`` text over MCP, an ``OverflowError`` (no usable text) becomes
``Error executing tool <name>``. ``parse_day`` and ``_window`` are recorded on their own (``store`` null).

Pins, as for the MCP transcripts: ``DISCONECT_NOW`` (``tools/mcp_diff.NOW``) and ``TZ=Pacific/Chatham`` for the
block only. Stores: the committed serve stores (current schema, schema v1, never imported), the privacy-test seed
store, and ``wide.hbdb`` (synthetic too: 25 activities, sleep nights with absent columns, a float in an INTEGER
column, a retro night, a long stage timeline, 14 months of a daily metric and a sample metric, a daily metric of tiny floats), which exists so
the clamps, the per-cadence caps and the key order have something to bite on.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import pathlib
import random
import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import monorepo  # noqa: E402
from disconect import contract, identity, insight, queries, storage  # noqa: E402

FIXTURE = monorepo.CRATE / "tests" / "fixtures" / "read_paths.json"
SERVE_FIXTURES = HERE / "fixtures" / "serve"
MCP_FIXTURES = HERE / "fixtures" / "mcp"
WIDE = HERE / "fixtures" / "read" / "wide.hbdb"
PINNED_NOW = "2025-07-02T09:30:00Z"      # tools/serve_diff.NOW, the clock of every transcript
PINNED_TZ = "Pacific/Chatham"
SEED = 20261003

#: store name -> committed file; the first two get the full case set, the rest a short one.
STORES = {
    "synthetic": SERVE_FIXTURES / "synthetic.hbdb",
    "synthetic-v1": SERVE_FIXTURES / "synthetic-v1.hbdb",
    "empty": SERVE_FIXTURES / "empty.hbdb",
    "privacy-seed": MCP_FIXTURES / "privacy-seed.hbdb",
    "wide": WIDE,
}
FULL = ("synthetic", "wide")
FNS = ("metric_series", "sleep_detail", "list_activities", "period_facts", "parse_day", "window")


@contextlib.contextmanager
def pinned():
    """``DISCONECT_NOW`` and ``TZ`` for the block only: a test must not pin the clock for the suite."""
    before = {name: os.environ.get(name) for name in ("DISCONECT_NOW", "TZ")}
    os.environ["DISCONECT_NOW"] = PINNED_NOW
    os.environ["TZ"] = PINNED_TZ
    time.tzset()
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        time.tzset()


# ---- the wide store ----

def build_wide_store(target: pathlib.Path) -> None:
    """A synthetic store with the shapes the three small stores lack. Seeded: the same rows every run."""
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "wide.hbdb"
        shutil.copyfile(SERVE_FIXTURES / "empty.hbdb", path)
        with storage.open_for_write(path, "fixture") as conn:
            start = datetime.date(2024, 5, 1)
            for offset in range(426):                               # daily metric, 14 months
                day = (start + datetime.timedelta(days=offset)).isoformat()
                conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES (?,?,?,?)",
                             (day, "steps", round(rng.gauss(8000, 2500), rng.choice((0, 1, 4))), "local"))
                if offset % 3 == 0:
                    conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES (?,?,?,?)",
                                 (day, "resting_heart_rate", rng.randint(48, 62) + rng.choice((0.0, 0.5, 0.123456)),
                                  "device"))
                if offset % 5 == 0:
                    conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES (?,?,?,?)",
                                 (day, "resting_heart_rate", rng.randint(48, 62) + 0.25, "vendor_cloud"))
                if offset % 40 == 0:
                    conn.execute("INSERT INTO daily_labels(date, metric, label, source_scope) VALUES (?,?,?,?)",
                                 (day, "hrv_status", rng.choice(("BALANCED", "LOW", "UNBALANCED")), "device"))
            conn.execute("INSERT INTO clock_offsets(ts_utc, offset_s) VALUES (?,?)", ("2024-05-01T00:00:00Z", 45900))
            conn.execute("INSERT INTO clock_offsets(ts_utc, offset_s) VALUES (?,?)", ("2025-03-01T00:00:00Z", 49500))
            moment = datetime.datetime(2024, 5, 1, 3, 17, 5, tzinfo=datetime.timezone.utc)
            for _ in range(1300):                                   # sample metric, 3 a day, jittered, 2 scopes
                moment += datetime.timedelta(minutes=rng.randint(200, 460), seconds=rng.randint(0, 59))
                conn.execute("INSERT OR IGNORE INTO metric_samples(metric, ts_utc, value, source_scope) "
                             "VALUES (?,?,?,?)", ("stress", moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                  round(rng.gauss(35, 12), rng.choice((0, 2, 3))),
                                                  "device" if rng.random() < 0.8 else "local"))
            for index in range(25):                                 # activities, newest last inserted first
                stamp = datetime.datetime(2025, 5, 1, 6, 30, 0) + datetime.timedelta(days=index, minutes=index * 7)
                conn.execute(
                    "INSERT INTO activities(activity_id, start_utc, end_utc, sport, sub_sport, total_timer_s, "
                    "total_elapsed_s, distance_m, calories_kcal, avg_hr, max_hr, avg_speed_mps, total_ascent_m, "
                    "total_descent_m, source_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"a{index}", stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     None if index % 7 == 3 else (stamp + datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     rng.choice(("running", "cycling", "walking")), None if index % 4 == 0 else "generic",
                     3600.0 if index % 5 == 0 else 2700.123456, 2750.5,
                     None if index % 6 == 1 else round(rng.uniform(1000, 15000), 5),
                     rng.choice((None, 250, 410)), rng.choice((None, 120, 151)), 171 if index % 2 else None,
                     rng.choice((None, 2.78125, 3.3333333)), 0.0 if index % 9 == 0 else 12.3456, None,
                     rng.choice(("device", "vendor_cloud", "local"))))
            night = ("2025-06-20", "device", "2025-06-19T14:05:00Z", "2025-06-19T22:11:30Z", 5400, 14400, 6300, 90,
                     60, 76.5, 80, 71, 66, 55, 60, 82, 90, 75, 70, 68, 64, 2, 31.5, 95.5, 88, 54.5, 14.25, 11.0,
                     17.75, 1)
            columns = ("date, source_scope, start_utc, end_utc, deep_s, light_s, rem_s, awake_s, unmeasurable_s, "
                       "overall_score, quality_score, duration_score, recovery_score, deep_score, rem_score, "
                       "light_score, awake_time_score, awakenings_count_score, combined_awake_score, "
                       "restlessness_score, interruptions_score, awakenings_count, avg_stress, avg_spo2, "
                       "lowest_spo2, avg_hr, avg_respiration, lowest_respiration, highest_respiration, retro, "
                       "sleep_id")
            conn.execute(f"INSERT INTO sleep_sessions({columns}) VALUES ({','.join('?' * 31)})",
                         night + ("2025-06-20|device|",))
            conn.execute("INSERT INTO sleep_sessions(date, source_scope, deep_s, overall_score, retro, sleep_id) "
                         "VALUES (?,?,?,?,?,?)", ("2025-06-20", "vendor_cloud", 0, 70, 0, "2025-06-20|vendor_cloud|"))
            conn.execute("INSERT INTO sleep_sessions(date, source_scope, start_utc, end_utc, light_s, awake_s, "
                         "overall_score, retro, sleep_id) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("2025-06-21", "local", "2025-06-20T21:00:00Z", "2025-06-21T05:00:00Z", 3599, 91, 80, 0,
                          "2025-06-21|local|"))
            moment = datetime.datetime(2025, 6, 19, 14, 5, 0)
            for stage, minutes in (("light", 20), ("deep", 35), ("light", 45), ("rem", 22), ("awake", 3),
                                   ("light", 61), ("rem", 33)):
                end = moment + datetime.timedelta(minutes=minutes, seconds=rng.choice((0, 0, 30)))
                conn.execute("INSERT INTO sleep_stages(sleep_id, stage, start_utc, end_utc) VALUES (?,?,?,?)",
                             ("2025-06-20|device|", stage, moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              end.strftime("%Y-%m-%dT%H:%M:%SZ")))
                moment = end
            # a daily metric of tiny stored values (some below 1e-4, one below 1e-6, two signed zeros): the floats
            # that pydantic and json.dumps spell differently (``0.00003`` / ``3e-05``, ``1.5e-7`` / ``1.5e-07``)
            tiny = (3e-05, -4.5e-05, 1.5e-07, 9.9e-06, 0.000123, 2.5e-05, 0.0, 6.5e-06, -1.25e-05, -0.0)
            for offset in range(45):
                day = (datetime.date(2025, 5, 17) + datetime.timedelta(days=offset)).isoformat()
                conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES (?,?,?,?)",
                             (day, "skin_temp_deviation", tiny[offset % len(tiny)], "device"))
            # and one metric whose values cross the other threshold (>= 1e16 is exponent form in both libraries)
            for offset, value in enumerate((52.5, 1.5e22, 52.25, 9999999999999998.0, 1e16, 52.0, 2.5e17)):
                day = (datetime.date(2025, 6, 24) + datetime.timedelta(days=offset)).isoformat()
                conn.execute("INSERT INTO daily_metrics(date, metric, value, source_scope) VALUES (?,?,?,?)",
                             (day, "vo2max", value, "local"))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for leftover in path.parent.glob("wide.hbdb-*"):
            assert leftover.stat().st_size == 0, f"{leftover.name} still holds data"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


# ---- recording ----

def _record(call):
    """``{"result": value}`` or ``{"result": {"error", "class"}}`` for the two exceptions the paths raise."""
    try:
        return identity.neutral(call())
    except (ValueError, OverflowError) as exc:
        return {"error": str(exc), "class": type(exc).__name__}


def _case(store, fn, args, result):
    return {"store": store, "fn": fn, "args": args, "result": result}


def _empty_metrics(conn, count: int) -> list[str]:
    """Contract metrics with no rows of any kind in this store (the ``no_data`` facts)."""
    out = []
    for name in contract.metric_names():
        have = conn.execute("SELECT 1 FROM daily_metrics WHERE metric=? LIMIT 1", (name,)).fetchone() \
            or conn.execute("SELECT 1 FROM metric_samples WHERE metric=? LIMIT 1", (name,)).fetchone()
        if not have:
            out.append(name)
        if len(out) == count:
            break
    return out


def _cases_for(name: str, conn) -> list[dict]:
    full = name in FULL
    out: list[dict] = []

    def series(**args):
        out.append(_case(name, "metric_series", args, _record(lambda: queries.metric_series(conn, **args))))

    def sleep(**args):
        out.append(_case(name, "sleep_detail", args, _record(lambda: queries.sleep_detail(conn, **args))))

    def activities(**args):
        out.append(_case(name, "list_activities", args, _record(lambda: queries.list_activities(conn, **args))))

    def facts(**args):
        out.append(_case(name, "period_facts", args, _record(lambda: insight.period_facts(conn, **args))))

    mixed = ["steps", "heart_rate", "steps", "hrv_status", "bogus", "hrv_status", "training_readiness_level",
             "resting_heart_rate", "stress", "heart_rate", "Steps", "", "\ud800", "garmin_steps"]
    # -- metric_series
    series(metrics=["steps"])
    series(metrics=["heart_rate"])
    series(metrics=["hrv_status"])
    series(metrics=mixed)
    series(metrics=[])
    series(metrics=["bogus", "other"])
    series(metrics=["hrv_status", "hrv_status", "steps"], days=30)
    for scope in ("device", "vendor_cloud", "local"):
        series(metrics=mixed, days=60, source_scope=scope)
    for scope in ("cloud", "", "Device", "\ud800"):
        series(metrics=["steps"], source_scope=scope)
    series(metrics=["steps", "heart_rate", "hrv_status"], days=30, end_date="2025-06-30")
    for days in ((1, 90, 366, 367, 1825, 1826, 0, -5, 10 ** 30) if full else (1, 366, 1826)):
        series(metrics=["steps", "heart_rate", "resting_heart_rate", "hrv_status"], days=days)
        series(metrics=["steps", "stress", "hrv_status"], days=days, end_date="2025-06-30")
    for end in ("2025-06-15", "", None, "2030-01-01", "1999-01-01", "2025-06-30", "2025-02-28"):
        series(metrics=["steps", "stress", "hrv_status"], days=40, end_date=end)
    for end in ("20250630", "2025-6-30", "2025-02-30", " 2025-06-30", "2025-06-30\n", "\ud800", "٢٠٢٥-٠٦-٣٠",
                "２０２５-０６-３０", "not a date", "0000-01-01", "2025-13-01"):
        series(metrics=["steps"], end_date=end)
    # Every window at the calendar's ends is clamped (``coverage.earlier`` at the start since 2026-10-08, the
    # over-fetch windows at both ends before that), so these answer instead of raising; the ``OverflowError``
    # arm above is kept for a regression (none of the recorded cases raises it today).
    series(metrics=["steps"], end_date="9999-12-31")
    series(metrics=["steps"], end_date="9999-12-31", days=1)
    series(metrics=["hrv_status"], end_date="9999-12-31", days=400)
    series(metrics=["heart_rate"], end_date="9999-12-31", days=1)
    series(metrics=["heart_rate"], end_date="9999-12-30", days=1)
    series(metrics=["heart_rate"], end_date="9999-12-29", days=2)
    series(metrics=["steps", "heart_rate"], end_date="9999-12-29", days=2)
    series(metrics=["steps"], end_date="0001-01-01", days=1)
    series(metrics=["steps"], end_date="0001-01-01", days=2)
    series(metrics=["hrv_status"], end_date="0001-01-01", days=1)
    series(metrics=["hrv_status"], end_date="0001-01-01", days=2)
    series(metrics=["heart_rate"], end_date="0001-01-01", days=1)
    series(metrics=["bogus"], end_date="0001-01-01", days=5)
    series(metrics=["bogus"], end_date="9999-12-31")
    series(metrics=["bogus"], end_date="oops")
    series(metrics=["bogus", "hrv_status"], end_date="oops")
    series(metrics=["bogus"], source_scope="oops")
    series(metrics=["bogus"], end_date="2025-06-30", days=2, source_scope="local")
    if full:
        series(metrics=contract.metric_names() + contract.label_names(), days=45)
        series(metrics=contract.metric_names(), days=45, end_date="2025-06-30", source_scope="vendor_cloud")
        series(metrics=["steps"], days=1, end_date="2024-05-01")
        series(metrics=["stress"], days=366, end_date="2025-06-30")
        series(metrics=["stress"], days=367, end_date="2025-06-30", source_scope="local")
    # -- sleep_detail
    sleep()
    sleep(date=None)
    for date in ("2025-06-15", "2025-06-16", "2025-06-30", "2025-06-20", "2025-06-21", "2025-01-01", "9999-12-31",
                 "0001-01-01", "2024-02-29", ""):
        sleep(date=date)
    for date in ("20250630", "2025-6-30", "2025-02-30", "2025-02-29", " 2025-06-30", "2025-06-30 ", "2025-06-30\n",
                 "2025-W27-2", "\ud800", "٢٠٢٥-٠٦-٣٠", "２０２５-０６-３０", "x", "0000-01-01", "2025-13-01"):
        sleep(date=date)
    # -- list_activities
    activities()
    for limit in ((0, 1, 2, 20, 24, 25, 26, 199, 200, 201, -1, 10 ** 30, -10 ** 30) if full else (0, 1, 20, 201)):
        activities(limit=limit)
    # -- period_facts
    facts()
    for window_days, baseline_days in ((1, 1), (1, 31), (31, 365), (7, 28), (32, 366), (0, 0), (-3, -4),
                                       (10 ** 30, 10 ** 30), (3, 400), (14, 7)):
        facts(window_days=window_days, baseline_days=baseline_days)
    known_empty = _empty_metrics(conn, 3)
    facts(metrics=["steps", "resting_heart_rate", "heart_rate", "sleep_score"])
    facts(metrics=["sleep_score", "steps", "steps", "stress", "heart_rate", "steps"], include_points=True)
    facts(metrics=["steps", "bogus", "hrv_status", "garmin_x", "\ud800", ""], window_days=14)
    facts(metrics=known_empty + ["steps"])
    facts(metrics=known_empty, source_scope="device", include_points=True)
    facts(metrics=["steps", "heart_rate"], source_scope="device")
    facts(metrics=["steps", "heart_rate", "resting_heart_rate"], source_scope="vendor_cloud", include_points=True)
    facts(metrics=["steps", "steps", "bogus", "hrv_status"])      # an empty store echoes these, repeats included
    facts(metrics=["steps", "steps", "bogus", "hrv_status"], end_date="2025-06-30")
    facts(metrics=[])
    facts(metrics=[], include_points=True)
    facts(metrics=None, include_points=True)
    facts(end_date="")
    facts(end_date="", metrics=["steps"])
    facts(end_date="2030-01-01")
    facts(end_date="2030-01-01", metrics=["steps", "heart_rate", "bogus"])
    facts(end_date="2025-06-15", include_points=True)
    facts(end_date="2024-01-01", metrics=["steps"])
    facts(end_date="2025-06-30", window_days=1, baseline_days=1, include_points=True)
    for scope in ("device", "vendor_cloud", "local"):
        facts(source_scope=scope)
        facts(source_scope=scope, include_points=True, window_days=14, baseline_days=60)
    for include_points in (True, False):
        facts(include_points=include_points, window_days=21, baseline_days=90)
    for scope in ("cloud", "", "Local", "\ud800"):
        facts(source_scope=scope)
        facts(source_scope=scope, end_date="oops")
    for end in ("20250630", "2025-6-30", "2025-02-30", " 2025-06-30", "\ud800", "not a date", "2025-13-01",
                "0000-01-01", "２０２５-０６-３０"):
        facts(end_date=end)
        facts(end_date=end, metrics=["steps"])
    facts(end_date="9999-12-31")
    facts(end_date="9999-12-31", metrics=["steps"])
    facts(end_date="9999-12-31", metrics=["steps"], window_days=1, baseline_days=1)
    facts(end_date="9999-12-30", metrics=["heart_rate"])
    facts(end_date="9999-12-29", metrics=["steps", "heart_rate"], window_days=1, baseline_days=1)
    facts(end_date="0001-01-01", metrics=["steps"])
    facts(end_date="0001-01-01", metrics=["steps"], window_days=1)
    facts(end_date="0001-01-01")
    facts(end_date="0001-01-02", metrics=["steps"], window_days=1, baseline_days=1)
    facts(end_date="0001-01-02", metrics=["steps"], window_days=2, baseline_days=1)
    facts(end_date="0001-01-03", metrics=["heart_rate"], window_days=1, baseline_days=1)
    facts(end_date="0001-01-04", metrics=["heart_rate"], window_days=1, baseline_days=1)
    if full:
        facts(metrics=contract.metric_names() + contract.label_names(), include_points=True, window_days=14)
        facts(metrics=contract.metric_names(), end_date="2025-06-30", source_scope="local")
        facts(window_days=30, baseline_days=365, include_points=True)
        facts(metrics=["steps", "stress"], end_date="2025-06-30", window_days=31, baseline_days=365)
    return out


PARSE_DAY_TEXTS = ("2025-07-01", "", "20250701", "2025-7-1", "2025-02-30", "2025-02-29", "2024-02-29", "9999-12-31",
                   "0001-01-01", "0000-01-01", "2025-13-01", "2025-07-00", "٢٠٢٥-٠٧-٠١", "２０２５-０７-０１",
                   " 2025-07-01", "2025-07-01 ", "2025-07-01\n", "\n2025-07-01", "2025-W27-2", "2025-07-01T00:00",
                   "\ud800", "2025-07-0\ud800", "2025/07/01", "+025-07-01", "-001-07-01", "2025-07-1x", "x")


def _store_free_cases() -> list[dict]:
    out = []
    for text in PARSE_DAY_TEXTS:
        for label in ("end_date", "date"):
            out.append(_case(None, "parse_day", {"text": text, "name": label},
                             _record(lambda: {"date": queries.parse_day(text, label).isoformat()})))
    for end in ("2025-07-01", "", None, "9999-12-31", "9999-12-30", "0001-01-01", "0001-01-02", "0001-01-03",
                "2025-02-30", "bad"):
        for days in (1, 2, 90, 366, 1825, 1826, 0, -4, 10 ** 30):
            for cap in (queries.MAX_DAILY_DAYS, queries.MAX_SAMPLE_DAYS):
                out.append(_case(None, "window", {"days": days, "end_date": end, "cap": cap},
                                 _record(lambda: dict(zip(("from", "to"), queries._window(days, end, cap))))))
    return out


def build() -> dict:
    cases: list[dict] = []
    with pinned(), tempfile.TemporaryDirectory() as folder:
        for name, source in STORES.items():
            copy = pathlib.Path(folder) / f"{name}.hbdb"
            shutil.copyfile(source, copy)
            conn = storage.open_read_only(copy, allow_prompt=False)
            try:
                cases.extend(_cases_for(name, conn))
            finally:
                conn.close()
        cases.extend(_store_free_cases())
    counts = {fn: sum(case["fn"] == fn for case in cases) for fn in FNS}
    errors = {fn: sum(case["fn"] == fn and "error" in case["result"] for case in cases) for fn in FNS}
    return {
        "_note": "Generated by disconect/tests/gen_read_fixtures.py: the Python read paths called directly over the "
                 "committed stores under DISCONECT_NOW=" + PINNED_NOW + " and TZ=" + PINNED_TZ + ", after "
                 "identity.neutral. result = the return value, or {error, class} for ValueError/OverflowError. "
                 "Synthetic data only.",
        "now": PINNED_NOW,
        "counts": counts,
        "errors": errors,
        "cases": cases,
    }


def render(data: dict) -> str:
    return json.dumps(data, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file differs")
    parser.add_argument("--build-store", action="store_true", help="(re)build tests/fixtures/read/wide.hbdb")
    args = parser.parse_args()
    if args.build_store:
        build_wide_store(WIDE)
        print(f"wrote {WIDE.name}: {WIDE.stat().st_size} bytes")
        return 0
    text = render(build())
    if args.check:
        same = FIXTURE.exists() and FIXTURE.read_text() == text
        print("read_paths.json is current" if same else "read_paths.json is STALE")
        return 0 if same else 1
    FIXTURE.write_text(text)
    data = json.loads(text)
    print(f"wrote {FIXTURE.name}: {len(data['cases'])} cases, counts {data['counts']}, errors {data['errors']}, "
          f"{FIXTURE.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
