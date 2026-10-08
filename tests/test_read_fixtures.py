"""The Rust read paths' oracle (`disconect-core/tests/fixtures/read_paths.json`) is what Python answers today.

``gen_read_fixtures.py`` calls ``metric_series``, ``sleep_detail``, ``list_activities`` and ``period_facts``
(with every option) over the committed stores; the Rust test replays each case. This file keeps the committed
fixture and the committed ``wide.hbdb`` store honest, and pins the hazards the cases must keep exercising, so a
generator edit that quietly drops one fails here rather than weakening the Rust gate.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

import monorepo

monorepo.require()
import gen_read_fixtures as gen  # noqa: E402
from disconect import storage

CASES = json.loads(gen.FIXTURE.read_text())["cases"]


def _cases(fn, store=None, **args):
    return [case for case in CASES if case["fn"] == fn and (store is None or case["store"] == store)
            and all(case["args"].get(key) == value for key, value in args.items())]


def test_the_committed_fixture_is_what_the_generator_makes_today():
    assert gen.FIXTURE.read_text() == gen.render(gen.build()), (
        "read_paths.json is stale: run `python tests/gen_read_fixtures.py` and commit it")


def test_the_committed_wide_store_holds_the_rows_the_builder_writes():
    """Byte identity is not promised by SQLite; the rows are."""
    tables = ("daily_metrics", "daily_labels", "metric_samples", "activities", "sleep_sessions", "sleep_stages",
              "clock_offsets")
    with tempfile.TemporaryDirectory() as folder:
        rebuilt = pathlib.Path(folder) / "wide.hbdb"
        gen.build_wide_store(rebuilt)
        committed = pathlib.Path(folder) / "committed.hbdb"
        shutil.copyfile(gen.WIDE, committed)
        for table in tables:
            rows = []
            for path in (rebuilt, committed):
                conn = storage.open_read_only(path, allow_prompt=False)
                rows.append([tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")])
                conn.close()
            assert rows[0] == rows[1], table


def test_the_counts_per_function_are_what_the_rust_test_expects():
    data = json.loads(gen.FIXTURE.read_text())
    rust = (pathlib.Path(gen.FIXTURE).parents[2] / "tests" / "read_paths_test.rs").read_text()
    for fn in gen.FNS:
        total = sum(case["fn"] == fn for case in CASES)
        errors = sum(case["fn"] == fn and "error" in case["result"] for case in CASES)
        assert (data["counts"][fn], data["errors"][fn]) == (total, errors)
        assert f'("{fn}", {total}, {errors})' in rust, f"read_paths_test.rs EXPECTED is stale for {fn}"


def test_every_function_and_every_store_is_covered_and_each_error_class_occurs():
    assert {case["fn"] for case in CASES} == set(gen.FNS)
    for store in gen.STORES:
        for fn in ("metric_series", "sleep_detail", "list_activities", "period_facts"):
            assert _cases(fn, store), (fn, store)
    classes = {case["result"]["class"] for case in CASES if "error" in case["result"]}
    # OverflowError left the fixture with the lower-end ``date − n`` row (2026-10-08): every window at the
    # calendar's ends is clamped, so a read answers; the generator still records the class if one returns
    assert classes == {"ValueError"}
    # the Python text a ToolError carries
    texts = {case["result"]["error"] for case in CASES if case["result"].get("class") == "ValueError"}
    assert {"end_date must be YYYY-MM-DD", "date must be YYYY-MM-DD",
            "source_scope must be one of ('device', 'vendor_cloud', 'local', 'live')"} <= texts


def test_the_hazards_of_the_pitch_are_in_the_cases():
    sleep = _cases("sleep_detail", "synthetic", date="2025-06-30")[0]["result"]["sessions"][0]
    assert sleep["overall_score"] == 77 and type(sleep["overall_score"]) is int
    assert sleep["retro"] is False and "deep" in sleep["stage_minutes"] and "deep_s" not in sleep
    keys = list(sleep)
    assert keys.index("retro") < keys.index("stage_minutes"), "retro keeps its place in the dict"
    wide = _cases("sleep_detail", "wide", date="2025-06-20")[0]["result"]["sessions"]
    assert wide[0]["retro"] is True and wide[0]["overall_score"] == 76.5 and wide[1]["overall_score"] == 70
    assert wide[0]["stage_minutes"]["deep"] == 90.0 and len(wide[0]["stages"]) == 7
    partial = _cases("sleep_detail", "wide", date="2025-06-21")[0]["result"]["sessions"][0]
    assert partial["stage_minutes"]["light"] == 60.0 and "deep" not in partial["stage_minutes"]
    listed = _cases("list_activities", "wide", limit=25)[0]["result"]["activities"]
    assert {type(item["calories_kcal"]) for item in listed if "calories_kcal" in item} == {int}
    assert any("calories_kcal" not in item for item in listed), "an absent column is absent, never null"
    sizes = {limit: len(_cases("list_activities", "wide", limit=limit)[0]["result"]["activities"])
             for limit in (0, 1, 20, 25, 26, 200, 201)}
    assert sizes == {0: 1, 1: 1, 20: 20, 25: 25, 26: 25, 200: 25, 201: 25}


def test_the_per_cadence_caps_make_two_series_of_one_answer_start_on_different_days():
    answer = _cases("metric_series", "wide", days=1826, end_date="2025-06-30",
                    metrics=["steps", "stress", "hrv_status"])[0]["result"]
    by_metric = {entry["metric"]: entry for entry in answer["series"]}
    assert by_metric["steps"]["from"] != by_metric["stress"]["from"]
    assert answer["labels"][0]["from"] == by_metric["steps"]["from"]
    assert by_metric["steps"]["to"] == by_metric["stress"]["to"] == "2025-06-30"


def test_labels_follow_the_raw_list_series_are_deduplicated_and_unknown_names_are_ignored():
    answer = next(case for case in CASES if case["fn"] == "metric_series" and case["store"] == "synthetic"
                  and case["args"].get("metrics", [None])[:1] == ["steps"] and "hrv_status" in case["args"]["metrics"]
                  and "bogus" in case["args"]["metrics"] and len(case["args"]["metrics"]) == 14)["result"]
    assert [item["metric"] for item in answer["labels"]] == ["hrv_status", "hrv_status", "training_readiness_level"]
    assert [item["metric"] for item in answer["series"]].count("steps") <= 3  # once per scope, not per repeat
    assert "bogus" in answer["ignored_metrics"] and "hrv_status" not in answer["ignored_metrics"]


def test_period_facts_options_show_in_the_cases():
    def facts(store, **args):
        return _cases("period_facts", store, **args)
    no_data = [fact for case in facts("synthetic") for fact in case["result"].get("facts", [])
               if fact["reason_code"] == "no_data"]
    assert no_data and all(list(fact["evidence"]) == ["baseline_from", "baseline_to"] for fact in no_data)
    assert {fact["source_scope"] for fact in no_data} >= {"any", "device"}
    ordered = facts("synthetic", metrics=["sleep_score", "steps", "steps", "stress", "heart_rate", "steps"])[0]
    assert [fact["metric"] for fact in ordered["result"]["facts"]] == [
        "sleep_score", "sleep_score", "steps", "steps", "stress", "heart_rate"], "request order, not contract order"
    points = [fact for case in facts("synthetic", include_points=True) for fact in case["result"].get("facts", [])
              if "window_points" in fact["evidence"]]
    assert points
    empty = facts("empty", metrics=["steps", "steps", "bogus", "hrv_status"])[0]["result"]
    assert empty["ignored_metrics"] == ["steps", "steps", "bogus", "hrv_status"] and empty["facts"] == []
    assert facts("empty", metrics=[])[0]["result"]["ignored_metrics"] == []
