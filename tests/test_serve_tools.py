# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tools.call`` over the serve protocol (Bet 15, slice 2), on the Python oracle: the six MCP tools answered
through the session's own store, with the SDK's own argument coercion and result conversion. The Rust core is
compared with these answers by ``tools/serve_diff.py`` (the ``gen: tools.call`` script entries)."""

from __future__ import annotations

import asyncio
import json

import pytest

import gen_mcp_fixtures
from disconect import mcp_server, serve, storage
from test_privacy import _seed
from test_serve import PASS, Rig, _encrypt

#: (tool, arguments) the SDK and ``tools.call`` must answer alike: defaults, coercions, every parameter.
CASES = [
    ("get_data_health", {}),
    ("get_data_health", {"window_days": 400}),
    ("get_data_health", {"window_days": "7"}),
    ("get_metric_series", {"metrics": ["steps", "heart_rate"]}),
    ("get_metric_series", {"metrics": ["steps", "heart_rate"], "source_scope": "device", "days": "30"}),
    ("get_metric_series", {"metrics": '["steps"]'}),
    ("get_sleep_detail", {}),
    ("get_sleep_detail", {"date": "2025-06-16"}),
    ("list_activities", {}),
    ("list_activities", {"limit": "3"}),
    ("list_activities", {"limit": 0}),
    ("get_period_facts", {}),
    ("get_period_facts", {"include_points": True, "metrics": ["steps"]}),
    ("get_period_facts", {"metrics": ["no_such_metric"]}),
    ("get_contract", {}),
]


@pytest.fixture
def plain(db_path):
    _seed(db_path)
    return Rig(db_path)


@pytest.fixture
def encrypted(db_path, monkeypatch, capsys):
    _seed(db_path)
    _encrypt(db_path, monkeypatch)
    capsys.readouterr()
    return Rig(db_path)


def _sdk(tool: str, arguments: dict):
    """What the MCP server puts in ``structuredContent`` (or the text of its ``ToolError``)."""
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        result = asyncio.run(mcp_server.server.call_tool(tool, arguments))
    except ToolError as exc:
        return "error", str(exc)
    return "ok", result.structured_content


@pytest.mark.parametrize("tool, arguments", CASES, ids=[f"{tool} {json.dumps(args)}" for tool, args in CASES])
def test_tools_call_answers_what_the_mcp_tool_puts_in_structured_content(plain, db_path, monkeypatch, tool, arguments):
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(db_path))
    kind, expected = _sdk(tool, arguments)
    assert kind == "ok", expected
    assert plain.result("tools.call", name=tool, arguments=arguments) == {"name": tool, "result": expected}


def test_arguments_may_be_left_out_or_null(plain):
    left_out = plain.result("tools.call", name="get_sleep_detail")
    assert left_out == plain.result("tools.call", name="get_sleep_detail", arguments=None)
    assert left_out == plain.result("tools.call", name="get_sleep_detail", arguments={})
    assert left_out == plain.result("tools.call", name="get_sleep_detail", arguments={"surplus": 1})


def test_the_session_store_is_read_not_the_mcp_environment(plain, db_path, tmp_path, monkeypatch):
    monkeypatch.setenv(storage.DEFAULT_DB_ENV, str(tmp_path / "nowhere.db"))
    assert plain.result("tools.call", name="get_sleep_detail")["result"]["date"] == "2025-06-30"


def test_the_result_names_no_manufacturer(plain):
    for tool, arguments in CASES:
        text = json.dumps(plain.result("tools.call", name=tool, arguments=arguments))
        assert "garmin" not in text.lower(), tool


@pytest.mark.parametrize("params, code, message", [
    ({}, "invalid_params", "name must be a non-empty string"),
    ({"name": 5}, "invalid_params", "name must be a non-empty string"),
    ({"name": ""}, "invalid_params", "name must be a non-empty string"),
    ({"name": None}, "invalid_params", "name must be a non-empty string"),
    ({"name": "get_contract", "arguments": []}, "invalid_params", "arguments must be an object"),
    ({"name": "get_contract", "arguments": "{}"}, "invalid_params", "arguments must be an object"),
    ({"name": "no_such_tool"}, "unknown_tool", "Unknown tool: no_such_tool"),
    ({"name": "get_contract ", "arguments": {}}, "unknown_tool", "Unknown tool: get_contract "),
    ({"name": "list_activities", "arguments": {"limit": "abc"}}, "invalid_params", "arguments rejected: limit"),
    ({"name": "list_activities", "arguments": {"limit": 2.5}}, "invalid_params", "arguments rejected: limit"),
    ({"name": "list_activities", "arguments": {"limit": None}}, "invalid_params", "arguments rejected: limit"),
    ({"name": "get_period_facts", "arguments": {"window_days": "x", "baseline_days": [], "end_date": 5}},
     "invalid_params", "arguments rejected: baseline_days, end_date, window_days"),
    ({"name": "get_metric_series", "arguments": {}}, "invalid_params", "arguments rejected: metrics"),
    ({"name": "get_metric_series", "arguments": {"metrics": [1, 2]}}, "invalid_params", "arguments rejected: metrics"),
    ({"name": "get_metric_series", "arguments": {"metrics": []}}, "tool_error",
     "metrics must name at least one metric; call get_contract for the list"),
    ({"name": "get_sleep_detail", "arguments": {"date": "nonsense"}}, "tool_error", "date must be YYYY-MM-DD"),
    ({"name": "get_period_facts", "arguments": {"end_date": "2025-6-1"}}, "tool_error", "end_date must be YYYY-MM-DD"),
])
def test_failures_have_a_stable_code_and_never_echo_a_value(plain, params, code, message):
    response = plain.send("tools.call", **params)
    assert response["error"] == {"code": code, "message": message}


def test_a_value_the_tool_rejects_is_not_in_the_message(plain):
    response = plain.send("tools.call", name="list_activities", arguments={"limit": "secret-looking-text"})
    assert "secret" not in json.dumps(response)


def test_an_unexpected_exception_is_a_crash_text_with_only_the_tool_name(plain, monkeypatch):
    def boom(conn, **_kwargs):
        raise RuntimeError("detail that stays on the server /Users/someone")
    monkeypatch.setitem(mcp_server.BODIES, "get_sleep_detail", boom)
    response = plain.send("tools.call", name="get_sleep_detail")
    assert response["error"] == {"code": "tool_error", "message": "Error executing tool get_sleep_detail"}


def test_a_store_that_is_not_there_says_what_the_mcp_says(tmp_path):
    rig = Rig(tmp_path / "missing.db")
    got = rig.send("tools.call", name="get_data_health")["error"]
    assert got["code"] == "tool_error" and "Nothing can be answered until an import has run." in got["message"]
    assert rig.result("tools.call", name="get_contract")["name"] == "get_contract", "the contract needs no store"


def test_a_locked_store_answers_locked_until_it_is_unlocked(encrypted):
    for params in ({"name": "get_contract"}, {"name": "no_such_tool"}, {}):
        assert encrypted.send("tools.call", **params)["error"]["code"] == "locked"
    assert encrypted.result("key.unlock", passphrase=PASS) == {"unlocked": True}
    assert encrypted.result("tools.call", name="get_sleep_detail")["result"]["date"] == "2025-06-30"


def test_the_sdk_texts_tools_call_uses_are_the_ones_in_mcp_json():
    errors = json.loads(gen_mcp_fixtures.MCP_JSON.read_text(encoding="utf-8"))["errors"]
    assert mcp_server.UNKNOWN_TOOL_TEMPLATE == errors["unknown_tool"]
    assert mcp_server.CRASH_TEMPLATE == errors["crash"]


def test_every_tool_the_server_lists_has_a_body():
    listed = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    assert set(mcp_server.BODIES) == listed


def test_the_method_is_in_the_registry():
    assert serve.METHODS["tools.call"] is serve.tools_call
