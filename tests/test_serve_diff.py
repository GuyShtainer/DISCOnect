"""The two-core differential harness (``tools/serve_diff.py``) tests itself, and the oracle it feeds.

* the tagged-tree comparison tells ``1``, ``1.0`` and ``True`` apart and names a JSON path, never a value;
* the copied privacy constants have not drifted from ``test_privacy``;
* a core that differs in one type is caught by a real run (a stand-in for the Rust binary);
* the committed oracle responses and ledger answers are what the Python core answers today;
* the script's anchor days are the ones its stores give, and the booked requests are exactly the named two;
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

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import serve_diff  # noqa: E402

import gen_serve_fixtures  # noqa: E402
import test_privacy  # noqa: E402
from disconect import contract, serve  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "serve"
STORE = FIXTURES / "synthetic.hbdb"
STORE_V1 = FIXTURES / "synthetic-v1.hbdb"
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
    problems = serve_diff.privacy_walk([bad], ("hunter2 is long",))
    assert sorted(problems) == ["forbidden key at .result", "forbidden text at .result.note",
                                "manufacturer name at .result.m"]
    assert serve_diff.privacy_walk([json.dumps({"id": 1, "result": {"x": "hunter2 is long"}})], ("hunter2 is long",)) \
        == ["a passphrase appears in a line"]
    assert serve_diff.privacy_walk([json.dumps({"id": 1, "result": {"db": "/Users/x/y"}})], ()) == []


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
    assert set(serve.METHODS) <= methods and serve_diff.DEFERRED <= methods
    assert serve_diff.DEFERRED == set(), "slice 3D ported the last deferred methods"
    names = " ".join(e["name"] for e in entries)
    for needle in ("not JSON", "JSON array", "id: missing", "params null", "unknown method", "id: beyond u64",
                   "NaN in params", "bare CR", "empty line"):
        assert needle in names, needle


def _gunzipped(path: pathlib.Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("ascii")


@pytest.mark.parametrize("store, oracle", [(STORE, "oracle-synthetic.jsonl.gz"),
                                           (STORE_V1, "oracle-synthetic-v1.jsonl.gz"),
                                           (STORE_EMPTY, "oracle-empty.jsonl.gz")])
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
    assert len(script["entries"]) < 2000


def test_only_the_two_named_last_day_forms_are_booked():
    entries = json.loads(serve_diff.SCRIPT.read_text())["entries"]
    booked = [e for e in entries if "booked" in e]
    assert {e["booked"] for e in booked} == {gen_serve_fixtures.BOOKED_FROMISOFORMAT}
    assert {e["send"]["params"]["last_day"] for e in booked} == {"20261003", "2026-W40-6"}
    for entry in booked:   # Python really does accept both: that is what is booked
        assert datetime.date.fromisoformat(entry["send"]["params"]["last_day"])


def test_the_script_asks_for_every_contract_metric_in_every_scope():
    entries = json.loads(serve_diff.SCRIPT.read_text())["entries"]
    asked = {(params.get("metric"), params.get("scope")) for e in entries
             if "send" in e and e["send"]["method"] == "data.metric"
             for params in [e["send"].get("params")] if isinstance(params, dict)
             and isinstance(params.get("metric"), str) and isinstance(params.get("scope"), str)}
    assert {(item.metric, scope) for item in contract.METRICS for scope in contract.SOURCE_SCOPES} <= asked


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
    assert serve_diff.main(["--db", str(STORE), "--rust-bin", str(fake), "--no-import-leg"]) == 1
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
    assert serve_diff.main(["--db", str(STORE_EMPTY), "--rust-bin", str(fake), "--anchors-from", str(STORE)]) == 1, what
    report = capsys.readouterr().out
    assert "RESULT: FAILED" in report and "import leg: " in report and ", differing 0" not in report, what


@pytest.mark.skipif(not RUST_DEBUG.exists(), reason="build projects/disconect-core first (cargo build)")
@pytest.mark.parametrize("store, label, now", [
    (STORE, "synthetic", None),
    (STORE_V1, "synthetic-v1", None),
    (STORE_EMPTY, "synthetic-never-imported", None),
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
    assert "booked (named allowance, Rust bad_params): 8 " in report


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
