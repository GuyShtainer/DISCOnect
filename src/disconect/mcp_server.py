"""MCP server: let an assistant query this person's data without taking it away.

Boundaries, drawn hard:

* **Read-only.** Every call opens the SQLite file ``mode=ro`` with
  ``query_only`` set, so a write is refused by SQLite, not by a branch here.
* **No network, no port.** Transport is stdio only. Importing data is the
  CLI's job (``disconect import``), never this process's.
* **No identifiers.** Responses carry no device serial numbers, file paths,
  account ids or GPS coordinates.
* **Missing means missing.** A day without a sample has no point; nothing is
  filled with 0. Units, time and source semantics come from
  ``disconect.contract`` -- the same text the CLI prints.

Run with ``disconect-mcp`` (stdio). Set ``DISCONECT_DB`` to point at a
database other than ``~/.disconect/disconect.db`` (the old ``~/.hearthbeat`` folder is read until
``disconect migrate-home`` moves it).
"""

from __future__ import annotations

import contextlib
import functools
import pathlib
from collections.abc import Callable, Iterator
from typing import Any

import pydantic
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from disconect import contract, health, identity, insight, queries, storage
from disconect.storage import sqlite

INSTRUCTIONS = identity.neutral(
    f"{identity.PRODUCT} serves one person's watch health data from a local database. "
    f"{contract.PRIVACY_NOTE}\nTime: {contract.TIME_CONVENTION}\n"
    f"Missing values: {contract.MISSING_VALUE_CONVENTION}\nSources: {contract.SOURCE_CONVENTION}\n"
    f"Coverage: {contract.COVERAGE_CONVENTION}\n"
    "Before concluding that a period has no data, call get_data_health and read its coverage "
    "ledger: a day is present, failed, source_empty or not_covered -- never guess which. "
    "Do not diagnose; describe."
)


def _neutral_result(tool: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Run ``tool`` and scrub the manufacturer's name from its result, as ``serve`` does."""
    @functools.wraps(tool)
    def scrubbed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return identity.neutral(tool(*args, **kwargs))
    return scrubbed


#: Every ``ToolError`` text the server writes itself, as templates (``{exc}`` is the text of the exception that
#: caused it). Written to ``disconect-core/mcp.json`` so the Rust MCP says the same words.
ERROR_TEMPLATES = {
    "not_configured": "{exc}. Nothing can be answered until an import has run.",
    "schema_too_new": "{exc}",
    "locked": ("{exc}. The store is encrypted or locked: run 'disconect key cache' once in a terminal, "
               "then restart disconect-mcp."),
    "open_failed": "database could not be opened: {exc}",
    "value_error": "{exc}",
    "database_error": "database error: {exc}",
    "no_metrics": "metrics must name at least one metric; call get_contract for the list",
}

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)

server = MCPServer(name=identity.MCP_SERVER_NAME, version=identity.VERSION, instructions=INSTRUCTIONS)


#: The two texts the SDK writes itself around a tool call (``errors.unknown_tool`` and ``errors.crash`` of
#: ``disconect-core/mcp.json``, which ``tests/test_mcp_json.py`` holds equal to these); ``tools.call`` of
#: ``serve`` words its ``unknown_tool`` and crash failures with them.
UNKNOWN_TOOL_TEMPLATE = "Unknown tool: {name}"
CRASH_TEMPLATE = "Error executing tool {name}"


@contextlib.contextmanager
def open_db(db_path: pathlib.Path) -> Iterator[sqlite.Connection]:
    """A read-only connection to ``db_path`` for one tool call; problems become ToolErrors the client can read."""
    try:
        conn = storage.open_read_only(db_path, allow_prompt=False)
    except storage.NotConfigured as exc:
        raise ToolError(ERROR_TEMPLATES["not_configured"].format(exc=exc)) from exc
    except storage.SchemaTooNew as exc:
        raise ToolError(ERROR_TEMPLATES["schema_too_new"].format(exc=exc)) from exc
    except (storage.Encrypted, storage.NotEncrypted) as exc:
        raise ToolError(ERROR_TEMPLATES["locked"].format(exc=exc)) from exc
    except storage.DatabaseError as exc:
        raise ToolError(ERROR_TEMPLATES["open_failed"].format(exc=exc)) from exc
    try:
        yield conn
    except ValueError as exc:
        raise ToolError(ERROR_TEMPLATES["value_error"].format(exc=exc)) from exc
    except sqlite.Error as exc:
        raise ToolError(ERROR_TEMPLATES["database_error"].format(exc=exc)) from exc
    finally:
        conn.close()


def _db() -> contextlib.AbstractContextManager[sqlite.Connection]:
    """``open_db`` on the MCP's own resolution of the database path (``DISCONECT_DB`` or the data folder)."""
    return open_db(storage.default_db_path())


#: Tool name -> its body, ``body(conn, **arguments)``: what the tool answers from an open connection. The MCP
#: functions below call them under :func:`_db`; ``serve``'s ``tools.call`` calls them (through :func:`run_tool`)
#: on the session's own connection.
BODIES: dict[str, Callable[..., dict[str, Any]]] = {}


def _body(name: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def register(body: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        BODIES[name] = body
        return body
    return register


@_body("get_data_health")
def _data_health(conn: sqlite.Connection, window_days: int = 30) -> dict[str, Any]:
    return health.data_health(conn, window_days)


def _require_metrics(metrics: list[str]) -> None:
    """Refused before the store is opened, as a tool call is."""
    if not metrics:
        raise ToolError(ERROR_TEMPLATES["no_metrics"])


@_body("get_metric_series")
def _metric_series(conn: sqlite.Connection, metrics: list[str], days: int = 90, source_scope: str | None = None,
                   end_date: str | None = None) -> dict[str, Any]:
    return queries.metric_series(conn, metrics, days, source_scope, end_date)


@_body("get_sleep_detail")
def _sleep_detail(conn: sqlite.Connection, date: str | None = None) -> dict[str, Any]:
    return queries.sleep_detail(conn, date)


@_body("list_activities")
def _list_activities(conn: sqlite.Connection, limit: int = 20) -> dict[str, Any]:
    return queries.list_activities(conn, limit)


@_body("get_period_facts")
def _period_facts(conn: sqlite.Connection, window_days: int = 7, baseline_days: int = 28,
                  end_date: str | None = None, metrics: list[str] | None = None, source_scope: str | None = None,
                  include_points: bool = False) -> dict[str, Any]:
    return insight.period_facts(conn, window_days, baseline_days, end_date, metrics, source_scope, include_points)


@_body("get_contract")
def _contract(conn: sqlite.Connection | None = None) -> dict[str, Any]:
    return contract.as_dict()


#: Tools that answer without reading the store.
NO_DB = frozenset({"get_contract"})


class UnknownTool(Exception):
    """:func:`run_tool` was asked for a tool the server does not have."""

    def __init__(self, name: str):
        self.text = UNKNOWN_TOOL_TEMPLATE.format(name=name)
        super().__init__(self.text)


class ArgumentsRejected(Exception):
    """The arguments do not fit the tool's input schema; ``names`` are the parameters at fault, sorted."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__("arguments rejected: " + ", ".join(names))


def run_tool(name: str, arguments: dict[str, Any], db_path: pathlib.Path) -> dict[str, Any]:
    """What the MCP tool ``name`` puts in ``structuredContent``, for a store this process can already open.

    Arguments are validated by the SDK's own argument model (so ``"7"``, ``7.0`` and a JSON-text list are
    accepted exactly as the MCP accepts them); the body runs on a read-only connection to ``db_path`` and the
    result is scrubbed and converted exactly as the SDK converts it. Raises :class:`UnknownTool`,
    :class:`ArgumentsRejected`, ``ToolError`` (an anticipated failure, with its text) or whatever the body
    raised (a crash: the caller words it with ``CRASH_TEMPLATE``)."""
    tool = server._tool_manager.get_tool(name)  # noqa: SLF001 - the SDK has no public lookup by name
    if tool is None or name not in BODIES:
        raise UnknownTool(name)
    try:
        kwargs = tool.fn_metadata.validate_arguments(arguments)
    except pydantic.ValidationError as exc:
        raise ArgumentsRejected(sorted({str(error["loc"][0]) for error in exc.errors()})) from None
    if name == "get_metric_series":
        _require_metrics(kwargs["metrics"])
    if name in NO_DB:
        result = BODIES[name](None, **kwargs)
    else:
        with open_db(db_path) as conn:
            result = BODIES[name](conn, **kwargs)
    return tool.fn_metadata.convert_result(identity.neutral(result)).structured_content


@server.tool(name="get_data_health", annotations=READ_ONLY, description=(
    "What the local store holds: per-stream import provenance and failures, per-metric coverage "
    "(all time and in a recent window), sleep nights, activities, recent imports, and the coverage "
    "ledger: for every metric and source scope, how many days in the window (up to 3650) are present, "
    "failed, source_empty or not_covered, with the gap ranges. Use it to tell 'never imported' from "
    "'failed to decode' from 'the source had nothing'. " + contract.COVERAGE_CONVENTION))
@_neutral_result
def get_data_health(window_days: int = 30) -> dict[str, Any]:
    """window_days: coverage window ending today, 1-3650."""
    with _db() as conn:
        return _data_health(conn, window_days)


@server.tool(name="get_metric_series", annotations=READ_ONLY, description=(
    "Daily series for one or more metrics (see the metric enum), newest window ending today or "
    "end_date. Daily metrics return {date, value}; sample metrics (heart_rate, stress, "
    "respiration_rate, spo2, hrv_rmssd) return per-local-day {date, min, mean, max, samples}. "
    "Each series states its unit and source_scope; scopes are never merged. Unknown metric names "
    "are listed in ignored_metrics, not errors. " + contract.MISSING_VALUE_CONVENTION))
@_neutral_result
def get_metric_series(metrics: list[str], days: int = 90, source_scope: str | None = None,
                      end_date: str | None = None) -> dict[str, Any]:
    """metrics: names from the contract; days: 1-1825 (sample metrics capped at 366);
    source_scope: device | vendor_cloud | local | live | omitted for all; end_date: YYYY-MM-DD."""
    _require_metrics(metrics)
    with _db() as conn:
        return _metric_series(conn, metrics, days, source_scope, end_date)


@server.tool(name="get_sleep_detail", annotations=READ_ONLY, description=(
    "One night in full: window, stage minutes, the score breakdown, overnight SpO2/HR/respiration, "
    "and the stage timeline when the watch recorded one. Every source's record of the night is "
    "returned side by side. Omit date for the latest night. " + contract.MISSING_VALUE_CONVENTION))
@_neutral_result
def get_sleep_detail(date: str | None = None) -> dict[str, Any]:
    """date: local YYYY-MM-DD the sleep ended on; omitted = latest stored night."""
    with _db() as conn:
        return _sleep_detail(conn, date)


@server.tool(name="list_activities", annotations=READ_ONLY, description=(
    "Recent recorded activities (sport, duration, distance, calories, heart rate), newest first. "
    "No routes or coordinates are stored or returned. " + contract.MISSING_VALUE_CONVENTION))
@_neutral_result
def list_activities(limit: int = 20) -> dict[str, Any]:
    """limit: 1-200."""
    with _db() as conn:
        return _list_activities(conn, limit)


@server.tool(name="get_period_facts", annotations=READ_ONLY, description=(
    "Deterministic facts: the last window_days (default 7) of every metric with data, compared "
    "with this person's own baseline over the baseline_days (default 28) before it. Each fact "
    "carries value, direction, delta, z-score, confidence (high/medium/low/insufficient by "
    "baseline days with data), a reason_code and the evidence dates. Comparisons are only "
    "against the person's own history; no population norms exist here. A fact with confidence "
    "'insufficient' has no comparison, by design. Facts describe, they do not diagnose. "
    "end_date defaults to the latest stored date (see as_of). " + contract.MISSING_VALUE_CONVENTION))
@_neutral_result
def get_period_facts(window_days: int = 7, baseline_days: int = 28, end_date: str | None = None,
                     metrics: list[str] | None = None, source_scope: str | None = None,
                     include_points: bool = False) -> dict[str, Any]:
    """window_days 1-31; baseline_days 1-365; end_date YYYY-MM-DD; metrics: contract names or omitted
    for all; source_scope: device | vendor_cloud | local | live | omitted for every scope but live; include_points adds the
    window's per-day values to each fact's evidence (dates are always included)."""
    with _db() as conn:
        return _period_facts(conn, window_days, baseline_days, end_date, metrics, source_scope, include_points)


@server.tool(name="get_contract", annotations=READ_ONLY, description=(
    f"The read contract every {identity.PRODUCT} outlet follows: time, missing-value and source conventions, "
    "and the full list of metrics with units, cadence and meaning."))
@_neutral_result
def get_contract() -> dict[str, Any]:
    return _contract()


def main() -> None:
    """Entry point for ``disconect-mcp``: unlock once (never prompting), then serve over stdio."""
    import sys
    storage.home.announce_legacy_env()      # before the store resolves, so the warning survives a no-$HOME refusal
    try:
        db_path = storage.default_db_path()
    except storage.NoHome as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    storage.home.announce_default_resolution(db_path)
    try:
        storage.prime(db_path, allow_prompt=False)
    except storage.Encrypted as exc:
        print(f"disconect-mcp: {exc}", file=sys.stderr)
        sys.exit(9)
    server.run("stdio")


if __name__ == "__main__":
    main()
