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
database other than ``~/.hearthbeat/hearthbeat.db``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from disconect import __version__, contract, health, identity, insight, queries, storage
from disconect.storage import sqlite

INSTRUCTIONS = (
    f"{identity.PRODUCT} serves one person's watch health data from a local database. "
    f"{contract.PRIVACY_NOTE}\nTime: {contract.TIME_CONVENTION}\n"
    f"Missing values: {contract.MISSING_VALUE_CONVENTION}\nSources: {contract.SOURCE_CONVENTION}\n"
    f"Coverage: {contract.COVERAGE_CONVENTION}\n"
    "Before concluding that a period has no data, call get_data_health and read its coverage "
    "ledger: a day is present, failed, source_empty or not_covered -- never guess which. "
    "Do not diagnose; describe."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)

server = MCPServer(name=identity.MCP_SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)


@contextlib.contextmanager
def _db() -> Iterator[sqlite.Connection]:
    """A read-only connection for one tool call; problems become ToolErrors the client can read."""
    try:
        conn = storage.open_read_only(storage.default_db_path(), allow_prompt=False)
    except storage.NotConfigured as exc:
        raise ToolError(f"{exc}. Nothing can be answered until an import has run.") from exc
    except storage.SchemaTooNew as exc:
        raise ToolError(str(exc)) from exc
    except (storage.Encrypted, storage.NotEncrypted) as exc:
        raise ToolError(f"{exc}. The store is encrypted or locked: run 'disconect key cache' once in a "
                        "terminal, then restart disconect-mcp.") from exc
    except storage.DatabaseError as exc:
        raise ToolError(f"database could not be opened: {exc}") from exc
    try:
        yield conn
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except sqlite.Error as exc:
        raise ToolError(f"database error: {exc}") from exc
    finally:
        conn.close()


@server.tool(name="get_data_health", annotations=READ_ONLY, description=(
    "What the local store holds: per-stream import provenance and failures, per-metric coverage "
    "(all time and in a recent window), sleep nights, activities, recent imports, and the coverage "
    "ledger: for every metric and source scope, how many days in the window (up to 3650) are present, "
    "failed, source_empty or not_covered, with the gap ranges. Use it to tell 'never imported' from "
    "'failed to decode' from 'the source had nothing'. " + contract.COVERAGE_CONVENTION))
def get_data_health(window_days: int = 30) -> dict[str, Any]:
    """window_days: coverage window ending today, 1-3650."""
    with _db() as conn:
        return health.data_health(conn, window_days)


@server.tool(name="get_metric_series", annotations=READ_ONLY, description=(
    "Daily series for one or more metrics (see the metric enum), newest window ending today or "
    "end_date. Daily metrics return {date, value}; sample metrics (heart_rate, stress, "
    "respiration_rate, spo2, hrv_rmssd) return per-local-day {date, min, mean, max, samples}. "
    "Each series states its unit and source_scope; scopes are never merged. Unknown metric names "
    "are listed in ignored_metrics, not errors. " + contract.MISSING_VALUE_CONVENTION))
def get_metric_series(metrics: list[str], days: int = 90, source_scope: str | None = None,
                      end_date: str | None = None) -> dict[str, Any]:
    """metrics: names from the contract; days: 1-1825 (sample metrics capped at 366);
    source_scope: device | vendor_cloud | local | omitted for all; end_date: YYYY-MM-DD."""
    if not metrics:
        raise ToolError("metrics must name at least one metric; call get_contract for the list")
    with _db() as conn:
        return queries.metric_series(conn, metrics, days, source_scope, end_date)


@server.tool(name="get_sleep_detail", annotations=READ_ONLY, description=(
    "One night in full: window, stage minutes, the score breakdown, overnight SpO2/HR/respiration, "
    "and the stage timeline when the watch recorded one. Every source's record of the night is "
    "returned side by side. Omit date for the latest night. " + contract.MISSING_VALUE_CONVENTION))
def get_sleep_detail(date: str | None = None) -> dict[str, Any]:
    """date: local YYYY-MM-DD the sleep ended on; omitted = latest stored night."""
    with _db() as conn:
        return queries.sleep_detail(conn, date)


@server.tool(name="list_activities", annotations=READ_ONLY, description=(
    "Recent recorded activities (sport, duration, distance, calories, heart rate), newest first. "
    "No routes or coordinates are stored or returned. " + contract.MISSING_VALUE_CONVENTION))
def list_activities(limit: int = 20) -> dict[str, Any]:
    """limit: 1-200."""
    with _db() as conn:
        return queries.list_activities(conn, limit)


@server.tool(name="get_period_facts", annotations=READ_ONLY, description=(
    "Deterministic facts: the last window_days (default 7) of every metric with data, compared "
    "with this person's own baseline over the baseline_days (default 28) before it. Each fact "
    "carries value, direction, delta, z-score, confidence (high/medium/low/insufficient by "
    "baseline days with data), a reason_code and the evidence dates. Comparisons are only "
    "against the person's own history; no population norms exist here. A fact with confidence "
    "'insufficient' has no comparison, by design. Facts describe, they do not diagnose. "
    "end_date defaults to the latest stored date (see as_of). " + contract.MISSING_VALUE_CONVENTION))
def get_period_facts(window_days: int = 7, baseline_days: int = 28, end_date: str | None = None,
                     metrics: list[str] | None = None, source_scope: str | None = None,
                     include_points: bool = False) -> dict[str, Any]:
    """window_days 1-31; baseline_days 1-365; end_date YYYY-MM-DD; metrics: contract names or omitted
    for all; source_scope: device | vendor_cloud | local | omitted for all; include_points adds the
    window's per-day values to each fact's evidence (dates are always included)."""
    with _db() as conn:
        return insight.period_facts(conn, window_days, baseline_days, end_date, metrics, source_scope,
                                    include_points)


@server.tool(name="get_contract", annotations=READ_ONLY, description=(
    f"The read contract every {identity.PRODUCT} outlet follows: time, missing-value and source conventions, "
    "and the full list of metrics with units, cadence and meaning."))
def get_contract() -> dict[str, Any]:
    return contract.as_dict()


def main() -> None:
    """Entry point for ``disconect-mcp``: unlock once (never prompting), then serve over stdio."""
    import sys
    try:
        storage.prime(storage.default_db_path(), allow_prompt=False)
    except storage.Encrypted as exc:
        print(f"disconect-mcp: {exc}", file=sys.stderr)
        sys.exit(9)
    server.run("stdio")


if __name__ == "__main__":
    main()
