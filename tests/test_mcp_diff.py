"""The MCP differential harness (``tools/mcp_diff.py``, not in this repository) and its stock-client leg test themselves, and the oracle.

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

import pydantic_core
import pytest

import monorepo

mcp_diff = monorepo.harness("mcp_diff")
mcp_stock_client = monorepo.harness("mcp_stock_client")
serve_diff = monorepo.harness("serve_diff")

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


@pytest.mark.parametrize("name", list(gen_mcp_fixtures.HOSTILE))
def test_python_against_the_committed_hostile_oracle_is_zero_differences_and_current(tmp_path, capsys, name):
    store, _ = gen_mcp_fixtures.STORES[gen_mcp_fixtures.HOSTILE_STORE]
    fresh = tmp_path / "oracle.jsonl.gz"
    script = gen_mcp_fixtures.hostile_script_path(name)
    if gen_mcp_fixtures.HOSTILE[name][1] == "lines":
        assert script.read_text(encoding="utf-8") == gen_mcp_fixtures.hostile_script_text(name), (
            "stale script: run python tests/gen_mcp_fixtures.py")
    status = mcp_diff.main(["--store", str(store), "--label", f"hostile-{name}", "--script", str(script),
                            "--oracle", str(gen_mcp_fixtures.hostile_oracle_path(name)), "--oracle-out", str(fresh)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "differing: 0" in report and "RESULT: 0 differences" in report
    assert _gunzipped(fresh) == _gunzipped(gen_mcp_fixtures.hostile_oracle_path(name)), (
        "regenerate: python tests/gen_mcp_fixtures.py --oracle")


def test_python_against_python_is_zero_differences(capsys):
    status = mcp_diff.main(["--store", str(PRIVACY_SEED), "--label", "privacy-seed",
                            "--right-cmd", str(mcp_diff.PY_MCP)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "identical: 457" in report and "differing: 0" in report and "right: store copy unchanged: yes" in report


@pytest.mark.parametrize("store", [PRIVACY_SEED, gen_mcp_fixtures.STORES["synthetic"][0],
                                   gen_mcp_fixtures.STORES["empty"][0]])
def test_the_stock_client_leg_passes_against_the_python_server(capsys, store):
    status = mcp_stock_client.main(["--store", str(store)])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "12 tool calls, 0 problems" in report


# ---- the comparison itself ----

def _success(structured: dict, text: str | None = None) -> str:
    text = mcp_diff.pydantic_text(structured) if text is None else text
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


def test_the_text_must_be_pydantics_indent_two_json_of_the_structured_content():
    """The Python SDK builds the text with ``pydantic_core.to_json(indent=2)``: raw UTF-8, pydantic's floats."""
    structured = {"s": "é😀", "n": [1, 2], "tiny": 1e-5, "small": 1.5e-7, "big": 1e16, "whole": 100.0}
    good = json.loads(_success(structured))["result"]
    assert mcp_diff.text_problem(good) is None
    text = good["content"][0]["text"]
    assert "é😀" in text and "\\u00e9" not in text, "raw UTF-8, not ASCII-escaped"
    assert '"tiny": 0.00001' in text and '"small": 1.5e-7' in text and '"big": 1e+16' in text
    for wrong in (json.dumps(structured, indent=2, ensure_ascii=False),     # json.dumps floats: 1e-05, 1.5e-07
                  json.dumps(structured, indent=2, ensure_ascii=True),
                  pydantic_core.to_json(structured, indent=4).decode(),
                  pydantic_core.to_json(structured).decode()):
        assert mcp_diff.text_problem(json.loads(_success(structured, wrong))["result"]), wrong
    assert mcp_diff.text_problem(json.loads(_error("Error executing tool x: y"))["result"]) is None


def test_the_same_json_in_other_bytes_is_a_difference():
    """Parsed, the two texts are equal; byte for byte they are not: key order, escapes, a float's spelling."""
    structured = {"b": 1, "a": 2, "x": 1e-05, "s": "é"}
    good = _success(structured)
    shuffled = {"a": 2, "b": 1, "x": 1e-05, "s": "é"}
    for other_text in (mcp_diff.pydantic_text(shuffled),                        # key order
                       json.dumps(structured, indent=2, ensure_ascii=True),     # escapes and 1e-05
                       mcp_diff.pydantic_text(structured).replace("0.00001", "1e-05")):
        other = _success(structured, other_text)
        verdict, path = mcp_diff.compare_lines(good, other)
        assert verdict == "differs" and path.endswith("text (same JSON, different bytes)"), (verdict, path)
        assert mcp_diff.compare_lines(other, good)[0] == "differs"


def test_the_pydantic_float_table_is_current():
    import gen_pydantic_floats

    assert gen_pydantic_floats.FIXTURE.read_text(encoding="utf-8") == gen_pydantic_floats.rendered(), (
        "stale: run `python tests/gen_pydantic_floats.py` and commit it")


def test_only_the_wording_of_a_rejected_argument_is_allowed_to_differ():
    pydantic = ("Error executing tool list_activities: 1 validation error for list_activitiesArguments\n"
                "limit\n  Input should be a valid integer [type=int_parsing]")
    ours = ("Error executing tool list_activities: 1 validation error for list_activitiesArguments\n"
            "limit\n  Input should be a valid integer")
    assert mcp_diff.compare_lines(_error(pydantic), _error(ours)) == ("allowed", None)
    assert mcp_diff.compare_lines(_error(pydantic), _error(pydantic)) == ("identical", None)
    other_prefix = ours.replace("list_activities", "get_contract")
    assert mcp_diff.compare_lines(_error(pydantic), _error(other_prefix))[0] == "differs"
    assert mcp_diff.compare_lines(_error(pydantic), _success({"a": 1}))[0] == "differs", "isError must match"
    assert mcp_diff.compare_lines(_error("Error executing tool x: date must be YYYY-MM-DD"),
                                  _error("Error executing tool x: date is bad"))[0] == "differs", \
        "any other error text is exact"


PYDANTIC_TWO = ("Error executing tool get_metric_series: 2 validation errors for get_metric_seriesArguments\n"
                "metrics\n  Input should be a valid list [type=list_type, input_value='x', input_type=str]\n"
                "    For further information visit https://errors.pydantic.dev/2.13/v/list_type\n"
                "days\n  Input should be a valid integer [type=int_parsing, input_value='y', input_type=str]\n"
                "    For further information visit https://errors.pydantic.dev/2.13/v/int_parsing")
OURS_TWO = ("Error executing tool get_metric_series: 2 validation errors for get_metric_seriesArguments\n"
            "metrics\n  Input should be a valid list\ndays\n  Input should be a valid integer")
OURS_ONE = ("Error executing tool get_metric_series: 1 validation error for get_metric_seriesArguments\n"
            "metrics\n  Input should be a valid list")


def test_the_validation_allowance_needs_a_validation_text_on_both_sides_naming_the_same_parameters():
    """Review 11e M1: a ToolError text on one side and validation wording on the other was passed as allowed."""
    tool_error = "Error executing tool get_sleep_detail: date must be YYYY-MM-DD"
    validation = ("Error executing tool get_sleep_detail: 1 validation error for get_sleep_detailArguments\n"
                  "date\n  Input should be a valid string [type=string_type, input_value=3, input_type=int]")
    assert mcp_diff.compare_lines(_error(tool_error), _error(validation))[0] == "differs"
    assert mcp_diff.compare_lines(_error(validation), _error(tool_error))[0] == "differs"
    assert mcp_diff.compare_lines(_error(tool_error), _error(tool_error)) == ("identical", None)
    # the same parameters, a different number of errors: still allowed
    assert mcp_diff.compare_lines(_error(PYDANTIC_TWO), _error(OURS_TWO)) == ("allowed", None)
    assert mcp_diff.compare_lines(_error(OURS_TWO), _error(PYDANTIC_TWO)) == ("allowed", None)
    one_error_of_two = OURS_ONE.replace("1 validation error for", "2 validation errors for")
    assert mcp_diff.compare_lines(_error(PYDANTIC_TWO), _error(one_error_of_two))[0] == "differs", \
        "a missing parameter name is a difference, even with the count equal"
    assert mcp_diff.compare_lines(_error(PYDANTIC_TWO), _error(OURS_ONE))[0] == "differs"
    other_tool = OURS_TWO.replace("get_metric_series", "get_period_facts")
    assert mcp_diff.compare_lines(_error(PYDANTIC_TWO), _error(other_tool))[0] == "differs"
    nested = PYDANTIC_TWO.replace("\nmetrics\n", "\nmetrics.0\n")
    assert mcp_diff.compare_lines(_error(nested), _error(OURS_TWO)) == ("allowed", None), "an item is its parameter"


def test_every_legitimate_validation_pair_of_the_script_stays_allowed():
    """The 59 rejected-argument cases of the script, against pydantic's own wording of each (Python vs Python
    with the text re-worded the Rust way): allowed, never differing."""
    records = mcp_diff.load_oracle(gen_mcp_fixtures.oracle_path("synthetic"))
    allowed = 0
    for record in records:
        for entry in record.entries:
            for line in entry["stdout"]:
                node = json.loads(line)
                if not mcp_diff.validation_prefix(node):
                    continue
                text = node["result"]["content"][0]["text"]
                names = mcp_diff.validation_parameters(text)
                tool = mcp_diff.validation_prefix(node).split()[3].rstrip(":")
                ours = (f"{mcp_diff.validation_prefix(node)}{len(names)} validation error"
                        f"{'' if len(names) == 1 else 's'} for {tool}Arguments\n"
                        + "\n".join(f"{name}\n  reason" for name in names))
                reworded = json.loads(line)
                reworded["result"]["content"][0]["text"] = ours
                assert mcp_diff.compare_lines(line, json.dumps(reworded)) == ("allowed", None), text
                allowed += 1
    assert allowed == 59


LOCKED_PY = ("Error executing tool list_activities: file is not a database. The store is encrypted or locked: run "
             "'disconect key cache' once in a terminal, then restart disconect-mcp.")
LOCKED_RS = "Error executing tool list_activities: " + mcp_diff.RUST_LOCKED_TEXT


def test_the_locked_midsession_allowance_is_one_exact_case_and_two_sided():
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _error(LOCKED_RS)) == ("allowed-locked", None)
    for same in (LOCKED_PY, LOCKED_RS):
        assert mcp_diff.compare_lines(_error(same), _error(same)) == ("identical", None)
    assert mcp_diff.compare_lines(_error(LOCKED_RS), _error(LOCKED_PY))[0] == "differs", "the sides are not symmetric"
    other_tool = LOCKED_RS.replace("list_activities", "get_data_health")
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _error(other_tool))[0] == "differs"
    python_with_another_text = LOCKED_PY.replace("restart disconect-mcp", "restart something")
    assert mcp_diff.compare_lines(_error(python_with_another_text), _error(LOCKED_RS))[0] == "differs"
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _error(LOCKED_RS + " Also this."))[0] == "differs"
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _error(LOCKED_PY.replace("list_activities", "x")))[0] == "differs"
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _success({"a": 1}))[0] == "differs"
    assert mcp_diff.compare_lines(_error(LOCKED_PY), _error("Error executing tool list_activities: not locked"))[
        0] == "differs", "any other Rust text is exact"


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
import pydantic_core
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
            result["content"][0]["text"] = ("Error executing tool list_activities: 1 validation error for "
                                            "list_activitiesArguments\\nlimit\\n  Input should be a valid integer")
    ''')
    assert status == 0, report
    assert "allowed (kb23-pydantic-validation-text, counted apart): 1" in report and "differing: 0" in report


@pytest.mark.parametrize("body, expect", [
    ('''
        result = node.get("result") or {}
        if "structuredContent" in result and "contract_version" in result["structuredContent"]:
            result["structuredContent"]["contract_version"] = 1
            result["content"][0]["text"] = pydantic_core.to_json(result["structuredContent"], indent=2).decode()
     ''', "an int where Python has a string"),
    ('''
        result = node.get("result") or {}
        if result.get("isError") and "YYYY" in result["content"][0]["text"]:
            result["content"][0]["text"] = "Error executing tool get_sleep_detail: date is bad"
     ''', "another error text"),
    ('''
        result = node.get("result") or {}
        if "structuredContent" in result and "activities" in result["structuredContent"]:
            result["content"][0]["text"] = pydantic_core.to_json(result["structuredContent"], indent=4).decode()
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
            result["content"][0]["text"] = pydantic_core.to_json(result["structuredContent"], indent=2).decode()
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
    paths = {name: gen_mcp_fixtures.oracle_path(name) for name in gen_mcp_fixtures.STORES}
    paths |= {f"hostile-{name}": gen_mcp_fixtures.hostile_oracle_path(name) for name in gen_mcp_fixtures.HOSTILE}
    for name, path in paths.items():
        records = mcp_diff.load_oracle(path)
        lines = [line for r in records for e in r.entries for line in e["stdout"]]
        assert mcp_diff.line_problems(lines, (gen_mcp_fixtures.ENCRYPTED_PASSPHRASE,)) == [], name
        assert mcp_diff.shape_problems(lines) == [], name
        assert mcp_diff.stderr_problems([line for r in records for line in r.stderr],
                                        (gen_mcp_fixtures.ENCRYPTED_PASSPHRASE,)) == [], name


def test_the_copied_privacy_constants_still_match_the_privacy_test():
    assert serve_diff.FORBIDDEN_KEYS == test_privacy.FORBIDDEN_KEYS
    assert serve_diff.FORBIDDEN_TEXT == test_privacy.FORBIDDEN_TEXT
