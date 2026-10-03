"""The language-neutral MCP description the Rust core loads (``projects/disconect-core/mcp.json``).

The live Python server is the source: ``tests/gen_mcp_fixtures.py`` runs it, measures what it serves and
answers, and this test demands the committed file be byte-identical. A change to a tool, a text, a clamp or the
SDK's coercion is one deliberate act: run ``python tests/gen_mcp_fixtures.py``, review the diff, commit. The
same goes for the request script and the committed stores the transcripts are played on.
"""

from __future__ import annotations

import json
import re

import gen_mcp_fixtures
from disconect import identity


def test_the_committed_mcp_json_is_what_the_python_server_serves():
    assert gen_mcp_fixtures.MCP_JSON.read_text(encoding="utf-8") == gen_mcp_fixtures.rendered(), (
        "mcp.json is stale: run `python tests/gen_mcp_fixtures.py` and commit it")


def test_what_the_rust_mcp_relies_on_is_in_the_file():
    data = json.loads(gen_mcp_fixtures.MCP_JSON.read_text(encoding="utf-8"))
    assert [tool["name"] for tool in data["tools"]] == ["get_data_health", "get_metric_series", "get_sleep_detail",
                                                        "list_activities", "get_period_facts", "get_contract"]
    for tool in data["tools"]:
        assert set(tool) == {"annotations", "description", "inputSchema", "name", "outputSchema"}, tool["name"]
    assert data["serverInfo"] == {"name": identity.MCP_SERVER_NAME, "version": identity.VERSION}
    assert data["protocol"] == {"accepted": ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"],
                                "default": "2025-11-25"}
    assert data["errors"]["unknown_tool"] == "Unknown tool: {name}"
    assert len(data["errors"]["templates"]) == 7
    assert set(data["coercions"]) == {"int", "str|None", "bool", "list[str]", "list[str]|None", "arguments"}
    outcomes = {case["outcome"] for group in data["coercions"].values() for case in group["cases"]}
    assert outcomes == {"accepted", "rejected", "protocol_error"}, "the table must hold accepts and rejects"
    for group in data["coercions"].values():
        for case in group["cases"]:
            assert ("coerced" in case) == (case["outcome"] == "accepted")


def test_the_file_names_no_manufacturer():
    """ADR 0001: the descriptions, instructions and conventions the file carries are the scrubbed ones."""
    text = gen_mcp_fixtures.MCP_JSON.read_text(encoding="utf-8")
    assert not re.search("garmin", text, re.IGNORECASE)
    assert identity.VENDOR_PLACEHOLDER in text


def test_the_validation_text_is_never_stored():
    """Named allowance kb23-pydantic-validation-text: the library's wording stays out of the contract."""
    text = gen_mcp_fixtures.MCP_JSON.read_text(encoding="utf-8")
    assert "validation error" not in text and "errors.pydantic.dev" not in text


def test_the_committed_request_script_is_what_the_generator_writes_today():
    assert gen_mcp_fixtures.SCRIPT.read_text(encoding="utf-8") == gen_mcp_fixtures.script_text(), (
        "regenerate: python tests/gen_mcp_fixtures.py")


def test_the_privacy_seed_store_is_rebuilt_to_the_same_bytes(tmp_path):
    """The seed rows are fixed and the clock is pinned, so the committed store is reproducible."""
    gen_mcp_fixtures.build_privacy_seed(tmp_path / "again.hbdb")
    assert (tmp_path / "again.hbdb").read_bytes() == gen_mcp_fixtures.PRIVACY_SEED.read_bytes(), (
        "regenerate: python tests/gen_mcp_fixtures.py")


def test_the_script_covers_what_the_pitch_lists():
    script = json.loads(gen_mcp_fixtures.SCRIPT.read_text(encoding="utf-8"))
    names = {session["name"] for session in script["sessions"]}
    for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28", "1999-01-01"):
        assert f"version {version}" in names
    assert {"pre-initialize", "methods", "malformed lines", "tools default", "tool results", "coercion"} <= names
    entries = [entry for session in script["sessions"] for entry in session["entries"]]
    methods = {entry["send"].get("method") for entry in entries if "send" in entry}
    assert {"initialize", "ping", "tools/list", "tools/call", "resources/list", "resources/templates/list",
            "prompts/list", "logging/setLevel", "completion/complete", "server/discover",
            "notifications/initialized", "notifications/cancelled", "notifications/unknown"} <= methods
    labels = " ".join(entry["name"] for entry in entries)
    for needle in ("batch of one", "jsonrpc missing", "id a float", "id null", "tools/list with a cursor",
                   "list_activities limit=0", "list_activities limit=1", "list_activities limit=200",
                   "list_activities limit=201", "get_metric_series duplicates", "a label twice",
                   "get_metric_series empty metrics", "get_period_facts end_date=''", "get_sleep_detail no date",
                   "include_points", "unknown metric names", "NaN in params"):
        assert needle in labels, needle
    for scope in ("device", "vendor_cloud", "local"):
        assert f"source_scope='{scope}'" in labels
    for name in ("get_data_health", "get_metric_series", "get_sleep_detail", "list_activities", "get_period_facts",
                 "get_contract"):
        assert f"default {name}" in labels
