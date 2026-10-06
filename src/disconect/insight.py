"""Deterministic facts: a recent window of one person's data against their own history.

This layer emits **facts, evidence and confidence** and never a sentence. The
CLI, MCP and any UI render or interpret them; an AI model may explain a fact
but not rewrite it, so the same store answers the same question the same way
everywhere.

Three hard rules, enforced here and pinned by tests:

1. **Compare only against the person's own history.** There are no population
   norms in this project, and none are planned; "compared with healthy adults"
   has no local basis and never appears.
2. **Thin evidence says so.** Below ``MIN_BASELINE_DAYS`` of baseline data a
   fact carries ``confidence = "insufficient"`` and no comparison, instead of
   a number computed from three points.
3. **No diagnosis, treatment or risk prediction.** A fact states "7-day mean
   resting heart rate is 4 bpm above your 28-day baseline (z = 1.8)". Whether
   "higher" is good or bad is left to the reader: a lower pace is faster, a
   lower resting heart rate is usually better, and the engine does not know
   which reading the user wants.

Baseline rules are named constants because they decide conclusions; tests pin
the numbers so a change is deliberate.
"""

from __future__ import annotations

import dataclasses
import datetime
import statistics

from disconect import contract, queries
from disconect.ingest.clock import ClockOffsets
from disconect.storage import sqlite

DEFAULT_WINDOW_DAYS = 7
DEFAULT_BASELINE_DAYS = 28
MAX_WINDOW_DAYS = 31
MAX_BASELINE_DAYS = 365
#: Fewer baseline days than this: no comparison, confidence "insufficient".
MIN_BASELINE_DAYS = 3
#: Confidence from how many baseline days actually had data (not calendar days).
CONFIDENCE_BANDS: tuple[tuple[int, str], ...] = ((8, "high"), (5, "medium"), (3, "low"))
INSUFFICIENT = "insufficient"

RULES = (
    "Comparisons are against this person's own earlier data only; no population norms exist here.",
    "With fewer than three baseline days a fact has no comparison and confidence 'insufficient'.",
    "Facts describe direction and size; they do not diagnose, and 'higher' is not 'better'.",
)


def confidence_for(baseline_days_with_data: int) -> str:
    """Confidence band from the number of baseline days that had data."""
    for threshold, label in CONFIDENCE_BANDS:
        if baseline_days_with_data >= threshold:
            return label
    return INSUFFICIENT


@dataclasses.dataclass(frozen=True)
class Comparison:
    baseline_mean: float
    baseline_sd: float | None
    delta: float
    delta_percent: float | None
    z_score: float | None
    direction: str  # higher | lower | unchanged


@dataclasses.dataclass(frozen=True)
class Fact:
    """One metric in one window for one source scope, with its evidence."""

    fact_id: str
    metric: str
    unit: str
    cadence: str
    source_scope: str
    value: float | None
    window_min: float | None
    window_max: float | None
    window_days_with_data: int
    baseline_days_with_data: int
    comparison: Comparison | None
    confidence: str
    reason_code: str  # ok | no_data_in_window | baseline_too_thin | no_baseline | no_data
    evidence: dict

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        return data


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _latest_stored_date(conn: sqlite.Connection) -> str | None:
    daily = conn.execute("SELECT MAX(date) FROM daily_metrics").fetchone()[0]
    # a live-link session today must not move every device fact's window (its rows are scope 'live')
    sample = conn.execute("SELECT MAX(substr(ts_utc, 1, 10)) FROM metric_samples WHERE source_scope != 'live'").fetchone()[0]
    candidates = [d for d in (daily, sample) if d]
    return max(candidates) if candidates else None


def _daily_points(conn, metric, start, end, scopes) -> dict[str, list[dict]]:
    by_scope: dict[str, list[dict]] = {}
    for date, value, scope in conn.execute(
            "SELECT date, value, source_scope FROM daily_metrics WHERE metric=? AND date BETWEEN ? AND ? "
            "ORDER BY date", (metric, start, end)):
        if scope in scopes:
            by_scope.setdefault(scope, []).append({"date": date, "value": value})
    return by_scope


def _build_fact(metric: contract.MetricContract, scope: str, window: list[dict], baseline: list[dict],
                bounds: dict, include_points: bool) -> Fact:
    window_values = [p["value"] for p in window]
    baseline_values = [p["value"] for p in baseline]
    evidence = {
        "window_dates": [p["date"] for p in window],
        "baseline_from": bounds["baseline_from"], "baseline_to": bounds["baseline_to"],
        "baseline_days_with_data": len(baseline_values),
        "aggregation": ("mean of the window's daily values" if metric.cadence == contract.CADENCE_DAILY
                        else "mean of per-day means of samples"),
    }
    if include_points:
        evidence["window_points"] = [{"date": p["date"], "value": _round(p["value"])} for p in window]
    base = dict(fact_id=f"period.{metric.metric}", metric=metric.metric, unit=metric.unit,
                cadence=metric.cadence, source_scope=scope, window_days_with_data=len(window_values),
                baseline_days_with_data=len(baseline_values), evidence=evidence)
    if not window_values:
        return Fact(value=None, window_min=None, window_max=None, comparison=None,
                    confidence=INSUFFICIENT, reason_code="no_data_in_window", **base)
    value = statistics.fmean(window_values)
    if len(baseline_values) < MIN_BASELINE_DAYS:
        reason = "no_baseline" if not baseline_values else "baseline_too_thin"
        return Fact(value=_round(value), window_min=_round(min(window_values)),
                    window_max=_round(max(window_values)), comparison=None, confidence=INSUFFICIENT,
                    reason_code=reason, **base)
    baseline_mean = statistics.fmean(baseline_values)
    baseline_sd = statistics.stdev(baseline_values) if len(baseline_values) > 1 else None
    delta = value - baseline_mean
    comparison = Comparison(
        baseline_mean=_round(baseline_mean), baseline_sd=_round(baseline_sd), delta=_round(delta),
        delta_percent=_round(delta / baseline_mean * 100) if baseline_mean else None,
        z_score=_round(delta / baseline_sd) if baseline_sd else None,
        direction="unchanged" if abs(delta) < 1e-9 else ("higher" if delta > 0 else "lower"))
    return Fact(value=_round(value), window_min=_round(min(window_values)),
                window_max=_round(max(window_values)), comparison=comparison,
                confidence=confidence_for(len(baseline_values)), reason_code="ok", **base)


def period_facts(conn: sqlite.Connection, window_days: int = DEFAULT_WINDOW_DAYS,
                 baseline_days: int = DEFAULT_BASELINE_DAYS, end_date: str | None = None,
                 metrics: list[str] | None = None, source_scope: str | None = None,
                 include_points: bool = False) -> dict:
    """Facts for every metric with data: the last ``window_days`` vs the ``baseline_days`` before.

    ``end_date`` defaults to the latest date the store holds (stated as ``as_of``),
    so an old archive still answers. Metrics without any data in either range
    are omitted unless explicitly requested, in which case they appear with
    ``reason_code = "no_data"``. Unknown metric names land in ``ignored_metrics``.
    Evidence lists the window's dates; ``include_points`` adds their values. With no
    ``source_scope`` the session scope ``live`` is left out; name it to get its facts.
    """
    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    baseline_days = max(1, min(int(baseline_days), MAX_BASELINE_DAYS))
    if source_scope is not None and source_scope not in contract.SOURCE_SCOPES:
        raise ValueError(f"source_scope must be one of {contract.SOURCE_SCOPES}")
    # a session scope (live) answers only when asked for: a link's minutes never shape the default facts (9b-2)
    scopes = (source_scope,) if source_scope else contract.DEFAULT_FACT_SCOPES
    if end_date:
        queries.parse_day(end_date, "end_date")
    as_of = end_date or _latest_stored_date(conn)
    if as_of is None:
        return {"as_of": None, "facts": [], "ignored_metrics": list(metrics or []),
                "reason": "nothing stored yet", "rules": list(RULES)}
    end = datetime.date.fromisoformat(as_of)
    window_start = end - datetime.timedelta(days=window_days - 1)
    baseline_end = window_start - datetime.timedelta(days=1)
    baseline_start = baseline_end - datetime.timedelta(days=baseline_days - 1)
    bounds = {"window_from": window_start.isoformat(), "window_to": as_of,
              "baseline_from": baseline_start.isoformat(), "baseline_to": baseline_end.isoformat()}

    wanted = list(dict.fromkeys(metrics)) if metrics else contract.metric_names()
    known = {item.metric: item for item in contract.METRICS}
    ignored = [name for name in wanted if name not in known]
    offsets = ClockOffsets.load(conn)
    facts: list[Fact] = []
    for name in wanted:
        item = known.get(name)
        if item is None:
            continue
        if item.cadence == contract.CADENCE_DAILY:
            by_scope = _daily_points(conn, name, bounds["baseline_from"], as_of, scopes)
        else:
            aggregates = queries.sample_day_aggregates(conn, name, bounds["baseline_from"], as_of,
                                                       offsets, scopes)
            by_scope = {scope: [{"date": p["date"], "value": p["mean"]} for p in points]
                        for scope, points in aggregates.items()}
        if not by_scope:
            if metrics:
                facts.append(Fact(fact_id=f"period.{name}", metric=name, unit=item.unit,
                                  cadence=item.cadence, source_scope=source_scope or "any", value=None,
                                  window_min=None, window_max=None, window_days_with_data=0,
                                  baseline_days_with_data=0, comparison=None, confidence=INSUFFICIENT,
                                  reason_code="no_data", evidence={"baseline_from": bounds["baseline_from"],
                                                                   "baseline_to": bounds["baseline_to"]}))
            continue
        for scope in contract.SOURCE_SCOPES:
            points = by_scope.get(scope)
            if not points:
                continue
            window = [p for p in points if p["date"] >= bounds["window_from"]]
            baseline = [p for p in points if p["date"] <= bounds["baseline_to"]]
            facts.append(_build_fact(item, scope, window, baseline, bounds, include_points))
    return {
        "as_of": as_of, "window": {"from": bounds["window_from"], "to": bounds["window_to"],
                                   "days": window_days},
        "baseline": {"from": bounds["baseline_from"], "to": bounds["baseline_to"], "days": baseline_days,
                     "min_days_with_data": MIN_BASELINE_DAYS, "confidence_bands": dict(
                         (label, f">= {threshold} baseline days") for threshold, label in CONFIDENCE_BANDS)},
        "facts": [fact.as_dict() for fact in facts],
        "ignored_metrics": ignored, "rules": list(RULES),
        "missing_values": contract.MISSING_VALUE_CONVENTION, "sources": contract.SOURCE_CONVENTION,
    }
