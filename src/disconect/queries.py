"""Read-side queries shared by the MCP server and any future UI.

Everything here is read-only, returns plain data, quotes the contract, and
omits device serials, paths and identifiers. Sample-cadence metrics are served
as per-local-day aggregates: a model asking about "stress this month" needs
thirty rows, not forty thousand.
"""

from __future__ import annotations

import datetime
import re

from disconect import contract, coverage
from disconect.ingest.clock import ClockOffsets
from disconect.storage import parse_iso_utc
from disconect.storage._time import now_utc
from disconect.storage import sqlite

UTC = datetime.timezone.utc
MAX_DAILY_DAYS = 1825
MAX_SAMPLE_DAYS = 366


def _clean(value):
    """Round floats for presentation; FIT's fixed-point scales carry no more than 3 decimals."""
    return round(value, 3) if isinstance(value, float) else value


def _today() -> datetime.date:
    return now_utc().date()


def local_today(conn: sqlite.Connection) -> str:
    """Today as ``YYYY-MM-DD``: the later of the UTC date and the watch's local date.

    The watch's clock can be ahead of UTC (a late evening there is already tomorrow here), and
    a store without any known offset falls back to UTC, so the later of the two is "today".
    """
    now = now_utc()
    return max(now.date().isoformat(), ClockOffsets.load(conn).local_date(now))


_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_day(text: str, name: str) -> datetime.date:
    """``YYYY-MM-DD`` and nothing else (``date.fromisoformat`` alone also takes ``20261003`` and ``2026-W40-6``).

    Raises ``ValueError("<name> must be YYYY-MM-DD")`` for any other text, a real calendar day included or not.
    """
    if _DAY.fullmatch(text):
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            pass
    raise ValueError(f"{name} must be YYYY-MM-DD")


def _window(days: int, end_date: str | None, cap: int) -> tuple[str, str]:
    days = max(1, min(int(days), cap))
    end = parse_day(end_date, "end_date") if end_date else _today()
    start = end - datetime.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def sample_day_aggregates(conn: sqlite.Connection, metric: str, start: str, end: str,
                          offsets: ClockOffsets, scopes: tuple[str, ...]) -> dict[str, list[dict]]:
    """Per-local-day min/mean/max/count for one sample-cadence metric, keyed by source scope."""
    # Local days can begin up to 14 h before/after their UTC namesake; over-fetch and filter.
    lo = (datetime.date.fromisoformat(start) - datetime.timedelta(days=1)).isoformat()
    hi = (datetime.date.fromisoformat(end) + datetime.timedelta(days=2)).isoformat()
    buckets: dict[str, dict[str, list[float]]] = {}
    for ts_text, value, scope in conn.execute(
            "SELECT ts_utc, value, source_scope FROM metric_samples WHERE metric=? "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (metric, lo, hi)):
        if scope not in scopes:
            continue
        day = offsets.local_date(parse_iso_utc(ts_text))
        if start <= day <= end:
            buckets.setdefault(scope, {}).setdefault(day, []).append(value)
    out: dict[str, list[dict]] = {}
    for scope, days in buckets.items():
        out[scope] = [{"date": day, "min": _clean(min(v)), "mean": round(sum(v) / len(v), 2),
                       "max": _clean(max(v)), "samples": len(v)} for day, v in sorted(days.items())]
    return out


def intraday_samples(conn: sqlite.Connection, metric: str, date: str | None = None,
                     source_scope: str | None = None) -> dict:
    """Every stored sample of one sample-cadence metric inside one local day.

    ``date`` defaults to the last local day that has samples of this metric.
    Each point carries ``hour``: hours since local midnight (0.0-24.0), which
    is the only form an intraday x-axis can use, since the same local day
    spans a shifting UTC window. Raises ValueError for a daily-cadence metric.
    """
    if contract.cadence_for(metric) != contract.CADENCE_SAMPLE:
        raise ValueError(f"{metric!r} is not a sample-cadence metric")
    if source_scope is not None and source_scope not in contract.SOURCE_SCOPES:
        raise ValueError(f"source_scope must be one of {contract.SOURCE_SCOPES}")
    scopes = (source_scope,) if source_scope else contract.SOURCE_SCOPES
    offsets = ClockOffsets.load(conn)
    if date is None:
        date = _latest_sample_day(conn, metric, offsets, scopes)
        if date is None:
            return {"metric": metric, "unit": contract.unit_for(metric), "date": None, "series": [],
                    "reason": "no samples of this metric stored yet",
                    "missing_values": contract.MISSING_VALUE_CONVENTION,
                    "time": contract.TIME_CONVENTION}
    # A local day starts up to 14 h either side of its UTC namesake; over-fetch, then filter.
    lo = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    hi = (datetime.date.fromisoformat(date) + datetime.timedelta(days=2)).isoformat()
    by_scope: dict[str, list[dict]] = {}
    for ts_text, value, scope in conn.execute(
            "SELECT ts_utc, value, source_scope FROM metric_samples WHERE metric=? "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (metric, lo, hi)):
        if scope not in scopes:
            continue
        moment = parse_iso_utc(ts_text)
        if offsets.local_date(moment) != date:
            continue
        local = moment + datetime.timedelta(seconds=offsets.offset_at(moment) or 0)
        hour = local.hour + local.minute / 60 + local.second / 3600
        by_scope.setdefault(scope, []).append({"ts_utc": ts_text, "hour": round(hour, 4),
                                               "value": _clean(value)})
    series = [{"source_scope": scope, "points": by_scope[scope]}
              for scope in contract.SOURCE_SCOPES if scope in by_scope]
    result = {"metric": metric, "unit": contract.unit_for(metric), "date": date, "series": series,
              "missing_values": contract.MISSING_VALUE_CONVENTION, "time": contract.TIME_CONVENTION}
    if not series:
        result["reason"] = "no samples of this metric on that local day"
    return result


def _latest_sample_day(conn, metric, offsets: ClockOffsets, scopes: tuple[str, ...]) -> str | None:
    """Local date of the newest stored sample of ``metric`` within ``scopes``."""
    for ts_text, scope in conn.execute(
            "SELECT ts_utc, source_scope FROM metric_samples WHERE metric=? ORDER BY ts_utc DESC",
            (metric,)):
        if scope in scopes:
            return offsets.local_date(parse_iso_utc(ts_text))
    return None


def metric_series(conn: sqlite.Connection, metrics: list[str], days: int = 90,
                  source_scope: str | None = None, end_date: str | None = None) -> dict:
    """Series for one or more contract metrics ending on ``end_date`` (default today).

    Daily metrics come back as ``{date, value}`` per source scope; sample
    metrics as per-day ``{date, min, mean, max, samples}``. Unknown metric
    names are listed under ``ignored_metrics`` rather than raising, so one typo
    does not void a multi-metric request. Days without data have no point.
    """
    if source_scope is not None and source_scope not in contract.SOURCE_SCOPES:
        raise ValueError(f"source_scope must be one of {contract.SOURCE_SCOPES}")
    scopes = (source_scope,) if source_scope else contract.SOURCE_SCOPES
    offsets = ClockOffsets.load(conn)
    series: list[dict] = []
    ignored: list[str] = []
    label_names = set(contract.label_names())
    for name in dict.fromkeys(metrics):
        cadence = contract.cadence_for(name)
        if cadence is None:
            if name not in label_names:
                ignored.append(name)
            continue
        if cadence == contract.CADENCE_DAILY:
            start, end = _window(days, end_date, MAX_DAILY_DAYS)
            by_scope: dict[str, list[dict]] = {}
            for date, value, scope in conn.execute(
                    "SELECT date, value, source_scope FROM daily_metrics WHERE metric=? "
                    "AND date BETWEEN ? AND ? ORDER BY date", (name, start, end)):
                if scope in scopes:
                    by_scope.setdefault(scope, []).append({"date": date, "value": _clean(value)})
        else:
            start, end = _window(days, end_date, MAX_SAMPLE_DAYS)
            by_scope = sample_day_aggregates(conn, name, start, end, offsets, scopes)
        for scope in contract.SOURCE_SCOPES:
            if scope in by_scope:
                series.append({"metric": name, "unit": contract.unit_for(name), "cadence": cadence,
                               "source_scope": scope, "from": start, "to": end,
                               "points": by_scope[scope]})
        if not by_scope:
            series.append({"metric": name, "unit": contract.unit_for(name), "cadence": cadence,
                           "source_scope": None, "from": start, "to": end, "points": [],
                           "note": "no data in this window for any source scope"})
    labels = _labels_in_window(conn, metrics, days, end_date, scopes)
    return {"series": series, "labels": labels, "ignored_metrics": ignored,
            "missing_values": contract.MISSING_VALUE_CONVENTION, "time": contract.TIME_CONVENTION,
            "sources": contract.SOURCE_CONVENTION}


def _labels_in_window(conn, metrics, days, end_date, scopes) -> list[dict]:
    wanted = [m for m in metrics if m in contract.label_names()]
    if not wanted:
        return []
    start, end = _window(days, end_date, MAX_DAILY_DAYS)
    out = []
    for name in wanted:
        points = [{"date": d, "label": label, "source_scope": scope} for d, label, scope in conn.execute(
            "SELECT date, label, source_scope FROM daily_labels WHERE metric=? AND date BETWEEN ? AND ? "
            "ORDER BY date", (name, start, end)) if scope in scopes]
        out.append({"metric": name, "from": start, "to": end, "points": points})
    return out


_SLEEP_COLUMNS = (
    "date", "source_scope", "start_utc", "end_utc", "deep_s", "light_s", "rem_s", "awake_s",
    "unmeasurable_s", "overall_score", "quality_score", "duration_score", "recovery_score",
    "deep_score", "rem_score", "light_score", "awake_time_score", "awakenings_count_score",
    "combined_awake_score", "restlessness_score", "interruptions_score", "awakenings_count",
    "avg_stress", "avg_spo2", "lowest_spo2", "avg_hr", "avg_respiration", "lowest_respiration",
    "highest_respiration", "retro", "sleep_id",
)


def _hhmm(moment: str, offset_s: int) -> str:
    """``HH:MM`` of a UTC instant on a clock ``offset_s`` seconds ahead of UTC."""
    return (parse_iso_utc(moment) + datetime.timedelta(seconds=offset_s)).strftime("%H:%M")


def sleep_detail(conn: sqlite.Connection, date: str | None = None, local: bool = False) -> dict:
    """Every source's record of one night (default: the latest night stored), with stages.

    Durations are reported in minutes; a stage or score the source did not
    state is absent, never 0. Sessions from different scopes sit side by side.
    ``local`` (``data.sleep``) adds per session ``utc_offset_s`` (the stored offset nearest the
    session's end, its start when it has no end) and per stage ``start_local``/``end_local`` as
    ``HH:MM``, each instant under the offset nearest to it (a stage across a clock change shows the
    wall-clock times the watch showed); all of it is absent while no offset is stored.
    """
    if date is not None:
        parse_day(date, "date")
    if date is None:
        row = conn.execute("SELECT MAX(date) FROM sleep_sessions").fetchone()
        date = row[0] if row else None
        if date is None:
            return {"date": None, "sessions": [], "reason": "no sleep stored yet",
                    "missing_values": contract.MISSING_VALUE_CONVENTION}
    offsets = ClockOffsets.load(conn) if local else None
    sessions = []
    for row in conn.execute(
            f"SELECT {', '.join(_SLEEP_COLUMNS)} FROM sleep_sessions WHERE date=? ORDER BY source_scope",
            (date,)):
        record = dict(zip(_SLEEP_COLUMNS, row))
        sleep_id = record.pop("sleep_id")
        entry = {key: _clean(value) for key, value in record.items()
                 if value is not None and not key.endswith("_s")}
        entry["stage_minutes"] = {key[:-2]: round(record[key] / 60, 1) for key in
                                  ("deep_s", "light_s", "rem_s", "awake_s", "unmeasurable_s")
                                  if record[key] is not None}
        entry["retro"] = bool(record["retro"])
        stages = [{"stage": stage, "start_utc": start, "end_utc": end,
                   "minutes": round((parse_iso_utc(end) - parse_iso_utc(start)).total_seconds() / 60, 1)}
                  for stage, start, end in conn.execute(
                      "SELECT stage, start_utc, end_utc FROM sleep_stages WHERE sleep_id=? ORDER BY start_utc",
                      (sleep_id,))]
        offset = None
        anchor = record["end_utc"] or record["start_utc"]
        if offsets is not None and anchor:
            offset = offsets.offset_at(parse_iso_utc(anchor))
        if offset is not None:
            entry["utc_offset_s"] = offset
            for stage in stages:
                for key in ("start", "end"):
                    moment = stage[f"{key}_utc"]
                    stage[f"{key}_local"] = _hhmm(moment, offsets.offset_at(parse_iso_utc(moment)))
        if stages:
            entry["stages"] = stages
        sessions.append(entry)
    result = {"date": date, "sessions": sessions, "units": {"stage_minutes": "min", "avg_hr": "bpm",
                                                             "avg_spo2": "%", "scores": "0-100"},
              "missing_values": contract.MISSING_VALUE_CONVENTION, "time": contract.TIME_CONVENTION}
    if not sessions:
        result["reason"] = "no record of that night from any source"
    return result


def list_activities(conn: sqlite.Connection, limit: int = 20) -> dict:
    """Most recent activities (sessions) newest first. No route or GPS data is stored."""
    limit = max(1, min(int(limit), 200))
    columns = ("start_utc", "end_utc", "sport", "sub_sport", "total_timer_s", "total_elapsed_s",
               "distance_m", "calories_kcal", "avg_hr", "max_hr", "avg_speed_mps", "total_ascent_m",
               "total_descent_m", "source_scope")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM activities ORDER BY start_utc DESC LIMIT ?", (limit,))
    activities = [{k: _clean(v) for k, v in zip(columns, row) if v is not None} for row in rows]
    return {"activities": activities,
            "units": {"distance_m": "m", "total_timer_s": "s", "total_elapsed_s": "s",
                      "calories_kcal": "kcal", "avg_hr": "bpm", "max_hr": "bpm", "avg_speed_mps": "m/s",
                      "total_ascent_m": "m", "total_descent_m": "m"},
            "missing_values": contract.MISSING_VALUE_CONVENTION, "time": contract.TIME_CONVENTION}


def _numeric_scope(metric: str, scope: str) -> str:
    """Validate a (numeric metric, scope) pair; return the metric's cadence. Raises ValueError."""
    cadence = contract.cadence_for(metric)
    if cadence is None:
        raise ValueError(f"unknown numeric metric {metric!r}; see the contract for the list")
    if scope not in contract.SOURCE_SCOPES:
        raise ValueError(f"scope must be one of {contract.SOURCE_SCOPES}")
    return cadence


def _daily_values(conn: sqlite.Connection, metric: str, scope: str, first_day: str,
                  last_day: str) -> dict[str, float]:
    """Stored value per local day for one daily metric and scope."""
    return {date: _clean(value) for date, value in conn.execute(
        "SELECT date, value FROM daily_metrics WHERE metric=? AND source_scope=? AND date BETWEEN ? AND ?",
        (metric, scope, first_day, last_day))}


def _calendar_values(conn: sqlite.Connection, metric: str, scope: str, cadence: str, first_day: str,
                     last_day: str) -> dict[str, float]:
    """Value per local day: the stored daily value, or the mean of that day's samples."""
    if cadence == contract.CADENCE_DAILY:
        return _daily_values(conn, metric, scope, first_day, last_day)
    aggregates = sample_day_aggregates(conn, metric, first_day, last_day, ClockOffsets.load(conn), (scope,))
    return {point["date"]: point["mean"] for point in aggregates.get(scope, [])}


def metric_calendar(conn: sqlite.Connection, metric: str, scope: str, first_day: str,
                    last_day: str) -> list[dict]:
    """Every local day from ``first_day`` to ``last_day`` (inclusive, oldest first) for one metric.

    Each entry is ``{"day", "value", "status", "completeness"}``: ``value`` is None where the store
    has none (never zero) and ``status`` says why, from the coverage ledger. Sample metrics carry the
    day's mean; ``completeness`` (0-100) says how much of the day it rests on for a per-minute metric,
    None otherwise; both from one coverage pass (``coverage.calendar``).
    Raises ValueError for an unknown metric or scope, a malformed date, or a span beyond the
    series caps.
    """
    cadence = _numeric_scope(metric, scope)
    first, last = datetime.date.fromisoformat(first_day), datetime.date.fromisoformat(last_day)
    cap = MAX_DAILY_DAYS if cadence == contract.CADENCE_DAILY else MAX_SAMPLE_DAYS
    span = (last - first).days + 1
    if not 1 <= span <= cap:
        raise ValueError(f"the day range must cover 1 to {cap} days for {metric}")
    values = _calendar_values(conn, metric, scope, cadence, first_day, last_day)
    return [{"day": day, "value": values.get(day), "status": status, "completeness": share}
            for day, status, share in coverage.calendar(conn, metric, scope, first_day, last_day)]


#: The live link's source scope and its retained stream (``contract.SESSION_STREAMS_FOR``).
LIVE_SCOPE = "live"


def _local_minute(moment: datetime.datetime, offsets: ClockOffsets) -> str:
    """``YYYY-MM-DDTHH:MM`` on the watch's clock at ``moment`` (UTC assumed where no offset is known)."""
    local = moment.astimezone(datetime.timezone.utc) + datetime.timedelta(seconds=offsets.offset_at(moment) or 0)
    return local.strftime("%Y-%m-%dT%H:%M")


def live_day(conn: sqlite.Connection, day: str) -> dict:
    """The live link on one local day: its sessions and, per folded metric, the minutes and their median.

    A session is a run of ``json:live`` records whose spans overlap or touch (a partial file stored
    before the full one is one session), kept when its start or end falls on ``day`` on the watch's
    clock; ``minutes`` counts the distinct folded minutes inside it. ``metrics`` lists every metric
    the fold keeps, in contract order: the minutes whose local day is ``day`` and their lower median
    (null when none). Raises ValueError for a day that is not ``YYYY-MM-DD``.
    """
    parse_day(day, "day")
    offsets = ClockOffsets.load(conn)
    streams = tuple(dict.fromkeys(stream for (_, scope), names in contract.SESSION_STREAMS_FOR.items()
                                  if scope == LIVE_SCOPE for stream in names))
    sessions: list[list[datetime.datetime]] = []
    for start, end in conn.execute(
            f"SELECT start_utc, end_utc FROM raw_records WHERE stream IN ({','.join('?' * len(streams))}) "
            "AND start_utc IS NOT NULL AND end_utc IS NOT NULL ORDER BY start_utc, end_utc", streams):
        first, last = parse_iso_utc(start), parse_iso_utc(end)
        if offsets.local_date(first) != day and offsets.local_date(last) != day:
            continue
        if sessions and first <= sessions[-1][1]:
            sessions[-1][1] = max(sessions[-1][1], last)
        else:
            sessions.append([first, last])
    out_sessions = []
    for first, last in sessions:
        lo, hi = first.strftime("%Y-%m-%dT%H:%M:%SZ"), last.strftime("%Y-%m-%dT%H:%M:%SZ")
        minutes = conn.execute("SELECT count(DISTINCT ts_utc) FROM metric_samples WHERE source_scope=? "
                               "AND ts_utc BETWEEN ? AND ?", (LIVE_SCOPE, lo, hi)).fetchone()[0]
        out_sessions.append({"start_utc": lo, "end_utc": hi, "start_local": _local_minute(first, offsets),
                             "end_local": _local_minute(last, offsets), "minutes": minutes})
    # local days can begin up to 14 h before/after their UTC namesake; over-fetch and filter
    date = datetime.date.fromisoformat(day)
    fetch_lo = (date - datetime.timedelta(days=1)).isoformat()
    fetch_hi = (date + datetime.timedelta(days=2)).isoformat()
    metrics = []
    for item in contract.METRICS:
        if (item.metric, LIVE_SCOPE) not in contract.SESSION_STREAMS_FOR:
            continue
        values = [value for ts_text, value in conn.execute(
            "SELECT ts_utc, value FROM metric_samples WHERE metric=? AND source_scope=? "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (item.metric, LIVE_SCOPE, fetch_lo, fetch_hi))
            if offsets.local_date(parse_iso_utc(ts_text)) == day]
        median = _clean(sorted(values)[(len(values) - 1) // 2]) if values else None
        metrics.append({"metric": item.metric, "unit": item.unit, "minutes": len(values), "median": median})
    return {"day": day, "sessions": out_sessions, "metrics": metrics}


def latest_value(conn: sqlite.Connection, metric: str, scope: str) -> tuple[str, float] | None:
    """The newest ``(local day, value)`` stored for one numeric metric and scope, or None.

    Sample metrics report the mean of their newest local day. Raises ValueError for an unknown
    metric or scope.
    """
    cadence = _numeric_scope(metric, scope)
    if cadence == contract.CADENCE_DAILY:
        row = conn.execute("SELECT date, value FROM daily_metrics WHERE metric=? AND source_scope=? "
                           "ORDER BY date DESC LIMIT 1", (metric, scope)).fetchone()
        return (row[0], _clean(row[1])) if row else None
    offsets = ClockOffsets.load(conn)
    day = _latest_sample_day(conn, metric, offsets, (scope,))
    if day is None:
        return None
    means = _calendar_values(conn, metric, scope, cadence, day, day)
    return (day, means[day]) if day in means else None
