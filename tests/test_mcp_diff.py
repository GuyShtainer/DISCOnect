"""The MCP differential harness (``tools/mcp_diff.py``) and its stock-client leg test themselves, and the oracle.

* the committed oracle transcripts are what the Python server answers today, on every store (content compare),
  and the harness compares that run with the oracle at 0 differences, privacy walk clean;
* Python against Python is 0 differences;
* the stock-client leg (``ClientSession`` and ``Client(mode="auto")``) passes against the Python server;
* the comparison is strict: a type, a text or an indent that differs is caught, the one named allowance
  (``kb23-pydantic-validation-text``) is counted apart, and the privacy walk reads inside a result's ``text``;
* the encrypted store is served with a passphrase through ``--passphrase-file``.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import mcp_diff  # noqa: E402
import mcp_stock_client  # noqa: E402
import serve_diff  # noqa: E402

import gen_mcp_fixtures  # noqa: E402
import test_privacy  # noqa: E402

PRIVACY_SEED = gen_mcp_fixtures.PRIVACY_SEED


def _gunzipped(path: pathlib.Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("ascii")


@pytest.mark.parametrize("name", list(gen_mcp_fixtures.STORES))
def test_python_against_the_committed_oracle_is_zero_differences_and_the_oracle_is_current(tmp_path, capsys, name):
    store, keys = gen_mcp_fixtures.STORES[name]
    fresh = tmp_path / "oracle.jsonl.gz"
    status = mcp_diff.main(["--store", str(store), "--label", name, "--oracle", str(gen_mcp_fixtures.oracle_path(name)),
                            "--oracle-out", str(fresh), *(["--keys", str(keys)] if keys else [])])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "differing: 0" in report and "RESULT: 0 differences" in report
    assert "store copy unchanged: yes" in report
    assert _gunzipped(fresh) == _gunzipped(gen_mcp_fixtures.oracle_path(name)), (
        "regenerate: python tests/gen_mcp_fixtures.py --oracle")


def test_python_against_python_is_zero_differences(capsys):
    status = mcp_diff.main(["--store", str(PRIVACY_SEED), "--label", "privacy-seed",
                            "--right-cmd", str(mcp_diff.PY_MCP)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "identical: 432" in report and "differing: 0" in report and "right: store copy unchanged: yes" in report


@pytest.mark.parametrize("store", [PRIVACY_SEED, gen_mcp_fixtures.STORES["synthetic"][0],
                                   gen_mcp_fixtures.STORES["empty"][0]])
def test_the_stock_client_leg_passes_against_the_python_server(capsys, store):
    status = mcp_stock_client.main(["--store", str(store)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "12 tool calls, 0 problems" in report


# ---- the comparison itself ----

def _success(structured: dict, text: str | None = None) -> str:
    text = json.dumps(structured, indent=2, ensure_ascii=True) if text is None else text
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}],
                                                            "isError": False, "structuredContent": structured}})


def _error(text: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}],
                                                            "isError": True}})


def test_a_result_text_is_compared_as_parsed_json_and_types_stay_apart():
    assert mcp_diff.compare_lines(_success({"n": 1}), _success({"n": 1})) == ("identical", None)
    verdict, path = mcp_diff.compare_lines(_success({"n": 1}), _success({"n": 1.0}))
    assert verdict == "differs" and path.endswith(".n (type int vs float)")
    verdict, path = mcp_diff.compare_lines(_success({"n": 1}), _success({"n": True}))
    assert verdict == "differs" and "type int vs bool" in path
    verdict, path = mcp_diff.compare_lines(_success({"n": 0.1 + 0.2}), _success({"n": 0.3}))
    assert verdict == "differs" and path.endswith("(value)"), "floats compare by repr"
    assert "secret" not in mcp_diff.compare_lines(_success({"n": "secret"}), _success({"n": "other"}))[1]


def test_the_text_must_be_the_indent_two_ascii_dump_of_the_structured_content():
    good = json.loads(_success({"s": "é", "n": [1, 2]}))["result"]
    assert mcp_diff.text_problem(good) is None
    assert "\\u00e9" in good["content"][0]["text"], "ASCII-escaped"
    for wrong in (json.dumps({"s": "é", "n": [1, 2]}, indent=2, ensure_ascii=False),
                  json.dumps({"s": "é", "n": [1, 2]}, indent=4, ensure_ascii=True),
                  json.dumps({"s": "é", "n": [1, 2]}, ensure_ascii=True)):
        assert mcp_diff.text_problem(json.loads(_success({"s": "é", "n": [1, 2]}, wrong))["result"])
    assert mcp_diff.text_problem(json.loads(_error("Error executing tool x: y"))["result"]) is None


def test_only_the_wording_of_a_rejected_argument_is_allowed_to_differ():
    pydantic = ("Error executing tool list_activities: 1 validation error for list_activitiesArguments\n"
                "limit\n  Input should be a valid integer [type=int_parsing]")
    ours = "Error executing tool list_activities: limit must be an integer"
    assert mcp_diff.compare_lines(_error(pydantic), _error(ours)) == ("allowed", None)
    assert mcp_diff.compare_lines(_error(pydantic), _error(pydantic)) == ("identical", None)
    other_prefix = "Error executing tool get_contract: limit must be an integer"
    assert mcp_diff.compare_lines(_error(pydantic), _error(other_prefix))[0] == "differs"
    assert mcp_diff.compare_lines(_error(pydantic), _success({"a": 1}))[0] == "differs", "isError must match"
    assert mcp_diff.compare_lines(_error("Error executing tool x: date must be YYYY-MM-DD"),
                                  _error("Error executing tool x: date is bad"))[0] == "differs", \
        "any other error text is exact"


def test_the_privacy_walk_reads_inside_the_text_and_names_a_kind():
    leaky = _success({"note": "see /Users/someone", "m": "Garmin Connect", "serial": "x"})
    problems = mcp_diff.line_problems([leaky], ("hunter2 is long",))
    assert "forbidden key at .result.structuredContent" in problems
    assert any(problem.startswith("manufacturer name") for problem in problems)
    assert any(problem.startswith("forbidden text") for problem in problems)
    assert all("someone" not in problem for problem in problems)
    clean = _success({"n": 1})
    assert mcp_diff.line_problems([clean], ()) == []


def test_the_wire_shape_check_allows_responses_and_notifications_only():
    ok = ['{"jsonrpc":"2.0","id":1,"result":{}}', '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"x"}}',
          '{"jsonrpc":"2.0","method":"notifications/message","params":{}}']
    assert mcp_diff.shape_problems(ok) == []
    bad = ['STRAY', '{"id":1,"result":{}}', '{"jsonrpc":"2.0","id":1}', '{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
           '{"jsonrpc":"2.0"}', '[1]']
    assert len(mcp_diff.shape_problems(bad)) == 6


# ---- a stand-in for the Rust binary: the Python server with one answer changed (``rewrite`` edits the parsed line) ----

FAKE = """#!{python}
import json, subprocess, sys
child = subprocess.Popen([{server!r}, *sys.argv[1:]], stdin=sys.stdin, stdout=subprocess.PIPE)


def dump(node):
    return json.dumps(node, separators=(",", ":"), ensure_ascii=False).encode() + b"\\n"


def rewrite(node):
{body}
    return node


for raw in iter(child.stdout.readline, b""):
    try:
        node = json.loads(raw)
    except ValueError:
        sys.stdout.buffer.write(raw)
    else:
        sys.stdout.buffer.write(dump(rewrite(node)))
    sys.stdout.buffer.flush()
sys.exit(child.wait())
"""

MINI_SCRIPT = {"sessions": [{"name": "mini", "entries": gen_mcp_fixtures.handshake() + [
    {"name": "contract", "send": gen_mcp_fixtures.call(2, "get_contract", {})},
    {"name": "limit 3", "send": gen_mcp_fixtures.call(3, "list_activities", {"limit": 3})},
    {"name": "limit abc", "send": gen_mcp_fixtures.call(4, "list_activities", {"limit": "abc"})},
    {"name": "bad date", "send": gen_mcp_fixtures.call(5, "get_sleep_detail", {"date": "20250630"})}]}]}


def _fake(tmp_path, body: str):
    fake = tmp_path / "fake-mcp"
    fake.write_text(FAKE.format(python=sys.executable, server=str(mcp_diff.PY_MCP),
                                body=textwrap.indent(textwrap.dedent(body), "    ")))
    fake.chmod(0o755)
    script = tmp_path / "mini.json"
    script.write_text(json.dumps(MINI_SCRIPT))
    return fake, script


def _run(tmp_path, capsys, body: str):
    fake, script = _fake(tmp_path, body)
    status = mcp_diff.main(["--store", str(PRIVACY_SEED), "--script", str(script), "--right-cmd", str(fake)])
    return status, capsys.readouterr().out


def test_a_stand_in_that_only_words_a_rejection_differently_is_allowed_and_counted_apart(tmp_path, capsys):
    status, report = _run(tmp_path, capsys, '''
        result = node.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError") and "validation error" in text:
            result["content"][0]["text"] = "Error executing tool list_activities: limit must be an integer"
    ''')
    assert status == 0, report
    assert "allowed (kb23-pydantic-validation-text, counted apart): 1" in report and "differing: 0" in report


@pytest.mark.parametrize("body, expect", [
    ('''
        result = node.get("result") or {}
        if "structuredContent" in result and "contract_version" in result["structuredContent"]:
            result["structuredContent"]["contract_version"] = 1
            result["content"][0]["text"] = json.dumps(result["structuredContent"], indent=2, ensure_ascii=True)
     ''', "an int where Python has a string"),
    ('''
        result = node.get("result") or {}
        if result.get("isError") and "YYYY" in result["content"][0]["text"]:
            result["content"][0]["text"] = "Error executing tool get_sleep_detail: date is bad"
     ''', "another error text"),
    ('''
        result = node.get("result") or {}
        if "structuredContent" in result and "activities" in result["structuredContent"]:
            result["content"][0]["text"] = json.dumps(result["structuredContent"], indent=4, ensure_ascii=True)
     ''', "an indent of four"),
    ('''
        result = node.get("result") or {}
        if result.get("isError") and "validation error" in result["content"][0]["text"]:
            result["isError"] = False
     ''', "a rejection that is not an error"),
    ('''
        if node.get("result", {}).get("serverInfo"):
            node["result"]["serverInfo"]["version"] = "0.1.0.dev0"
     ''', "the server version"),
    ('''
        result = node.get("result") or {}
        if "structuredContent" in result and "contract_version" in result["structuredContent"]:
            result["structuredContent"]["time"] += " Garmin"
            result["content"][0]["text"] = json.dumps(result["structuredContent"], indent=2, ensure_ascii=True)
     ''', "the manufacturer's name"),
], ids=["type", "error-text", "indent", "is-error", "version", "vendor"])
def test_a_stand_in_that_differs_is_caught(tmp_path, capsys, body, expect):
    status, report = _run(tmp_path, capsys, body)
    assert status == 1, (expect, report)
    assert "RESULT: FAILED" in report, (expect, report)


def test_a_server_that_exits_with_another_code_is_caught(tmp_path, capsys):
    fake, script = _fake(tmp_path, "pass")
    fake.write_text(fake.read_text().replace("sys.exit(child.wait())", "child.wait()\nsys.exit(3)"))
    status = mcp_diff.main(["--store", str(PRIVACY_SEED), "--script", str(script), "--right-cmd", str(fake)])
    report = capsys.readouterr().out
    assert status == 1 and "exit code 0 vs 3" in report


def test_the_encrypted_store_is_served_with_a_passphrase_file(tmp_path, capsys):
    store, keys = gen_mcp_fixtures.STORES["encrypted-locked"]
    passphrase = tmp_path / "pass.txt"
    passphrase.write_text(gen_mcp_fixtures.ENCRYPTED_PASSPHRASE + "\n")
    script = tmp_path / "mini.json"
    script.write_text(json.dumps({"sessions": [{"name": "unlocked", "entries": gen_mcp_fixtures.handshake() + [
        {"name": "contract", "send": gen_mcp_fixtures.call(2, "get_contract", {})},
        {"name": "health", "send": gen_mcp_fixtures.call(3, "get_data_health", {"window_days": 3})}]}]}))
    status = mcp_diff.main(["--store", str(store), "--keys", str(keys), "--passphrase-file", str(passphrase),
                            "--script", str(script), "--right-cmd", str(mcp_diff.PY_MCP)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "identical: 4" in report and "left: 3 stdout lines" in report, "unlocked, the server answers"


def test_the_locked_encrypted_store_exits_with_nine_and_no_stdout():
    records = mcp_diff.load_oracle(gen_mcp_fixtures.oracle_path("encrypted-locked"))
    assert records and all(r.exit_code == 9 and all(not e["stdout"] for e in r.entries) for r in records)
    assert all(len(r.stderr) == 1 and "key cache" in r.stderr[0] for r in records)
    assert not any(serve_diff.SERIAL in line or "/Users/" in line for r in records for line in r.stderr)


def test_the_committed_transcripts_carry_no_identifier_the_privacy_test_forbids():
    for name in gen_mcp_fixtures.STORES:
        records = mcp_diff.load_oracle(gen_mcp_fixtures.oracle_path(name))
        lines = [line for r in records for e in r.entries for line in e["stdout"]]
        assert mcp_diff.line_problems(lines, (gen_mcp_fixtures.ENCRYPTED_PASSPHRASE,)) == [], name
        assert mcp_diff.shape_problems(lines) == [], name
        assert mcp_diff.stderr_problems([line for r in records for line in r.stderr],
                                        (gen_mcp_fixtures.ENCRYPTED_PASSPHRASE,)) == [], name


def test_the_copied_privacy_constants_still_match_the_privacy_test():
    assert serve_diff.FORBIDDEN_KEYS == test_privacy.FORBIDDEN_KEYS
    assert serve_diff.FORBIDDEN_TEXT == test_privacy.FORBIDDEN_TEXT
