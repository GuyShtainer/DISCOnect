"""Data health: what the store holds, per stream and per metric, and what went wrong.

The question this answers for a person or a model is: when a query comes back
empty, is that because nothing was ever imported for that period, because the
files for it failed to decode, or because the watch genuinely recorded nothing?
Flattening those into one number would make the question unanswerable.
"""

from __future__ import annotations

import datetime

from disconect import contract, coverage, queries
from disconect.ingest.clock import ClockOffsets
from disconect.redact import redact_text
from disconect.storage import migrations
from disconect.storage._time import now_utc
from disconect.storage import parse_iso_utc, sqlite

UTC = datetime.timezone.utc


# The "last imports" list (`recent_imports` here, `import.last` in serve): the newest LAST_IMPORTS runs of
# every transport but `ble`, plus the newest `ble` run, newest first. A live link ends with a sweep of the
# readings folder (transport `ble`, usually "0 imported, N duplicate"); listed like any run, the sweeps
# push the USB and export runs out of the list within a day.
LAST_IMPORTS = 5
RUN_COLUMNS = ("id", "started_at", "finished_at", "transport", "status", "files_seen", "files_imported",
               "files_duplicate", "files_failed", "records_written", "error")
RECENT_RUNS_SQL = (
    f"SELECT {', '.join(RUN_COLUMNS)} FROM import_runs WHERE id IN ("
    "SELECT id FROM (SELECT id FROM import_runs WHERE transport IS NOT 'ble' ORDER BY id DESC LIMIT ?) "
    "UNION SELECT id FROM (SELECT id FROM import_runs WHERE transport = 'ble' ORDER BY id DESC LIMIT 1)"
    ") ORDER BY id DESC"
)


def _rows(conn: sqlite.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _window_start(window_days: int) -> str:
    return (now_utc() - datetime.timedelta(days=window_days)).date().isoformat()


def source_agreement(conn: sqlite.Connection) -> list[dict]:
    """Where two sources state the same daily figure on the same date, how closely they agree.

    Same metric id in two scopes, plus the pairs in ``contract.COMPARISON_TARGETS`` (our id
    against the vendor's id for the same quantity; ``compared_with`` names the vendor id).

    This is the decoder's running self-check: Garmin's cloud figures (vendor_cloud)
    are the oracle for what the watch's own bytes (device / local) should decode
    to. A match is a difference within 0.5 or 0.5 % of the vendor value, whichever
    is larger. Reported per metric and scope pair; no per-day values leave here.
    """
    pairs: dict[tuple[str, str | None, str, str], list[tuple[float, float]]] = {}
    for metric, scope_a, scope_b, value_a, value_b in conn.execute(
            "SELECT a.metric, a.source_scope, b.source_scope, a.value, b.value FROM daily_metrics a "
            "JOIN daily_metrics b ON b.date = a.date AND b.metric = a.metric "
            "AND a.source_scope < b.source_scope ORDER BY a.date, a.metric, a.source_scope, b.source_scope"):
        pairs.setdefault((metric, None, scope_a, scope_b), []).append((value_a, value_b))
    for (ours, our_scope), (theirs, their_scope) in contract.COMPARISON_TARGETS.items():
        rows = conn.execute(
            "SELECT a.value, b.value FROM daily_metrics a JOIN daily_metrics b ON b.date = a.date "
            "WHERE a.metric=? AND a.source_scope=? AND b.metric=? AND b.source_scope=?",
            (ours, our_scope, theirs, their_scope)).fetchall()
        if rows:
            pairs[(ours, theirs, our_scope, their_scope)] = [(a, b) for a, b in rows]
    report = []
    for (metric, compared_with, scope_a, scope_b), values in sorted(pairs.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2:])):
        diffs = [a - b for a, b in values]
        matching = sum(1 for (a, b), d in zip(values, diffs) if abs(d) <= max(0.5, 0.005 * abs(b)))
        absolute = sorted(abs(d) for d in diffs)
        report.append({
            "metric": metric, "compared_with": compared_with, "unit": contract.unit_for(metric),
            "scope_a": scope_a, "scope_b": scope_b,
            "days_compared": len(values), "days_matching": matching,
            "median_abs_diff": round(absolute[len(absolute) // 2], 3),
            "max_abs_diff": round(absolute[-1], 3),
            "mean_diff_a_minus_b": round(sum(diffs) / len(diffs), 3),
        })
    return report


def _live_day(offsets: ClockOffsets, stamp: str | None) -> str | None:
    """The watch-local day of a stored live stamp; None stays None; a day the calendar cannot hold (year 9999
    plus a positive offset) keeps the UTC prefix, like the Rust core's ``live_day``."""
    if stamp is None:
        return None
    try:
        return offsets.local_date(parse_iso_utc(stamp))
    except OverflowError:
        return stamp[:10]


def data_health(conn: sqlite.Connection, window_days: int = 30) -> dict:
    """Coverage, provenance and recent imports as plain data. PII-free."""
    window_days = max(1, min(int(window_days), 3650))
    since = _window_start(window_days)
    today = now_utc().date().isoformat()

    raw = {row[0]: {"records": row[1], "first": row[2], "last": row[3]}
           for row in conn.execute(
               "SELECT stream, COUNT(*), MIN(start_utc), MAX(end_utc) FROM raw_records GROUP BY stream")}

    provenance = _rows(conn, "SELECT * FROM stream_provenance ORDER BY stream")
    for row in provenance:
        for key in ("last_parse_error_message", "last_write_error_message"):
            row[key] = redact_text(row[key])

    # sample counts keep FIT's "day with data" meaning: a live-link session (scope 'live') is not a day of monitoring
    samples_total = {row[0]: {"days_with_data": row[1], "first_day": row[2], "last_day": row[3]}
                     for row in conn.execute(
                         "SELECT metric, COUNT(DISTINCT substr(ts_utc,1,10)), MIN(substr(ts_utc,1,10)), "
                         "MAX(substr(ts_utc,1,10)) FROM metric_samples WHERE source_scope != 'live' GROUP BY metric")}
    samples_window = {row[0]: row[1] for row in conn.execute(
        "SELECT metric, COUNT(DISTINCT substr(ts_utc,1,10)) FROM metric_samples "
        "WHERE source_scope != 'live' AND substr(ts_utc,1,10) >= ? GROUP BY metric", (since,))}
    live_records = conn.execute("SELECT COUNT(*) FROM raw_records WHERE stream='json:live'").fetchone()[0]
    live_samples, live_first, live_last = conn.execute(
        "SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM metric_samples WHERE source_scope = 'live'").fetchone()
    # the live block's days are the watch's local days, like data.live's; every other day in this report
    # (samples_total, streams, coverage) is the UTC prefix of the stored stamp
    known_offsets = ClockOffsets.load(conn)  # once: the live block, local_today and the coverage ledger share it
    live = {"records": live_records, "samples": live_samples,
            "first_day": _live_day(known_offsets, live_first), "last_day": _live_day(known_offsets, live_last)}

    daily_total = {}
    for metric, scope, days, first, last in conn.execute(
            "SELECT metric, source_scope, COUNT(*), MIN(date), MAX(date) FROM daily_metrics "
            "GROUP BY metric, source_scope"):
        daily_total.setdefault(metric, {})[scope] = {"days_with_data": days, "first_day": first,
                                                     "last_day": last}
    daily_window = {}
    for metric, scope, days in conn.execute(
            "SELECT metric, source_scope, COUNT(*) FROM daily_metrics WHERE date >= ? "
            "GROUP BY metric, source_scope", (since,)):
        daily_window.setdefault(metric, {})[scope] = days

    sleep = {scope: {"nights": nights, "first_day": first, "last_day": last}
             for scope, nights, first, last in conn.execute(
                 "SELECT source_scope, COUNT(*), MIN(date), MAX(date) FROM sleep_sessions GROUP BY source_scope")}
    activities = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    offsets = conn.execute("SELECT COUNT(*) FROM clock_offsets").fetchone()[0]

    runs = _rows(conn, RECENT_RUNS_SQL, (LAST_IMPORTS,))
    for run in runs:
        run["error"] = redact_text(run["error"])

    metrics = []
    for item in contract.METRICS:
        entry = {"metric": item.metric, "unit": item.unit, "cadence": item.cadence}
        if item.cadence == contract.CADENCE_SAMPLE:
            entry["total"] = samples_total.get(item.metric)
            entry["days_in_window"] = samples_window.get(item.metric, 0)
        else:
            entry["total"] = daily_total.get(item.metric)
            entry["days_in_window"] = daily_window.get(item.metric, {})
        metrics.append(entry)

    return {
        "contract_version": contract.CONTRACT_VERSION,
        "coverage": coverage.ledger(conn, queries.local_today(conn, known_offsets), window_days, known_offsets),
        "schema_version": migrations.current_version(conn),
        "window": {"days": window_days, "from": since, "to": today},
        "never_imported": not raw,
        "streams": raw,
        "provenance": provenance,
        "metrics": metrics,
        "sleep": sleep,
        "live": live,
        "activities": activities,
        "clock_offsets_known": offsets,
        "source_agreement": source_agreement(conn),
        "recent_imports": runs,
        "conventions": {
            "time": contract.TIME_CONVENTION,
            "missing_values": contract.MISSING_VALUE_CONVENTION,
            "sources": contract.SOURCE_CONVENTION,
            "coverage": contract.COVERAGE_CONVENTION,
            "completeness": contract.COMPLETENESS_CONVENTION,
        },
    }


def summarize_for_humans(health: dict) -> str:
    """A short plain-text rendering of :func:`data_health` for the CLI."""
    lines = []
    if health["never_imported"]:
        return "Nothing imported yet. Run: disconect import <export-zip-or-folder>"
    lines.append(f"schema v{health['schema_version']}, contract v{health['contract_version']}")
    lines.append("streams:")
    for stream, info in sorted(health["streams"].items()):
        span = f"{info['first'] or '?'} .. {info['last'] or '?'}"
        lines.append(f"  {stream:18s} {info['records']:6d} raw records   {span}")
    failed = [p for p in health["provenance"] if p["files_failed"]]
    if failed:
        lines.append("failures:")
        for p in failed:
            lines.append(f"  {p['stream']}: {p['files_failed']} file(s) failed "
                         f"({p['last_parse_error_kind'] or p['last_write_error_kind']})")
    lines.append(f"metrics with data (all time), window = last {health['window']['days']} days:")
    for entry in health["metrics"]:
        total = entry["total"]
        if not total:
            continue
        if entry["cadence"] == "sample":
            lines.append(f"  {entry['metric']:32s} {total['days_with_data']:5d} days  "
                         f"{total['first_day']} .. {total['last_day']}   window: {entry['days_in_window']}")
        else:
            for scope, info in total.items():
                window = entry["days_in_window"].get(scope, 0)
                lines.append(f"  {entry['metric']:32s} {info['days_with_data']:5d} days  "
                             f"{info['first_day']} .. {info['last_day']}   [{scope}] window: {window}")
    for scope, info in health["sleep"].items():
        lines.append(f"sleep [{scope}]: {info['nights']} nights  {info['first_day']} .. {info['last_day']}")
    live = health.get("live")
    if live and live["records"]:
        lines.append(f"live link: {live['records']} session records, {live['samples']} minute samples [live]  "
                     f"{live['first_day'] or '?'} .. {live['last_day'] or '?'}")
    lines.append(f"activities: {health['activities']}   clock offsets known: {health['clock_offsets_known']}")
    if health.get("source_agreement"):
        lines.append("source agreement (same day, two sources; 'vs' names the vendor's id for our figure):")
        for row in health["source_agreement"]:
            name = row["metric"] + (f" vs {row['compared_with']}" if row.get("compared_with") else "")
            lines.append(f"  {name:32s} {row['scope_a']} vs {row['scope_b']}: "
                         f"{row['days_matching']}/{row['days_compared']} days match, "
                         f"median |diff| {row['median_abs_diff']}, max {row['max_abs_diff']}")
    lines.extend(_coverage_lines(health["coverage"]))
    if health["recent_imports"]:
        last = health["recent_imports"][0]
        lines.append(f"last import: {last['started_at']} {last['transport']} -> {last['status']} "
                     f"({last['files_imported']} imported, {last['files_duplicate']} duplicate, "
                     f"{last['files_failed']} failed)")
    return "\n".join(lines)


def _coverage_lines(ledger: dict) -> list[str]:
    """Why days are missing, one line per (metric, scope) that has data or failures in the window."""
    window = ledger["window"]
    lines = [f"coverage, last {window['days']} days ({window['from']}..{window['to']}): "
             "present / failed / source_empty / not_covered"]
    for row in ledger["ledger"]:
        if not (row["present"] or row["failed"] or row["source_empty"]):
            continue
        sparse = "  (sparse)" if row["sparse"] else ""
        shown = [g for g in row["gaps"] if g["status"] != coverage.NOT_COVERED]
        gaps = "".join(f"  {g['from']}..{g['to']} {g['status']}" for g in shown[-3:])
        more = "  ..." if row["gaps_truncated"] or len(shown) > 3 else ""
        lines.append(f"  {row['metric']:32s} [{row['source_scope']:12s}] {row['present']:4d} / {row['failed']:3d} / "
                     f"{row['source_empty']:3d} / {row['not_covered']:3d}{sparse}{gaps}{more}")
    if ledger["map_drift"]:
        drifted = ", ".join(f"{d['metric']}[{d['source_scope']}]<-{d['stream']}" for d in ledger["map_drift"])
        lines.append(f"  map drift (observed but undeclared): {drifted}")
    if ledger["unattributed_failures"] or ledger["undatable_files"]:
        lines.append(f"  failures not placeable on a day: {ledger['unattributed_failures']}; "
                     f"retained files without a date span: {ledger['undatable_files']}")
    if not ledger["refinements_available"]:
        lines.append(f"  note: {ledger['refinements']}")
    return lines
