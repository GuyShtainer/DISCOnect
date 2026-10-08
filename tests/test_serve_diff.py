"""The two-core differential harness (``tools/serve_diff.py``) tests itself, and the oracle it feeds.

* the tagged-tree comparison tells ``1``, ``1.0`` and ``True`` apart and names a JSON path, never a value;
* the copied privacy constants have not drifted from ``test_privacy``;
* a core that differs in one type is caught by a real run (a stand-in for the Rust binary);
* the committed oracle responses and ledger answers are what the Python core answers today;
* the script's anchor days are the ones its stores give, and no request is booked (the last allowance is retired);
* when the Rust debug binary exists, the real gate runs on the committed synthetic stores (current and
  schema v1) and under further clock pins.
"""

import datetime
import gzip
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

import monorepo

serve_diff = monorepo.harness("serve_diff")

import gen_serve_fixtures  # noqa: E402
import test_privacy  # noqa: E402
from disconect import contract, queries, serve  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "serve"
STORE = FIXTURES / "synthetic.hbdb"
STORE_V1 = FIXTURES / "synthetic-v1.hbdb"
STORE_LIVE = FIXTURES / "synthetic-live.hbdb"
STORE_EMPTY = FIXTURES / "empty.hbdb"
RUST_DEBUG = serve_diff.default_rust_bin(release=False)


def test_tagged_trees_keep_python_equal_looking_types_apart():
    tag = serve_diff.tag
    assert tag(1) != tag(1.0) and tag(1) != tag(True) and tag(0) != tag(False) and tag(None) != tag(False)
    assert tag("1") != tag(1) and tag([1]) != tag(1) and tag({"a": 1}) != tag([["a", 1]])
    assert tag(0.1 + 0.2) != tag(0.3), "floats compare by repr"
    assert tag([1, 2]) != tag([2, 1]), "list order is significant"


def test_the_first_difference_is_a_path_and_never_a_value():
    one = serve_diff.tag({"id": 1, "result": {"a": [1, {"b": "secret-value"}], "n": 3}})
    two = serve_diff.tag({"id": 1, "result": {"a": [1, {"b": "other-value"}], "n": 3}})
    found = serve_diff.first_difference(one, two)
    assert found == ".result.a[1].b (value)" and "secret" not in found and "other" not in found
    assert serve_diff.first_difference(serve_diff.tag({"n": 3}), serve_diff.tag({"n": 3.0})) == ".n (type int vs float)"
    assert serve_diff.first_difference(serve_diff.tag({"a": 1}), serve_diff.tag({"b": 1})) == ". (keys)"
    assert serve_diff.first_difference(serve_diff.tag([1]), serve_diff.tag([1, 2])) == ". (length 1 vs 2)"
    assert serve_diff.first_difference(one, one) is None


def test_exactly_two_key_paths_are_allowed_to_differ():
    def info(core, db, schema=3):
        return json.dumps({"id": 1, "result": {"product": "P", "core": core, "schema": schema, "contract": 1,
                                               "db": db, "encrypted": False, "notice": "N"}})
    assert serve_diff.compare_lines([info("a", "/x")], [info("b", "/y")]) is None
    assert serve_diff.compare_lines([info("a", "/x")], [info("b", "/y", schema=4)]) is not None
    not_a_string = json.dumps({"id": 1, "result": {"product": "P", "core": 7, "schema": 3, "contract": 1,
                                                   "db": "/y", "encrypted": False, "notice": "N"}})
    assert serve_diff.compare_lines([info("a", "/x")], [not_a_string]) is not None, "core must still be a string"
    other = json.dumps({"id": 1, "result": {"core": "a"}})
    assert serve_diff.compare_lines([other], [json.dumps({"id": 1, "result": {"core": "b"}})]) is not None


def test_the_privacy_walk_names_kinds_and_paths_not_text():
    bad = json.dumps({"id": 1, "result": {"serial": "x", "note": "see /Users/someone", "m": "Garmin Connect",
                                          "db": "/Users/ok/in/db"}})
    problems = serve_diff.privacy_walk([bad], ("hunter2 is long",), "app.info")
    assert sorted(problems) == ["forbidden key at .result", "forbidden text at .result.note",
                                "manufacturer name at .result.m"]
    assert serve_diff.privacy_walk([json.dumps({"id": 1, "result": {"x": "hunter2 is long"}})], ("hunter2 is long",)) \
        == ["a passphrase appears in a line"]
    assert serve_diff.privacy_walk([json.dumps({"id": 1, "result": {"db": "/Users/x/y"}})], (), "app.info") == []
    assert serve_diff.privacy_walk([json.dumps({"id": 1, "result": {"db": "/Users/x/y"}})], (), "sync.status") \
        == ["forbidden text at .result.db"], "the db allowance is keyed by the method too"


def test_the_relay_list_path_is_allowed_only_at_sync_status_relay_list_and_masked_only_when_a_cores_own():
    line = {"id": 7, "result": {"relay_list": [{"id": "a", "kind": "folder", "label": "", "serve": False,
                                                "path": "/Users/x/Garmin@example.com/relay"}]}}
    walk = serve_diff.privacy_walk
    assert walk([json.dumps(line)], (), "sync.status") == []
    assert walk([json.dumps(line)], (), methods={7: "sync.status"}) == []
    assert sorted(set(walk([json.dumps(line)], (), "sync.run"))) == [
        "forbidden key at .result.relay_list[0]", "forbidden text at .result.relay_list[0].path", "manufacturer name at .result.relay_list[0].path"]
    assert walk([json.dumps({"id": 7, "result": {"other": [{"path": "p"}]}})], (), "sync.status") \
        == ["forbidden key at .result.other[0]"]
    # masked only when the path IS the core's own scratch relay; a fixed literal compares unmasked
    status = lambda path: {"id": 1, "result": {"chains": [], "relay_list": [{"id": "a", "path": path}]}}  # noqa: E731
    assert serve_diff.apply_allowances(status("/s/py"), "/s/py") == serve_diff.apply_allowances(status("/s/rs"), "/s/rs")
    assert serve_diff.apply_allowances(status("~/relay x"), "/s/py") != serve_diff.apply_allowances(status("relay"), "/s/rs")
    assert serve_diff.apply_allowances(status("/s/py/sub"), "/s/py")["result"]["relay_list"][0]["path"] == "/s/py/sub"


def test_wire_shape_checks_ascii_and_protocol_form():
    assert serve_diff.shape_problems([b'{"id":1,"result":{}}', b'{"event":"log"}']) == []
    assert len(serve_diff.shape_problems([b"STRAY", b'{"x":1}', "café".encode()])) == 3


def test_the_copied_privacy_constants_match_the_privacy_test():
    assert serve_diff.FORBIDDEN_KEYS == test_privacy.FORBIDDEN_KEYS
    assert serve_diff.FORBIDDEN_TEXT == test_privacy.FORBIDDEN_TEXT
    assert serve_diff.SERIAL == test_privacy.SERIAL


def test_the_script_covers_every_protocol_method_and_the_malformed_line_classes():
    entries = json.loads(serve_diff.SCRIPT.read_text())["entries"]
    methods = {e["send"]["method"] for e in entries if "send" in e}
    assert set(serve.METHODS) - serve_diff.PHONE_ONLY <= methods and serve_diff.DEFERRED <= methods
    assert not serve_diff.PHONE_ONLY & methods, "a phone-only method is never sent: on a phone build it would erase the store"
    assert serve_diff.DEFERRED == set(), "slice 3D ported the last deferred methods"
    names = " ".join(e["name"] for e in entries)
    for needle in ("not JSON", "JSON array", "id: missing", "params null", "unknown method", "id: beyond u64",
                   "NaN in params", "bare CR", "empty line"):
        assert needle in names, needle


def _gunzipped(path: pathlib.Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("ascii")


def test_the_chains_allowance_masks_only_the_self_writer_id_and_orders_the_rows():
    row = lambda device, own: {"chain": device, "bundles": 1, "records": 2, "last_seq": 1, "self": own}  # noqa: E731
    one = {"id": 1, "result": {"chains": [row("0000000000000001", True), row("aaaaaaaaaaaaaaaa", False)]}}
    two = {"id": 1, "result": {"chains": [row("aaaaaaaaaaaaaaaa", False), row("ffffffffffffffff", True)]}}
    assert serve_diff.apply_allowances(one) == serve_diff.apply_allowances(two)
    other = {"id": 1, "result": {"chains": [row("aaaaaaaaaaaaaaab", False), row("ffffffffffffffff", True)]}}
    assert serve_diff.apply_allowances(one) != serve_diff.apply_allowances(other), "another writer's id still compares"
    short = {"id": 1, "result": {"chains": [row("ff", True)]}}
    assert serve_diff.apply_allowances(short)["result"]["chains"][0]["chain"] == "ff", "a malformed id is not masked"
    walk = lambda line: serve_diff.privacy_walk([json.dumps(line)], ())  # noqa: E731
    assert walk(one) == [], "the chain key is not a forbidden key, so the privacy walk needs no exception"
    assert walk({"id": 1, "result": {"device_id": "x"}}) == ["forbidden key at .result"]


@pytest.mark.parametrize("store, oracle", [(STORE, "oracle-synthetic.jsonl.gz"),
                                           (STORE_V1, "oracle-synthetic-v1.jsonl.gz"),
                                           (STORE_EMPTY, "oracle-empty.jsonl.gz"),
                                           (STORE_LIVE, "oracle-synthetic-live.jsonl.gz")])
def test_the_committed_oracle_is_what_the_python_core_answers_today(tmp_path, store, oracle):
    out = tmp_path / "oracle.jsonl.gz"
    assert serve_diff.main(["--python-only", "--db", str(store), "--anchors-from", str(STORE),
                            "--oracle-out", str(out)]) == 0
    assert _gunzipped(out) == _gunzipped(FIXTURES / oracle), "regenerate: python tests/gen_serve_fixtures.py --oracle"


@pytest.mark.parametrize("store, ledger", [(STORE, "ledger-synthetic.json.gz"), (STORE_V1, "ledger-synthetic-v1.json.gz")])
def test_the_committed_ledger_answers_are_what_the_python_core_answers_today(tmp_path, store, ledger):
    out = tmp_path / "ledger.json.gz"
    anchors = json.loads(serve_diff.SCRIPT.read_text())["anchors"]
    gen_serve_fixtures.build_ledger(store, out, anchors)
    assert _gunzipped(out) == _gunzipped(FIXTURES / ledger), "regenerate: python tests/gen_serve_fixtures.py"


def test_the_script_anchor_days_are_the_ones_the_committed_stores_give():
    script = json.loads(serve_diff.SCRIPT.read_text())
    assert serve_diff.anchors_for(STORE) == serve_diff.anchors_for(STORE_V1) == script["anchors"]
    assert set(script["anchors"]) == {"$FIRST", "$MID", "$LAST", "$BEFORE"}
    assert script["anchors"]["$BEFORE"] < script["anchors"]["$FIRST"] <= script["anchors"]["$MID"] <= script["anchors"]["$LAST"]


def test_the_generated_script_entries_are_what_the_generator_writes_today(tmp_path, monkeypatch):
    script = json.loads(serve_diff.SCRIPT.read_text())
    before = serve_diff.SCRIPT.read_text()
    monkeypatch.setattr(gen_serve_fixtures, "SCRIPT", tmp_path / "script.json")
    (tmp_path / "script.json").write_text(before)
    gen_serve_fixtures.build_script(script["anchors"])
    assert (tmp_path / "script.json").read_text() == before, "regenerate: python tests/gen_serve_fixtures.py"
    assert len(script["entries"]) < 2200  # raised from 2000 on 2026-10-06 for data.live (the oracle replay stays ≈2 min a store)


def test_no_request_is_booked_and_the_once_booked_forms_are_plain_bad_params():
    entries = json.loads(serve_diff.SCRIPT.read_text())["entries"]
    assert not [e for e in entries if "booked" in e], "the last named allowance was retired on 2026-10-03"
    asked = {e["send"]["params"].get("last_day") for e in entries
             if "send" in e and e["send"]["method"] == "data.metric" and isinstance(e["send"].get("params"), dict)
             and isinstance(e["send"]["params"].get("last_day"), str)}
    assert {"20261003", "2026-W40-6"} <= asked, "the compact and week forms stay in the script as bad requests"
    for form in ("20261003", "2026-W40-6"):   # fromisoformat alone would take them: that was the allowance
        assert datetime.date.fromisoformat(form)
        with pytest.raises(ValueError, match="last_day must be YYYY-MM-DD"):
            queries.parse_day(form, "last_day")


def test_the_script_asks_for_every_contract_metric_in_every_scope():
    entries = json.loads(serve_diff.SCRIPT.read_text())["entries"]
    asked = {(params.get("metric"), params.get("scope")) for e in entries
             if "send" in e and e["send"]["method"] == "data.metric"
             for params in [e["send"].get("params")] if isinstance(params, dict)
             and isinstance(params.get("metric"), str) and isinstance(params.get("scope"), str)}
    # a session scope (live) is asked only for the metrics the contract declares in it, plus one
    # "absent" probe (steps) — the full cross-product would pass the script's entry ceiling
    wanted = {(item.metric, scope) for item in contract.METRICS for scope in contract.SOURCE_SCOPES
              if scope not in contract.SESSION_SCOPES or (item.metric, scope) in contract.SESSION_STREAMS_FOR}
    assert wanted <= asked
    numeric = {item.metric for item in contract.METRICS}
    for scope in contract.SESSION_SCOPES:
        assert ("steps", scope) in asked
        assert {(m, s) for m, s in asked if s == scope and m in numeric} - wanted == {("steps", scope)}


FAKE_CORE = textwrap.dedent('''\
    #!{python}
    """A stand-in for the Rust binary: the Python core with one answer changed."""
    import subprocess, sys
    child = subprocess.Popen([{serve!r}, *sys.argv[1:]], stdin=sys.stdin, stdout=subprocess.PIPE)
    for line in iter(child.stdout.readline, b""):
        sys.stdout.buffer.write(line.replace({old!r}, {new!r}))
        sys.stdout.buffer.flush()
    sys.exit(child.wait())
''')


def _fake_core(tmp_path, old: bytes, new: bytes):
    fake = tmp_path / "fake-core"
    fake.write_text(FAKE_CORE.format(python=sys.executable, serve=str(serve_diff.PY_SERVE), old=old, new=new))
    fake.chmod(0o755)
    return fake


def test_a_core_that_differs_in_one_type_is_caught(tmp_path, capsys):
    fake = _fake_core(tmp_path, b'"schema":3,', b'"schema":3.0,')
    assert serve_diff.main(["--db", str(STORE), "--rust-bin", str(fake), "--no-import-leg", "--no-sync-leg"]) == 1
    report = capsys.readouterr().out
    assert "RESULT: FAILED" in report and ".result.schema (type int vs float)" in report
    assert "differing: 0" not in report


@pytest.mark.parametrize("old, new, what", [
    (b'"done":2,"total":null', b'"done":9,"total":null', "an event whose done does not restart at 0"),
    (b'"note":"duplicate"', b'"note":"dup"', "an event note"),
    (b'"partial":true', b'"partial":false', "the response"),
])
def test_the_import_leg_catches_a_core_that_differs_in_an_event_or_the_response(tmp_path, capsys, old, new, what):
    fake = _fake_core(tmp_path, old, new)
    assert serve_diff.main(["--db", str(STORE_EMPTY), "--rust-bin", str(fake), "--anchors-from", str(STORE),
                            "--no-sync-leg"]) == 1, what
    report = capsys.readouterr().out
    assert "RESULT: FAILED" in report and "import leg: " in report and ", differing 0" not in report, what


@pytest.mark.parametrize("old, new, what", [
    (b'"phase":"pull","state":"start"', b'"phase":"pull","state":"begun"', "an event"),
    (b'"applied":1', b'"applied":2', "a result count"),
    (b'"code":"not_found"', b'"code":"not_there"', "an error code"),
])
def test_the_sync_leg_catches_a_core_that_differs_in_an_event_or_the_response(tmp_path, capsys, old, new, what):
    fake = _fake_core(tmp_path, old, new)
    assert serve_diff.main(["--db", str(STORE_EMPTY), "--rust-bin", str(fake), "--anchors-from", str(STORE),
                            "--no-import-leg"]) == 1, what
    report = capsys.readouterr().out
    assert "RESULT: FAILED" in report and "sync leg: " in report and "identical 129, differing 0" not in report, what


@pytest.mark.skipif(not RUST_DEBUG.exists(), reason="build disconect-core first (cargo build)")
@pytest.mark.parametrize("store, label, now", [
    (STORE, "synthetic", None),
    (STORE_V1, "synthetic-v1", None),
    (STORE_EMPTY, "synthetic-never-imported", None),
    (STORE_LIVE, "synthetic-live", None),
    (STORE, "synthetic-watch-ahead", gen_serve_fixtures.OTHER_NOWS[0]),
    (STORE, "synthetic-watch-behind", gen_serve_fixtures.OTHER_NOWS[1]),
    (STORE, "synthetic-years-later", gen_serve_fixtures.OTHER_NOWS[2]),
])
def test_the_gate_passes_on_the_committed_synthetic_stores(capsys, store, label, now):
    status = serve_diff.main(["--db", str(store), "--label", label, "--anchors-from", str(STORE),
                              *(["--now", now] if now else [])])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "differing: 0" in report and "RESULT: 0 differences" in report
    assert "store copy unchanged: python yes, rust yes" in report
    assert "not yet ported (Rust unknown_method): 0 " in report, "every method of the oracle is ported"
    assert "import leg: " in report and ", differing 0" in report and "import leg events: python " in report
    assert "post-import core_diff: 0 differing rows" in report and "import leg run_id equal: 8/8" in report
    assert "sync leg: 129 steps" in report and "identical 129, differing 0" in report
    assert "sync.run results: python 7, rust 7" in report and "post-sync core_diff: 0 differing rows" in report
    # BL-7 review blocker: a step that removes relay.json turns every later bad_params check into not_found on both
    # cores (identical, so the diff stays 0) — the code histogram pins the mix the leg must answer
    assert "sync leg Rust error codes: None 38, bad_params 66, busy 5, locked 3, not_folder 1, not_found 6, pair_failed 10" in report
    assert "site leg: 8 pair.join steps beside no store, identical 8, differing 0" in report      # 12-H (d)
    assert "prefix passed (unsupported_transport) python 4, rust 4, expected 4" in report
    assert "site leg stores created: python False, rust False" in report
    assert "booked (named allowance, Rust bad_params): 0" in report


INPROC = serve_diff.default_rust_bin(release=False, inproc=True)


def test_inproc_excludes_the_other_binary_switches(capsys):
    assert serve_diff.main(["--db", str(STORE), "--inproc", "--release"]) == 2
    assert serve_diff.main(["--db", str(STORE), "--inproc", "--rust-bin", "x"]) == 2
    assert "--inproc excludes" in capsys.readouterr().err
    assert INPROC.name == "inproc_serve" and "examples" in INPROC.parts


@pytest.mark.skipif(not INPROC.exists(), reason="build the app's example first (cargo build --example inproc_serve in src-tauri)")
@pytest.mark.parametrize("store, label", [
    (STORE, "synthetic"),
    (STORE_V1, "synthetic-v1"),
    (STORE_EMPTY, "synthetic-never-imported"),
])
def test_the_gate_passes_through_the_apps_in_process_core(capsys, store, label):
    """Bet 12a: the app's in-process thread (examples/inproc_serve.rs) is byte-for-byte the sidecar's protocol."""
    status = serve_diff.main(["--db", str(store), "--label", label, "--anchors-from", str(STORE), "--inproc"])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "binary: in-process" in report and "differing: 0" in report and "RESULT: 0 differences" in report
    assert "import leg: " in report and ", differing 0" in report
    assert "post-import core_diff: 0 differing rows" in report
    assert "sync leg: 129 steps" in report and "identical 129, differing 0" in report
    assert "sync leg Rust error codes: None 38, bad_params 66, busy 5, locked 3, not_folder 1, not_found 6, pair_failed 10" in report
