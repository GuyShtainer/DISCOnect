"""The coverage ledger: why a (metric, scope, day) has no value.

Four statuses, decided per local day in a fixed order (see
``contract.COVERAGE_CONVENTION``): ``present`` > ``failed`` > ``source_empty``
> ``not_covered``. Everything here is derived, on request, from what the store
already retains -- ``raw_records`` spans, the export windows files claimed,
and the failures recorded with their spans -- so the ledger is right for every
import ever made, including those older than the tables that refine it. On a
schema without ``export_ranges`` / ``import_failures`` the ledger still
answers, with those refinements marked unavailable.

No value, label, path or device identifier is read or returned: the ledger is
dates, counts and stream names only.
"""

from __future__ import annotations

import dataclasses
import datetime

from disconect import contract
from disconect.ingest.clock import ClockOffsets
from disconect.storage import migrations, parse_iso_utc
from disconect.storage import sqlite

PRESENT = "present"
FAILED = "failed"
SOURCE_EMPTY = "source_empty"
NOT_COVERED = "not_covered"

MAX_WINDOW_DAYS = 3650  # the status window; gap lists, not the window, are what is capped
MAX_GAPS = 20

_DAY = datetime.timedelta(days=1)


class _Window:
    """Inclusive local-date window with day-index arithmetic."""

    def __init__(self, first: str, last: str):
        self.first = datetime.date.fromisoformat(first)
        self.last = datetime.date.fromisoformat(last)
        self.days = (self.last - self.first).days + 1

    def index(self, day: str) -> int | None:
        """Position of ``day`` inside the window, or None when outside it."""
        offset = (datetime.date.fromisoformat(day) - self.first).days
        return offset if 0 <= offset < self.days else None

    def mark(self, flags: bytearray, from_day: str, to_day: str) -> None:
        """Set every day of ``from_day..to_day`` (clipped to the window)."""
        start = max((datetime.date.fromisoformat(from_day) - self.first).days, 0)
        stop = min((datetime.date.fromisoformat(to_day) - self.first).days, self.days - 1)
        for position in range(start, stop + 1):
            flags[position] = 1

    def day(self, position: int) -> str:
        return (self.first + position * _DAY).isoformat()


def _observed_map(conn: sqlite.Connection) -> set[tuple[str, str, str]]:
    """Every (metric, scope, stream) triple the store has actually produced."""
    triples: set[tuple[str, str, str]] = set()
    for table in ("daily_metrics", "daily_labels", "metric_samples"):
        triples.update(tuple(row) for row in conn.execute(
            f"SELECT DISTINCT t.metric, t.source_scope, r.stream FROM {table} t "
            "JOIN raw_records r ON r.id = t.raw_record_id"))
    return triples


#: Device streams written once per night. The watch produces such a file only when there was
#: something to record, so the all-day monitoring files are what prove the day was imported.
NIGHTLY_DEVICE_STREAMS = {"fit:sleep": "fit:monitoring_b", "fit:hrv": "fit:monitoring_b",
                          "fit:skin_temp": "fit:monitoring_b"}


def _span_days(offsets: ClockOffsets, start_utc: str | None, end_utc: str | None) -> tuple[str, str] | None:
    """Local dates a UTC span touches. The end is exclusive: a file ending at local midnight
    (as monitoring files and Connect days do) does not cover the day that starts then."""
    if not start_utc or not end_utc:
        return None
    start, end = parse_iso_utc(start_utc), parse_iso_utc(end_utc)
    if end > start:
        end -= datetime.timedelta(microseconds=1)
    first, last = offsets.local_date(start), offsets.local_date(end)
    return (first, last) if first <= last else (last, first)


def _covered_by_stream(conn: sqlite.Connection, window: _Window, offsets: ClockOffsets,
                       refinements: bool) -> tuple[dict[str, bytearray], int]:
    """Per stream, the days some retained file or claimed window spans; plus the undatable-file count."""
    covered: dict[str, bytearray] = {}
    undatable = 0
    for stream, start_utc, end_utc in conn.execute("SELECT stream, start_utc, end_utc FROM raw_records"):
        if stream in contract.SESSION_STREAMS:
            continue  # a session claims no day
        span = _span_days(offsets, start_utc, end_utc)
        if span is None:
            undatable += 1
            continue
        window.mark(covered.setdefault(stream, bytearray(window.days)), *span)
    if refinements:
        for stream, from_day, to_day in conn.execute("SELECT stream, from_day, to_day FROM export_ranges"):
            window.mark(covered.setdefault(stream, bytearray(window.days)), from_day, to_day)
    for nightly, all_day in NIGHTLY_DEVICE_STREAMS.items():
        if all_day in covered:
            flags = covered.setdefault(nightly, bytearray(window.days))
            for position, flag in enumerate(covered[all_day]):
                flags[position] |= flag
    return covered, undatable


def _failed_by_stream(conn: sqlite.Connection, window: _Window, offsets: ClockOffsets,
                      refinements: bool) -> tuple[dict[str, bytearray], int]:
    """Per stream, the days a recorded failure spans; plus failures that cannot be placed on any day."""
    failed: dict[str, bytearray] = {}
    unattributed = 0
    if not refinements:
        return failed, unattributed
    for stream, start_utc, end_utc, raw_start, raw_end in conn.execute(
            "SELECT f.stream, f.start_utc, f.end_utc, r.start_utc, r.end_utc FROM import_failures f "
            "LEFT JOIN raw_records r ON r.id = f.raw_record_id"):
        if stream in contract.SESSION_STREAMS:
            continue  # a refused session record is a failed raw record, not a failed day
        span = _span_days(offsets, start_utc or raw_start, end_utc or raw_end)
        if stream is None or span is None:
            unattributed += 1
            continue
        window.mark(failed.setdefault(stream, bytearray(window.days)), *span)
    return failed, unattributed


def _present_days(conn: sqlite.Connection, window: _Window, offsets: ClockOffsets
                  ) -> dict[tuple[str, str], bytearray]:
    """Per (metric, scope), the local days that hold at least one row."""
    present: dict[tuple[str, str], bytearray] = {}
    first, last = window.first.isoformat(), window.last.isoformat()
    for table in ("daily_metrics", "daily_labels"):
        for metric, scope, day in conn.execute(
                f"SELECT DISTINCT metric, source_scope, date FROM {table} WHERE date BETWEEN ? AND ?",
                (first, last)):
            position = window.index(day)
            if position is not None:
                present.setdefault((metric, scope), bytearray(window.days))[position] = 1
    # Samples: resolve local dates once per UTC hour bucket, not per sample. Local days can
    # begin up to 14 h before/after their UTC namesake, so over-fetch one day each side.
    lo = (window.first - _DAY).isoformat()
    hi = (window.last + 2 * _DAY).isoformat()
    for metric, scope, hour in conn.execute(
            "SELECT metric, source_scope, substr(ts_utc, 1, 13) FROM metric_samples "
            "WHERE ts_utc >= ? AND ts_utc < ? GROUP BY 1, 2, 3", (lo, hi)):
        first_day = offsets.local_date(parse_iso_utc(hour + ":00:00Z"))
        last_day = offsets.local_date(parse_iso_utc(hour + ":59:59Z"))
        if first_day == last_day:
            days = [first_day]
        else:  # a local midnight falls inside this hour (half-hour zones): resolve each sample
            days = {offsets.local_date(parse_iso_utc(ts)) for (ts,) in conn.execute(
                "SELECT ts_utc FROM metric_samples WHERE metric=? AND source_scope=? "
                "AND substr(ts_utc, 1, 13)=?", (metric, scope, hour))}
        for day in days:
            position = window.index(day)
            if position is not None:
                present.setdefault((metric, scope), bytearray(window.days))[position] = 1
    return present


def _statuses(window: _Window, present: bytearray | None, streams: tuple[str, ...],
              covered: dict[str, bytearray], failed: dict[str, bytearray]) -> list[str]:
    failed_flags = [flags for stream in streams if (flags := failed.get(stream)) is not None]
    covered_flags = [flags for stream in streams if (flags := covered.get(stream)) is not None]
    out = []
    for position in range(window.days):
        if present is not None and present[position]:
            out.append(PRESENT)
        elif any(flags[position] for flags in failed_flags):
            out.append(FAILED)
        elif any(flags[position] for flags in covered_flags):
            out.append(SOURCE_EMPTY)
        else:
            out.append(NOT_COVERED)
    return out


def _gaps(window: _Window, statuses: list[str]) -> tuple[list[dict], bool]:
    """Runs of consecutive non-present days with one status, oldest first; the newest MAX_GAPS kept."""
    gaps: list[dict] = []
    run_start: int | None = None
    for position, status in enumerate(statuses + [PRESENT]):
        if run_start is not None and (status == PRESENT or status != statuses[run_start]):
            gaps.append({"from": window.day(run_start), "to": window.day(position - 1),
                         "status": statuses[run_start]})
            run_start = None
        if status != PRESENT and run_start is None:
            run_start = position
    if len(gaps) > MAX_GAPS:
        return gaps[-MAX_GAPS:], True
    return gaps, False


@dataclasses.dataclass
class _Analysis:
    """Everything the ledger derives for one window, before it is shaped for a caller."""

    window: _Window
    streams_for: dict[tuple[str, str], set[str]]
    drift: list[dict]
    statuses: dict[tuple[str, str], list[str]]
    undatable: int
    unattributed: int
    refinements: bool


def _declared_and_drift(conn: sqlite.Connection) -> tuple[dict[tuple[str, str], set[str]], list[dict]]:
    """Declared streams per (metric, scope), plus any observed stream the declaration lacks.

    A session pair (``contract.SESSION_STREAMS_FOR``) joins the ledger only once the store holds
    rows for it, so a store that never saw a live link shows no live rows at all; it is never drift.
    """
    streams_for: dict[tuple[str, str], set[str]] = {
        key: set(streams) for key, streams in contract.STREAMS_FOR.items()}
    drift = []
    for metric, scope, stream in sorted(_observed_map(conn)):
        if stream in contract.SESSION_STREAMS_FOR.get((metric, scope), ()):
            streams_for.setdefault((metric, scope), set()).update(contract.SESSION_STREAMS_FOR[(metric, scope)])
            continue
        if stream not in streams_for.setdefault((metric, scope), set()):
            streams_for[(metric, scope)].add(stream)
            drift.append({"metric": metric, "source_scope": scope, "stream": stream})
    return streams_for, drift


def _analyse(conn: sqlite.Connection, first_day: str, last_day: str) -> _Analysis:
    """The status of every local day in the window, for every (metric, scope) the store knows."""
    window = _Window(first_day, last_day)
    if window.days < 1:
        raise ValueError("last_day must not be before first_day")
    offsets = ClockOffsets.load(conn)
    refinements = migrations.has_table(conn, "export_ranges") and migrations.has_table(conn, "import_failures")
    streams_for, drift = _declared_and_drift(conn)
    covered, undatable = _covered_by_stream(conn, window, offsets, refinements)
    failed, unattributed = _failed_by_stream(conn, window, offsets, refinements)
    present = _present_days(conn, window, offsets)
    statuses = {key: _statuses(window, present.get(key), tuple(sorted(streams)), covered, failed)
                for key, streams in streams_for.items()}
    return _Analysis(window, streams_for, drift, statuses, undatable, unattributed, refinements)


def day_statuses(conn: sqlite.Connection, metric: str, scope: str, first_day: str,
                 last_day: str) -> dict[str, str]:
    """The coverage status of every local day from ``first_day`` to ``last_day`` for one metric and scope.

    Same precedence as the ledger: present, failed, source_empty, not_covered. A pair no stream
    can supply is ``not_covered`` throughout. Raises ValueError for a malformed or inverted range.
    """
    analysis = _analyse(conn, first_day, last_day)
    statuses = analysis.statuses.get((metric, scope)) or [NOT_COVERED] * analysis.window.days
    return {analysis.window.day(position): status for position, status in enumerate(statuses)}


def statuses_on(conn: sqlite.Connection, day: str) -> dict[tuple[str, str], str]:
    """The status of one local ``day`` for every (metric, scope) the store knows, in one pass."""
    analysis = _analyse(conn, day, day)
    return {key: statuses[0] for key, statuses in analysis.statuses.items()}


def ledger(conn: sqlite.Connection, last_day: str, window_days: int) -> dict:
    """The coverage block of ``data_health``: per (metric, scope) counts and gaps over the window.

    ``last_day`` is the window's last local date (inclusive); ``window_days``
    is clipped to :data:`MAX_WINDOW_DAYS`. Returns plain data: no values, no
    identifiers.
    """
    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    last = datetime.date.fromisoformat(last_day)
    analysis = _analyse(conn, (last - (window_days - 1) * _DAY).isoformat(), last.isoformat())
    window = analysis.window
    rows = []
    for (metric, scope), streams in sorted(analysis.streams_for.items()):
        statuses = analysis.statuses[(metric, scope)]
        gaps, truncated = _gaps(window, statuses)
        rows.append({
            "metric": metric, "source_scope": scope, "streams": sorted(streams),
            "sparse": metric in contract.SPARSE_METRICS,
            PRESENT: statuses.count(PRESENT), FAILED: statuses.count(FAILED),
            SOURCE_EMPTY: statuses.count(SOURCE_EMPTY), NOT_COVERED: statuses.count(NOT_COVERED),
            "gaps": gaps, "gaps_truncated": truncated,
        })
    return {
        "window": {"from": window.first.isoformat(), "to": window.last.isoformat(), "days": window.days},
        "statuses": [PRESENT, FAILED, SOURCE_EMPTY, NOT_COVERED],
        "ledger": rows,
        "map_drift": analysis.drift,
        "unattributed_failures": analysis.unattributed,
        "undatable_files": analysis.undatable,
        "refinements": ("claimed export windows and failure spans are recorded for imports made at "
                        "schema v2 or later; earlier imports contribute their files' spans only"
                        if analysis.refinements else
                        "claimed export windows and failure spans are not recorded on this database "
                        "(schema v1); files' spans still decide coverage. Any import or reparse "
                        "upgrades the schema; re-importing the export records its windows"),
        "refinements_available": analysis.refinements,
    }
