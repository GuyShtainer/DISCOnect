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

import bisect
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

#: Two consecutive readings of a per-minute metric at most this many seconds apart span worn time
#: between them (five minutes bridges a short unmeasurable spell; a charger hour shows as a gap).
WORN_GAP_S = 300
#: The seconds a reading accounts for on its own (the per-minute cadence): the last of a run, or one
#: whose next reading is farther than ``WORN_GAP_S``.
READING_S = 60

MAX_WINDOW_DAYS = 3650  # the status window; gap lists, not the window, are what is capped
MAX_GAPS = 20

_DAY = datetime.timedelta(days=1)


def earlier(day: datetime.date, days: int) -> datetime.date:
    """``days`` days before ``day``, never before the calendar's first day (0001-01-01).

    A window that would start before the calendar is shortened instead of failing the read (the same rule
    as ``fetch_window`` at the ends). Defined for ``days >= 0`` only (every caller passes a count); a negative
    ``days`` reads as 0, so the answer is never after ``day``. Twin of the Rust ``queries::earlier``.
    """
    return datetime.date.fromordinal(max(1, day.toordinal() - max(0, days)))


def fetch_window(first_day: datetime.date, last_day: datetime.date) -> tuple[str, str]:
    """ISO ``[lo, hi)`` bounds that over-fetch one day before and two after, clamped to the calendar.

    A local day starts up to 14 h either side of its UTC namesake, so readers fetch a wider UTC
    window and filter. At 0001-01-01 / 9999-12-31 the window is clamped instead of overflowing.
    """
    lo = earlier(first_day, 1)
    ceiling = datetime.date.max - 2 * _DAY
    hi = last_day + 2 * _DAY if last_day <= ceiling else datetime.date.max
    return lo.isoformat(), hi.isoformat()


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


#: The samples of one (metric, scope) in one UTC hour bucket (``YYYY-MM-DDTHH``). The ``ts_utc`` range lets
#: SQLite search the sample index for the hour; with ``substr`` alone it walked every row of the metric
#: once per hour that holds a local midnight (7b-13 review: ~11 s on both cores for a year of minutes on a
#: half-hour-zone watch). The ``substr`` keeps the row set exactly the bucket's.
_HOUR_SAMPLES = ("SELECT ts_utc FROM metric_samples WHERE metric=? AND source_scope=? "
                 "AND ts_utc >= ? AND ts_utc < ? AND substr(ts_utc, 1, 13)=?")


def _hour_end(hour: str) -> str:
    """The first text after every text that starts with ``hour``: its last character, one higher."""
    return hour[:-1] + chr(ord(hour[-1]) + 1)


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
    lo, hi = fetch_window(window.first, window.last)
    for metric, scope, hour in conn.execute(
            "SELECT metric, source_scope, substr(ts_utc, 1, 13) FROM metric_samples "
            "WHERE ts_utc >= ? AND ts_utc < ? GROUP BY 1, 2, 3", (lo, hi)):
        first_day = offsets.local_date(parse_iso_utc(hour + ":00:00Z"))
        last_day = offsets.local_date(parse_iso_utc(hour + ":59:59Z"))
        if first_day == last_day:
            days = [first_day]
        else:  # a local midnight falls inside this hour (half-hour zones): resolve each sample
            days = {offsets.local_date(parse_iso_utc(ts)) for (ts,) in conn.execute(
                _HOUR_SAMPLES, (metric, scope, hour, _hour_end(hour), hour))}
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
    offsets: ClockOffsets


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


def _analyse(conn: sqlite.Connection, first_day: str, last_day: str,
             offsets: ClockOffsets | None = None) -> _Analysis:
    """The status of every local day in the window, for every (metric, scope) the store knows.

    ``offsets`` is the store's clock offsets when the caller already loaded them; None loads them here.
    """
    window = _Window(first_day, last_day)
    if window.days < 1:
        raise ValueError("last_day must not be before first_day")
    if offsets is None:
        offsets = ClockOffsets.load(conn)
    refinements = migrations.has_table(conn, "export_ranges") and migrations.has_table(conn, "import_failures")
    streams_for, drift = _declared_and_drift(conn)
    covered, undatable = _covered_by_stream(conn, window, offsets, refinements)
    failed, unattributed = _failed_by_stream(conn, window, offsets, refinements)
    present = _present_days(conn, window, offsets)
    statuses = {key: _statuses(window, present.get(key), tuple(sorted(streams)), covered, failed)
                for key, streams in streams_for.items()}
    return _Analysis(window, streams_for, drift, statuses, undatable, unattributed, refinements, offsets)


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


def ledger(conn: sqlite.Connection, last_day: str, window_days: int,
           offsets: ClockOffsets | None = None) -> dict:
    """The coverage block of ``data_health``: per (metric, scope) counts and gaps over the window.

    ``last_day`` is the window's last local date (inclusive); ``window_days``
    is clipped to :data:`MAX_WINDOW_DAYS`; ``offsets`` is the store's clock offsets when the
    caller already loaded them (None loads them here). Returns plain data: no values, no
    identifiers.
    """
    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    last = datetime.date.fromisoformat(last_day)
    analysis = _analyse(conn, earlier(last, window_days - 1).isoformat(), last.isoformat(), offsets)
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


# ---- completeness (7b-12): how much of a day a per-minute metric's mean rests on -------------

UTC = datetime.timezone.utc


def _epoch(moment: datetime.datetime) -> int:
    return int(moment.timestamp())


_DAY_S = 86_400
_LAST_SECOND = 253_402_300_799  # 9999-12-31T23:59:59Z, the calendar's last second
_I64 = 2 ** 63


def _day_bounds(window: _Window, offsets: ClockOffsets) -> list[int]:
    """Epoch seconds each window day starts at, plus the end of the last one (``window.days + 1`` entries).

    Local midnight is read under the offset nearest to it, so a day across an offset change is as long
    as the watch's clock made it; UTC is assumed where no offset is known, as ``local_date`` does. The
    bounds never step back: an offset that jumps by more than a day between two midnights (a watch clock
    that was never set) leaves the day between them empty rather than negative, so the clipping stays
    non-negative and both cores search a sorted list. The bounds are plain integers, so the midnight
    after 9999-12-31 and a local midnight before year 1 are bounds too (the offset nearest to a moment
    past the calendar is read at its last second); only an offset the Rust twin's i64 cannot subtract
    raises OverflowError.
    """
    bounds: list[int] = []
    first = _epoch(datetime.datetime.combine(window.first, datetime.time(), tzinfo=UTC))
    for position in range(window.days + 1):
        midnight = first + position * _DAY_S
        moment = datetime.datetime.fromtimestamp(min(midnight, _LAST_SECOND), UTC)
        bound = midnight - (offsets.offset_at(moment) or 0)
        if not -_I64 <= bound < _I64:
            raise OverflowError("clock offset out of range")
        bounds.append(max(bound, bounds[-1]) if bounds else bound)
    return bounds


def _add_clipped(start: int, end: int, bounds: list[int], seconds: list[int]) -> None:
    """Add the seconds of ``[start, end)`` that fall inside each window day to ``seconds`` (one slot per day)."""
    position = max(bisect.bisect_right(bounds, start) - 1, 0)
    start = max(start, bounds[0])
    while position < len(seconds) and start < end:
        piece_end = min(end, bounds[position + 1])
        seconds[position] += piece_end - start
        start = piece_end
        position += 1


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping or touching spans folded into one each, in time order."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered_seconds(conn: sqlite.Connection, window: _Window, streams: tuple[str, ...], bounds: list[int],
                     refinements: bool) -> list[int]:
    """Per window day, the seconds some retained file of ``streams`` spans (a claimed window covers whole days)."""
    spans = []
    for stream in streams:
        if stream in contract.SESSION_STREAMS:
            continue  # a session claims no day
        for start_utc, end_utc in conn.execute("SELECT start_utc, end_utc FROM raw_records WHERE stream=?", (stream,)):
            if start_utc and end_utc:
                spans.append((_epoch(parse_iso_utc(start_utc)), _epoch(parse_iso_utc(end_utc))))
    seconds = [0] * window.days
    for start, end in _merged(spans):
        _add_clipped(start, end, bounds, seconds)
    if refinements:
        claimed = bytearray(window.days)
        for stream in streams:
            for from_day, to_day in conn.execute("SELECT from_day, to_day FROM export_ranges WHERE stream=?", (stream,)):
                window.mark(claimed, from_day, to_day)
        for position, flag in enumerate(claimed):
            if flag:
                seconds[position] = bounds[position + 1] - bounds[position]
    return seconds


def _worn_seconds(conn: sqlite.Connection, metric: str, scope: str, window: _Window, bounds: list[int]) -> list[int]:
    """Per window day, the seconds a reading accounts for: the gap to the next reading when that is at
    most ``WORN_GAP_S``, else its own minute (``READING_S``)."""
    # a run of readings can start the day before the window and the last window day can end 14 h after
    # its UTC namesake: over-fetch as the aggregates do and let the clipping sort it out
    lo, hi = fetch_window(window.first, window.last)
    seconds = [0] * window.days
    previous: int | None = None
    for (ts_utc,) in conn.execute(
            "SELECT ts_utc FROM metric_samples WHERE metric=? AND source_scope=? AND ts_utc >= ? AND ts_utc < ? "
            "ORDER BY ts_utc", (metric, scope, lo, hi)):
        moment = _epoch(parse_iso_utc(ts_utc))
        if previous is not None:
            _add_clipped(previous, moment if moment - previous <= WORN_GAP_S else previous + READING_S, bounds, seconds)
        previous = moment
    if previous is not None:
        _add_clipped(previous, previous + READING_S, bounds, seconds)
    return seconds


def _completeness_from(conn: sqlite.Connection, metric: str, scope: str, window: _Window, offsets: ClockOffsets,
                       streams_for: dict[tuple[str, str], set[str]]) -> list[int | None]:
    """Per window position, the completeness share of one (metric, scope) from an analysis's offsets and
    stream map (``contract.COMPLETENESS_CONVENTION``): None for a pair that is not per-minute, for a
    session scope, and for a day no file of the pair's streams spans."""
    if metric not in contract.PER_MINUTE_METRICS or scope in contract.SESSION_SCOPES:
        return [None] * window.days
    streams = tuple(sorted(streams_for.get((metric, scope), ())))
    bounds = _day_bounds(window, offsets)
    covered = _covered_seconds(conn, window, streams, bounds, migrations.has_table(conn, "export_ranges"))
    worn = _worn_seconds(conn, metric, scope, window, bounds)
    return [(min(100, 100 * worn[position] // covered[position]) if covered[position] > 0 else None)
            for position in range(window.days)]


def calendar(conn: sqlite.Connection, metric: str, scope: str, first_day: str,
             last_day: str) -> list[tuple[str, str, int | None]]:
    """``(day, status, completeness)`` for every local day from ``first_day`` to ``last_day``, oldest
    first, in one coverage pass: the status as ``day_statuses`` gives it, the completeness as
    ``day_completeness`` gives it (7b-13: ``metric_calendar`` used to pay for the store-wide stream map
    twice). Raises ValueError for a malformed or inverted range.
    """
    analysis = _analyse(conn, first_day, last_day)
    statuses = analysis.statuses.get((metric, scope)) or [NOT_COVERED] * analysis.window.days
    shares = _completeness_from(conn, metric, scope, analysis.window, analysis.offsets, analysis.streams_for)
    return [(analysis.window.day(position), statuses[position], shares[position])
            for position in range(analysis.window.days)]


def day_completeness(conn: sqlite.Connection, metric: str, scope: str, first_day: str,
                     last_day: str) -> dict[str, int | None]:
    """Per local day, the share (0-100) of the covered seconds a reading accounts for (the gap to the
    next reading when at most ``WORN_GAP_S``, else its minute; ``contract.COMPLETENESS_CONVENTION``);
    None for a pair that is not per-minute,
    for a session scope, and for a day no file of the pair's streams spans. Raises ValueError for a
    malformed or inverted range. A projection of ``calendar``.
    """
    return {day: share for day, _status, share in calendar(conn, metric, scope, first_day, last_day)}
