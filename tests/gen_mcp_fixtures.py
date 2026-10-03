#!/usr/bin/env python3
"""Build what the Rust MCP is held to: ``disconect-core/mcp.json``, the request script and the oracle transcripts.

    python tests/gen_mcp_fixtures.py             # rewrite mcp.json, tests/fixtures/mcp/script.json, privacy-seed.hbdb
    python tests/gen_mcp_fixtures.py --oracle    # also rewrite oracle-<store>.jsonl.gz (the Python server's lines)
    python tests/gen_mcp_fixtures.py --encrypted # (re)build the encrypted store of the locked-path transcript

``mcp.json`` is the language-neutral description of the server as it is served (pitch 11e): the tools exactly as
``tools/list`` sends them, ``instructions``, ``serverInfo``, ``capabilities``, the error templates, the protocol
version rule, the answers to the methods without parameters and the **coercion table**: for each parameter type,
every input tried and whether the Python server accepted it, rejected it, answered with a protocol error or sent
nothing, and the value pydantic coerced an accepted input to. All of it is measured by running the Python
server, never typed in. The pydantic error *text* of a rejection is not stored (named allowance
``kb23-pydantic-validation-text``).

The script is the same for every store; the transcripts are what the Python server answers to it per store
(synthetic, schema-v1, never-imported, the privacy-test seed, a store nobody imported yet, and an encrypted one on
its locked path). The encrypted store is built once (its salt is random) and committed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "tools"))

import mcp_diff  # noqa: E402
import serve_diff  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "mcp"
SERVE_FIXTURES = HERE / "fixtures" / "serve"
MCP_JSON = pathlib.Path(__file__).resolve().parents[2] / "disconect-core" / "mcp.json"
SCRIPT = FIXTURES / "script.json"
PRIVACY_SEED = FIXTURES / "privacy-seed.hbdb"
ENCRYPTED = FIXTURES / "encrypted.hbdb"
ENCRYPTED_KEYS = FIXTURES / "encrypted.keys.json"
ENCRYPTED_PASSPHRASE = "a synthetic passphrase for the locked fixture"
PINNED_NOW = mcp_diff.NOW
ABSENT = FIXTURES / "never-existed.hbdb"   # never created: the server is pointed at a file that is not there

#: transcript name -> (store, key file). ``ABSENT`` has no file on purpose.
STORES: dict[str, tuple[pathlib.Path, pathlib.Path | None]] = {
    "synthetic": (SERVE_FIXTURES / "synthetic.hbdb", None),
    "synthetic-v1": (SERVE_FIXTURES / "synthetic-v1.hbdb", None),
    "empty": (SERVE_FIXTURES / "empty.hbdb", None),
    "privacy-seed": (PRIVACY_SEED, None),
    "absent": (ABSENT, None),
    "encrypted-locked": (ENCRYPTED, ENCRYPTED_KEYS),
}
PROTOCOL_CANDIDATES = ("2024-10-07", "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28",
                       "1999-01-01")
KNOWN_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
NEWEST = KNOWN_VERSIONS[-1]
PROBE_VERSION = NEWEST


# ---- the stores ----

def build_privacy_seed(target: pathlib.Path) -> None:
    """The ``test_privacy`` seed store as one plain file; the clock is pinned so it is the same bytes every time."""
    os.environ["DISCONECT_NOW"] = PINNED_NOW
    from disconect import storage
    from test_privacy import _seed

    with tempfile.TemporaryDirectory() as folder:
        db_path = pathlib.Path(folder) / "privacy-seed.hbdb"
        _seed(db_path)
        with storage.open_for_write(db_path, "fixture") as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for leftover in db_path.parent.glob("privacy-seed.hbdb-*"):
            assert leftover.stat().st_size == 0, f"{leftover.name} still holds data"
        shutil.copyfile(db_path, target)


def build_encrypted(target: pathlib.Path, keys_target: pathlib.Path) -> None:
    """A tiny encrypted store and its key file. Not reproducible (random salt): built on request, committed."""
    from disconect import cli
    from disconect.storage import keys

    with tempfile.TemporaryDirectory() as folder:
        db_path = pathlib.Path(folder) / "encrypted.hbdb"
        shutil.copyfile(SERVE_FIXTURES / "empty.hbdb", db_path)
        keys.set_kdf_params(None)
        os.environ[keys.PASSPHRASE_ENV] = ENCRYPTED_PASSPHRASE
        try:
            assert cli.main(["--db", str(db_path), "key", "init"]) == 0
        finally:
            os.environ.pop(keys.PASSPHRASE_ENV, None)
        shutil.copyfile(db_path, target)
        shutil.copyfile(keys.key_path_for(db_path), keys_target)


# ---- request builders ----

def request(request_id, method: str, params=None) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params=None) -> dict:
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialize(request_id, version: str) -> dict:
    return request(request_id, "initialize", {"protocolVersion": version, "capabilities": {},
                                              "clientInfo": {"name": "mcp-diff", "version": "0"}})


def call(request_id, name: str, arguments) -> dict:
    return request(request_id, "tools/call", {"name": name, "arguments": arguments})


def raw_call(request_id, name: str, arguments_text: str | None) -> str:
    """A ``tools/call`` line whose ``arguments`` is the given JSON *text* (None: the key is left out)."""
    arguments = "" if arguments_text is None else f',"arguments":{arguments_text}'
    return f'{{"jsonrpc":"2.0","id":{request_id},"method":"tools/call","params":{{"name":"{name}"{arguments}}}}}'


def handshake(version: str = NEWEST) -> list[dict]:
    return [{"name": f"initialize {version}", "send": initialize(1, version)},
            {"name": "notifications/initialized", "send": notification("notifications/initialized")}]


# ---- the coercion table: inputs per parameter type ----

#: (type label, tool, parameter, other required arguments, raw JSON inputs). Raw texts, so ``NaN`` and big
#: numbers reach the server exactly as written.
PARAMETER_CASES = (
    ("int", "list_activities", "limit", {},
     ["3", '"3"', "2.0", "2.5", "true", "false", "null", '"abc"', '"1e2"', '" 3 "', '"3.0"', '""', "1e23", "-1e23",
      "99999999999999999999999", '"99999999999999999999999"', "[]", "{}", '"+3"', '"-3"', '"0x10"', '"1_000"',
      '"\\u0663"', '"\\uff13"', "NaN", "Infinity", "-0", "0", "-1", "1e2", "3e0", "0.0", "-0.0", "200", "201",
      "9007199254740993", "-9007199254740993", '"  "', '"3 "', '"3\\n"', '"٣"']),
    ("str|None", "get_sleep_detail", "date", {},
     ['"2025-06-30"', '""', "null", "3", "true", "2.5", "[]", "{}", '"x"', '"\\u00e9"', '"[\\"a\\"]"', '"null"',
      '"3"', '"\\ud83d\\ude00"']),
    ("bool", "get_period_facts", "include_points", {},
     ["true", "false", "1", "0", "2", "-1", '"true"', '"false"', '"True"', '"TRUE"', '"yes"', '"no"', '"on"', '"off"',
      '"1"', '"0"', '"t"', '"f"', '"y"', '"n"', '" true"', '"null"', "null", "1.0", "0.0", "0.5", '"abc"', '""',
      "[]", "{}", '"2"', '"1.0"']),
    ("list[str]", "get_metric_series", "metrics", {},
     ['["steps"]', "[]", '"steps"', '"[\\"steps\\"]"', '["steps",1]', "[null]", "null", "{}", '["steps","steps"]',
      "[1]", '"[]"', '[["steps"]]', '"steps,heart_rate"', '["heart_rate","stress"]', '[""]', '[true]', "3", "true",
      '"null"', '"{}"']),
    ("list[str]|None", "get_period_facts", "metrics", {},
     ['["steps"]', "[]", "null", '"steps"', '"[\\"steps\\"]"', "[1]", "{}", '["steps",null]', '"null"', '"[]"', '[""]']),
)
#: Whole-``arguments`` cases: (tool, the JSON text of ``arguments`` or None for the key left out).
ARGUMENT_CASES = (
    ("list_activities", None), ("list_activities", "null"), ("list_activities", "{}"), ("list_activities", "[]"),
    ("list_activities", '"x"'), ("list_activities", "3"), ("list_activities", "true"),
    ("list_activities", '{"limit":2,"extra":1}'), ("list_activities", '{"limit":1,"limit":2}'),
    ("list_activities", '{"Limit":2}'), ("list_activities", '{"limit":2,"limit":"x"}'),
    ("get_metric_series", None), ("get_metric_series", "{}"), ("get_metric_series", '{"days":3}'),
    ("get_metric_series", '{"metrics":["steps"]}'), ("get_metric_series", '{"metrics":["steps"],"end_date":null}'),
    ("get_metric_series", '{"metrics":["steps"],"source_scope":null,"days":null}'),
    ("get_contract", None), ("get_contract", "{}"), ("get_contract", '{"x":1}'), ("get_contract", "null"),
    ("get_data_health", None), ("get_data_health", '{"window_days":null}'),
    ("get_period_facts", '{"window_days":null}'), ("get_period_facts", '{"include_points":null}'),
    ("get_sleep_detail", None), ("get_sleep_detail", '{"date":null}'),
)
DEFAULT_ARGUMENTS = {"get_metric_series": {"metrics": ["steps"]}}


def tagged(value):
    """A coerced Python value in a form JSON keeps exact: ints as decimal text, floats by ``repr``."""
    if value is None:
        return {"null": True}
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, str):
        return {"str": value}
    if isinstance(value, list):
        return {"list": [tagged(item) for item in value]}
    if isinstance(value, dict):
        return {"dict": {key: tagged(item) for key, item in value.items()}}
    raise TypeError(type(value).__name__)


def _validated(tool: str, arguments: dict):
    """What pydantic makes of ``arguments`` for ``tool`` (the one-level kwargs the function receives), or None."""
    from pydantic import ValidationError
    from disconect import mcp_server

    spec = mcp_server.server._tool_manager.get_tool(tool)
    try:
        return spec.fn_metadata.validate_arguments(arguments)
    except ValidationError:
        return None


def _outcome(lines: list[str]) -> tuple[str, int | None]:
    """How the server answered one ``tools/call``: accepted, rejected (validation), protocol_error, dropped."""
    if not lines:
        return "dropped", None
    node = json.loads(lines[0])
    if "error" in node:
        return "protocol_error", node["error"]["code"]
    if mcp_diff.validation_prefix(node):
        return "rejected", None
    return "accepted", None


def coercion_entries() -> tuple[list[dict], list[dict]]:
    """The script entries that probe the table, and the case descriptions in the same order."""
    entries, cases, request_id = [], [], 100
    for label, tool, param, _, inputs in PARAMETER_CASES:
        for text in inputs:
            request_id += 1
            others = "".join(f',"{key}":{json.dumps(value)}' for key, value in DEFAULT_ARGUMENTS.get(tool, {}).items()
                             if key != param)
            arguments = f'{{"{param}":{text}{others}}}'
            entries.append({"name": f"coerce {label} {tool}.{param}={text}", "expect": request_id,
                            "raw": raw_call(request_id, tool, arguments)})
            cases.append({"type": label, "tool": tool, "param": param, "input": text, "arguments": arguments})
    for tool, arguments in ARGUMENT_CASES:
        request_id += 1
        entries.append({"name": f"coerce arguments {tool} {arguments}", "expect": request_id,
                        "raw": raw_call(request_id, tool, arguments)})
        cases.append({"type": "arguments", "tool": tool, "param": None,
                      "input": "<absent>" if arguments is None else arguments, "arguments": arguments})
    return entries, cases


def _case_arguments(case: dict) -> dict | None:
    """The ``arguments`` dict the server hands to validation, as the SDK builds it (``params.arguments or {}``);
    None when the text is not a JSON object or null (the request itself is then a protocol error)."""
    if case["arguments"] is None:
        return {}
    try:
        value = json.loads(case["arguments"])
    except ValueError:
        return None
    if value is None:
        return {}
    return value if isinstance(value, dict) else None


# ---- driving the Python server ----

@contextlib.contextmanager
def _scratch():
    with tempfile.TemporaryDirectory(prefix="mcp-gen-") as folder:
        yield pathlib.Path(folder)


def wire(sessions: list[dict], store: pathlib.Path | None = None) -> dict[str, list[list[dict]]]:
    """Play sessions into the Python server on a scratch copy of ``store`` (default: the empty store):
    session name -> per entry, the parsed stdout lines."""
    store = store or STORES["empty"][0]
    with _scratch() as scratch:
        run = mcp_diff.run_script([str(mcp_diff.PY_MCP)], "gen", {"sessions": sessions}, store, None, scratch,
                                  PINNED_NOW, mcp_diff.TZ, None)
    return {record.name: [[json.loads(line) for line in entry["stdout"]] for entry in record.entries]
            for record in run.records}


def _strip(node: dict) -> dict:
    return {key: value for key, value in node.items() if key not in ("jsonrpc", "id")}


def snapshot() -> dict:
    """Everything ``mcp.json`` holds, measured from the running Python server."""
    from disconect import identity, mcp_server

    coercion_script, cases = coercion_entries()
    sessions = [{"name": f"version {v}", "entries": [{"name": "initialize", "send": initialize(1, v)}]}
                for v in PROTOCOL_CANDIDATES]
    sessions.append({"name": "listing", "entries": handshake() + [
        {"name": "tools/list", "send": request(2, "tools/list")}]})
    sessions.append({"name": "methods", "entries": handshake() + [
        {"name": method, "send": request(index + 2, method, params)}
        for index, (method, params) in enumerate(METHOD_PROBES)]})
    sessions.append({"name": "pre-initialize", "entries": [
        {"name": method, "send": request(index + 1, method, params)}
        for index, (method, params) in enumerate(PRE_INITIALIZE_PROBES)]})
    sessions.append({"name": "coercion", "entries": handshake() + coercion_script})
    measured = wire(sessions)

    initialize_result = measured[f"version {PROBE_VERSION}"][0][0]["result"]
    accepted = [v for v in PROTOCOL_CANDIDATES
                if measured[f"version {v}"][0][0]["result"]["protocolVersion"] == v]
    default = measured["version 1999-01-01"][0][0]["result"]["protocolVersion"]
    tools = measured["listing"][2][0]["result"]["tools"]

    table: dict[str, dict] = {}
    for case, answer in zip(cases, measured["coercion"][2:]):
        outcome, code = _outcome([json.dumps(line) for line in answer])
        item: dict = {"input": case["input"], "outcome": outcome}
        if case["type"] == "arguments":
            item = {"tool": case["tool"], **item}
        if code is not None:
            item["code"] = code
        arguments = _case_arguments(case)
        validated = None if arguments is None else _validated(case["tool"], arguments)
        if outcome == "accepted":
            assert validated is not None, f"the server accepted {case} but pydantic rejects it in process"
            item["coerced"] = {key: tagged(value) for key, value in validated.items()
                               if case["type"] == "arguments" or key == case["param"]}
        elif outcome == "rejected":
            assert validated is None, f"the server rejected {case} but pydantic accepts it in process"
        group = table.setdefault(case["type"], {"tool": case["tool"], "param": case["param"], "cases": []})
        group["cases"].append(item)

    return {
        "notes": ["Measured from the Python server by tests/gen_mcp_fixtures.py; never edited by hand.",
                  "protocol.accepted: the versions an initialize request is answered with as itself; any other "
                  "version (older, newer, unknown) is answered with protocol.default.",
                  "coercions: outcome is accepted (coerced holds the value the tool function receives, ints as "
                  "decimal text, floats by repr), rejected (isError, kb23-pydantic-validation-text: only the "
                  "'Error executing tool <name>: ' prefix is held), protocol_error (a JSON-RPC error, code given) "
                  "or dropped (no line at all)."],
        "protocol": {"accepted": accepted, "default": default},
        "serverInfo": initialize_result["serverInfo"],
        "capabilities": initialize_result["capabilities"],
        "instructions": initialize_result["instructions"],
        "tools": tools,
        "errors": {
            "tool_error": "Error executing tool {name}: {message}",
            "crash": "Error executing tool {name}",
            "unknown_tool": "Unknown tool: {name}",
            "templates": mcp_server.ERROR_TEMPLATES,
            "storage": _storage_texts(),
            "bad_day": {"end_date": "end_date must be YYYY-MM-DD", "date": "date must be YYYY-MM-DD"},
        },
        "methods": {name: _strip(measured["methods"][index + 2][0]) for index, (name, _) in enumerate(METHOD_PROBES)},
        "pre_initialize": {name: _strip(measured["pre-initialize"][index][0])
                           for index, (name, _) in enumerate(PRE_INITIALIZE_PROBES)},
        "coercions": table,
        "identity": {"name": identity.MCP_SERVER_NAME, "version": identity.VERSION},
    }


#: Methods asked after the handshake, with the parameters used; ``tools/list`` with a cursor is in the script.
METHOD_PROBES = (
    ("ping", None), ("resources/list", None), ("resources/templates/list", None), ("prompts/list", None),
    ("logging/setLevel", {"level": "debug"}), ("completion/complete", {}), ("server/discover", None),
    ("no/such/method", None), ("resources/read", {"uri": "file:///x"}), ("prompts/get", {"name": "x"}),
    ("resources/subscribe", {"uri": "file:///x"}),
)
PRE_INITIALIZE_PROBES = (
    ("ping", None), ("tools/list", None), ("tools/call", {"name": "get_contract", "arguments": {}}),
    ("resources/list", None), ("initialize", {}),
)


def _storage_texts() -> dict[str, str]:
    """The words of the storage exceptions the tools wrap (the templates' ``{exc}``), measured, not typed."""
    from disconect import storage

    with _scratch() as folder:
        try:
            storage.open_read_only(folder / "missing.db", allow_prompt=False)
        except storage.NotConfigured as exc:
            not_configured = str(exc)
    return {"not_configured": not_configured}


def rendered() -> str:
    """The file's exact text: two-space indent, UTF-8, trailing newline."""
    return json.dumps(snapshot(), indent=2, ensure_ascii=False) + "\n"


# ---- the script ----

def _tool_default_calls() -> list[dict]:
    calls = [("get_data_health", {}), ("get_metric_series", {"metrics": ["steps"]}), ("get_sleep_detail", {}),
             ("list_activities", {}), ("get_period_facts", {}), ("get_contract", {})]
    return [{"name": f"default {name}", "send": call(index + 2, name, arguments)}
            for index, (name, arguments) in enumerate(calls)]


def build_script() -> dict:
    from disconect import contract

    sessions: list[dict] = []
    for version in KNOWN_VERSIONS + ("2026-07-28", "1999-01-01"):
        sessions.append({"name": f"version {version}", "entries": handshake(version) + [
            {"name": "ping", "send": request(2, "ping")},
            {"name": "tools/list", "send": request(3, "tools/list")},
            {"name": "list_activities", "send": call(4, "list_activities", {"limit": 1})},
            {"name": "empty metrics", "send": call(5, "get_metric_series", {"metrics": []})}]})
    sessions.append({"name": "no initialized notification", "entries": [
        {"name": "initialize", "send": initialize(1, NEWEST)},
        {"name": "tools/list", "send": request(2, "tools/list")},
        {"name": "get_contract", "send": call(3, "get_contract", {})}]})
    sessions.append({"name": "pre-initialize", "entries": [
        {"name": "ping", "send": request(1, "ping")},
        {"name": "tools/list", "send": request(2, "tools/list")},
        {"name": "tools/call", "send": call(3, "get_contract", {})},
        {"name": "initialize with empty params", "send": request(4, "initialize", {})},
        {"name": "initialize without params", "send": request(5, "initialize")},
        {"name": "tools/list after the failed initializes", "send": request(6, "tools/list")},
        {"name": "initialize", "send": initialize(7, NEWEST)},
        {"name": "notifications/initialized", "send": notification("notifications/initialized")},
        {"name": "tools/list", "send": request(8, "tools/list")},
        {"name": "initialize again", "send": initialize(9, "2025-06-18")}]})
    sessions.append({"name": "methods", "entries": handshake() + [
        {"name": f"{method} {json.dumps(params)}" if params is not None else method,
         "send": request(index + 2, method, params)} for index, (method, params) in enumerate(METHOD_PROBES)] + [
        {"name": "tools/list with a cursor", "send": request(30, "tools/list", {"cursor": "x"})},
        {"name": "tools/list with a null cursor", "send": request(31, "tools/list", {"cursor": None})},
        {"name": "tools/list with extra params", "send": request(32, "tools/list", {"x": 1})},
        {"name": "ping with params", "send": request(33, "ping", {"x": 1})},
        {"name": "initialize with a missing protocolVersion", "send": request(34, "initialize", {
            "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}})},
        {"name": "initialize with a number as protocolVersion", "send": request(35, "initialize", {
            "protocolVersion": 3, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}})},
        {"name": "initialize without clientInfo", "send": request(36, "initialize", {
            "protocolVersion": NEWEST, "capabilities": {}})},
        {"name": "notifications/cancelled", "send": notification("notifications/cancelled", {"requestId": 99})},
        {"name": "notifications/progress", "send": notification("notifications/progress", {
            "progressToken": "t", "progress": 1})},
        {"name": "notifications/roots/list_changed", "send": notification("notifications/roots/list_changed")},
        {"name": "unknown notification", "send": notification("notifications/unknown")},
        {"name": "unknown notification with an id", "send": request(37, "notifications/unknown")},
        {"name": "ping after the notifications", "send": request(38, "ping")}]})
    sessions.append({"name": "malformed lines", "entries": handshake() + [
        {"name": "not JSON", "raw": "not json at all"},
        {"name": "truncated JSON", "raw": '{"jsonrpc":"2.0","id":2,"method":"ping"'},
        {"name": "empty line", "raw": ""},
        {"name": "blank line", "raw": "   "},
        {"name": "JSON number", "raw": "3"},
        {"name": "JSON string", "raw": '"ping"'},
        {"name": "JSON null", "raw": "null"},
        {"name": "empty object", "raw": "{}"},
        {"name": "empty array", "raw": "[]"},
        {"name": "batch of one", "raw": '[{"jsonrpc":"2.0","id":3,"method":"ping"}]'},
        {"name": "batch of two", "raw": '[{"jsonrpc":"2.0","id":4,"method":"ping"},{"jsonrpc":"2.0","id":5,"method":"ping"}]'},
        {"name": "jsonrpc missing", "raw": '{"id":6,"method":"ping"}'},
        {"name": "jsonrpc 1.0", "raw": '{"jsonrpc":"1.0","id":7,"method":"ping"}'},
        {"name": "jsonrpc a number", "raw": '{"jsonrpc":2,"id":8,"method":"ping"}'},
        {"name": "method missing", "raw": '{"jsonrpc":"2.0","id":9}'},
        {"name": "method a number", "raw": '{"jsonrpc":"2.0","id":10,"method":3}'},
        {"name": "id a float", "raw": '{"jsonrpc":"2.0","id":11.5,"method":"ping"}'},
        {"name": "id a float with a zero fraction", "raw": '{"jsonrpc":"2.0","id":12.0,"method":"ping"}'},
        {"name": "id null", "raw": '{"jsonrpc":"2.0","id":null,"method":"ping"}'},
        {"name": "id a bool", "raw": '{"jsonrpc":"2.0","id":true,"method":"ping"}'},
        {"name": "id an array", "raw": '{"jsonrpc":"2.0","id":[1],"method":"ping"}'},
        {"name": "id an object", "raw": '{"jsonrpc":"2.0","id":{"a":1},"method":"ping"}'},
        {"name": "id a string", "raw": '{"jsonrpc":"2.0","id":"abc","method":"ping"}'},
        {"name": "id an empty string", "raw": '{"jsonrpc":"2.0","id":"","method":"ping"}'},
        {"name": "id a numeric string", "raw": '{"jsonrpc":"2.0","id":"13","method":"ping"}'},
        {"name": "id negative", "raw": '{"jsonrpc":"2.0","id":-14,"method":"ping"}'},
        {"name": "id zero", "raw": '{"jsonrpc":"2.0","id":0,"method":"ping"}'},
        {"name": "id beyond i64", "raw": '{"jsonrpc":"2.0","id":99999999999999999999,"method":"ping"}'},
        {"name": "id a unicode string", "raw": '{"jsonrpc":"2.0","id":"\\u00e9\\ud83d\\ude00","method":"ping"}'},
        {"name": "id a long string", "raw": '{"jsonrpc":"2.0","id":"' + "x" * 300 + '","method":"ping"}'},
        {"name": "duplicate id keys", "raw": '{"jsonrpc":"2.0","id":15,"id":16,"method":"ping"}'},
        {"name": "params a string", "raw": '{"jsonrpc":"2.0","id":17,"method":"ping","params":"x"}'},
        {"name": "params an array", "raw": '{"jsonrpc":"2.0","id":18,"method":"tools/list","params":[]}'},
        {"name": "params null", "raw": '{"jsonrpc":"2.0","id":19,"method":"tools/list","params":null}'},
        {"name": "tools/call params a string", "raw": '{"jsonrpc":"2.0","id":20,"method":"tools/call","params":"x"}'},
        {"name": "tools/call params empty", "raw": '{"jsonrpc":"2.0","id":21,"method":"tools/call","params":{}}'},
        {"name": "tools/call name a number", "raw": '{"jsonrpc":"2.0","id":22,"method":"tools/call","params":{"name":3}}'},
        {"name": "tools/call name null", "raw": '{"jsonrpc":"2.0","id":23,"method":"tools/call","params":{"name":null}}'},
        {"name": "tools/call unknown tool", "send": call(24, "no_such_tool", {})},
        {"name": "tools/call empty tool name", "send": call(25, "", {})},
        {"name": "tools/call a tool name in other case", "send": call(26, "GET_CONTRACT", {})},
        {"name": "tools/call with _meta", "raw": '{"jsonrpc":"2.0","id":27,"method":"tools/call","params":{"name":"list_activities","arguments":{"limit":1},"_meta":{"progressToken":"p"}}}'},
        {"name": "tools/call with a task field", "raw": '{"jsonrpc":"2.0","id":28,"method":"tools/call","params":{"name":"list_activities","arguments":{"limit":1},"task":{"ttl":1000}}}'},
        {"name": "request with an unknown top-level key", "raw": '{"jsonrpc":"2.0","id":29,"method":"ping","extra":1}'},
        {"name": "method with a unicode name", "send": request(30, "héllo/世界")},
        {"name": "method empty", "send": request(31, "")},
        {"name": "method with a quote and a backslash", "send": request(32, 'a"b\\c')},
        {"name": "NaN in params", "raw": '{"jsonrpc":"2.0","id":33,"method":"ping","params":{"x":NaN}}'},
        {"name": "response-shaped line", "raw": '{"jsonrpc":"2.0","id":34,"result":{}}'},
        {"name": "error-shaped line", "raw": '{"jsonrpc":"2.0","id":35,"error":{"code":-1,"message":"x"}}'},
        {"name": "line with a CR before the newline", "raw": '{"jsonrpc":"2.0","id":36,"method":"ping"}\r'},
        {"name": "line with a leading BOM", "raw": '﻿{"jsonrpc":"2.0","id":37,"method":"ping"}'},
        {"name": "line with leading spaces", "raw": '   {"jsonrpc":"2.0","id":38,"method":"ping"}'},
        {"name": "line with trailing garbage", "raw": '{"jsonrpc":"2.0","id":39,"method":"ping"} x'},
        {"name": "two objects on one line", "raw": '{"jsonrpc":"2.0","id":40,"method":"ping"}{"jsonrpc":"2.0","id":41,"method":"ping"}'},
        {"name": "invalid UTF-8 in a string", "raw": '{"jsonrpc":"2.0","id":42,"method":"ping","params":{"x":"\\ud800"}}'},
        {"name": "deep nesting", "raw": '{"jsonrpc":"2.0","id":43,"method":"ping","params":{"x":' + "[" * 100 + "]" * 100 + "}}"},
        {"name": "ping afterwards", "send": request(99, "ping")}]})
    sessions.append({"name": "tools default", "entries": handshake() + _tool_default_calls()})
    sessions.append({"name": "tool results", "entries": handshake() + _tool_result_entries(contract)})
    coercion, _ = coercion_entries()
    sessions.append({"name": "coercion", "entries": handshake() + coercion})
    return {"sessions": sessions}


def _tool_result_entries(contract) -> list[dict]:
    """Every tool with the arguments that reach its branches: metrics, dates, limits, scopes, unknown names."""
    entries: list[dict] = []
    counter = [1]

    def add(label: str, name: str, arguments) -> None:
        counter[0] += 1
        entries.append({"name": f"{name} {label}", "send": call(counter[0], name, arguments)})

    every_metric = contract.metric_names() + contract.label_names()
    for days in (30,):
        add("every metric", "get_metric_series", {"metrics": every_metric, "days": days, "end_date": "2025-06-30"})
    for name in ("steps", "heart_rate", "sleep_score", "hrv_status", "stress", "spo2", "vo2max", "nope", ""):
        add(f"{name!r} default window", "get_metric_series", {"metrics": [name]})
    add("duplicates", "get_metric_series", {"metrics": ["steps", "heart_rate", "steps", "heart_rate"], "days": 10,
                                            "end_date": "2025-06-30"})
    add("a label twice", "get_metric_series", {"metrics": ["hrv_status", "hrv_status"], "days": 40,
                                               "end_date": "2025-06-30"})
    add("a label and a metric", "get_metric_series", {"metrics": ["hrv_status", "steps", "zzz"], "days": 40,
                                                      "end_date": "2025-06-30"})
    add("empty metrics", "get_metric_series", {"metrics": []})
    add("only unknown names, bad end_date", "get_metric_series", {"metrics": ["nope"], "end_date": "20250630"})
    add("only a label, bad end_date", "get_metric_series", {"metrics": ["hrv_status"], "end_date": "x"})
    add("a daily metric, bad end_date", "get_metric_series", {"metrics": ["steps"], "end_date": "x"})
    for days in (0, 1, -5, 365, 366, 367, 1825, 1826, 100000):
        for metric in ("steps", "heart_rate"):
            add(f"{metric} days={days}", "get_metric_series", {"metrics": [metric], "days": days,
                                                               "end_date": "2025-06-30"})
    for scope in ("device", "vendor_cloud", "local", "bogus", "", None):
        add(f"source_scope={scope!r}", "get_metric_series", {"metrics": ["steps", "sleep_score", "stress", "hrv_status"],
                                                             "days": 30, "end_date": "2025-06-30", "source_scope": scope})
    for end in ("", None, "2025-06-30", "2025-06-15", "1999-01-01", "2099-12-31", "2025-02-30", "2025-6-30",
                "20250630", "2025-W26-1", " 2025-06-30", "2025-06-30 ", "2025-06-30T00:00:00Z", "9999-12-31",
                "0001-01-01", "٢٠٢٥-06-30", "2025-06-30\n"):
        add(f"end_date={end!r}", "get_metric_series", {"metrics": ["steps", "heart_rate"], "days": 5, "end_date": end})
        add(f"end_date={end!r}", "get_period_facts", {"end_date": end, "window_days": 3, "baseline_days": 5})
        add(f"date={end!r}", "get_sleep_detail", {"date": end})
    add("no date", "get_sleep_detail", {})
    add("the night of the seed", "get_sleep_detail", {"date": "2025-06-30"})
    add("a night without sleep", "get_sleep_detail", {"date": "2025-06-01"})
    add("a night far away", "get_sleep_detail", {"date": "1999-01-01"})
    for limit in (0, 1, 2, 3, 20, 199, 200, 201, -1, 100000):
        add(f"limit={limit}", "list_activities", {"limit": limit})
    add("default", "list_activities", {})
    add("default", "get_data_health", {})
    for window in (0, 1, 7, 30, 365, 3650, 3651, -1):
        add(f"window_days={window}", "get_data_health", {"window_days": window})
    add("default", "get_period_facts", {})
    add("include_points", "get_period_facts", {"include_points": True})
    add("include_points, end_date", "get_period_facts", {"include_points": True, "end_date": "2025-06-30"})
    for scope in ("device", "vendor_cloud", "local", "bogus", "", None):
        add(f"source_scope={scope!r}", "get_period_facts", {"source_scope": scope, "end_date": "2025-06-30",
                                                            "include_points": True})
    add("unknown metric names", "get_period_facts", {"metrics": ["nope", "steps", "nope", ""], "end_date": "2025-06-30"})
    add("only unknown metric names", "get_period_facts", {"metrics": ["nope"]})
    add("empty metrics", "get_period_facts", {"metrics": []})
    add("metrics null", "get_period_facts", {"metrics": None})
    add("duplicate metrics", "get_period_facts", {"metrics": ["steps", "steps", "sleep_score"], "end_date": "2025-06-30"})
    add("a label metric", "get_period_facts", {"metrics": ["hrv_status"], "end_date": "2025-06-30"})
    add("every metric, points", "get_period_facts", {"metrics": every_metric, "include_points": True,
                                                     "end_date": "2025-06-30"})
    for window, baseline in ((1, 1), (7, 28), (31, 365), (32, 366), (0, 0), (-3, -3), (100000, 100000)):
        add(f"window_days={window}, baseline_days={baseline}", "get_period_facts",
            {"window_days": window, "baseline_days": baseline, "end_date": "2025-06-30"})
    add("", "get_contract", {})
    return entries


def script_text() -> str:
    return json.dumps(build_script(), indent=1, ensure_ascii=True) + "\n"


# ---- the oracle ----

def record_store(name: str) -> list:
    """The Python server's transcript of the script on one of ``STORES``."""
    store, keys = STORES[name]
    with _scratch() as scratch:
        run = mcp_diff.run_script([str(mcp_diff.PY_MCP)], "gen", mcp_diff.load_script(SCRIPT), store, keys, scratch,
                                  PINNED_NOW, mcp_diff.TZ, None)
    return run.records


def oracle_path(name: str) -> pathlib.Path:
    return FIXTURES / f"oracle-{name}.jsonl.gz"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oracle", action="store_true", help="also rewrite the Python transcripts")
    parser.add_argument("--encrypted", action="store_true", help="rebuild the encrypted store (random salt)")
    args = parser.parse_args()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if args.encrypted or not ENCRYPTED.exists():
        build_encrypted(ENCRYPTED, ENCRYPTED_KEYS)
        print(f"wrote {ENCRYPTED.name}: {ENCRYPTED.stat().st_size} bytes")
    build_privacy_seed(PRIVACY_SEED)
    SCRIPT.write_text(script_text(), encoding="utf-8")
    print(f"wrote {SCRIPT.name}: {sum(len(s['entries']) for s in build_script()['sessions'])} entries")
    MCP_JSON.write_text(rendered(), encoding="utf-8")
    print(f"wrote {MCP_JSON.name}: {MCP_JSON.stat().st_size} bytes")
    if args.oracle:
        for store_name in STORES:
            records = record_store(store_name)
            mcp_diff.write_oracle(oracle_path(store_name), records)
            lines = sum(len(e["stdout"]) for r in records for e in r.entries)
            print(f"wrote {oracle_path(store_name).name}: {len(records)} sessions, {lines} lines, "
                  f"{sum(len(r.stderr) for r in records)} stderr lines")
