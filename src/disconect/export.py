"""Plain-file exports of the canonical tables, for spreadsheets and other tools.

The SQLite file is already the user's data in an open format; these helpers
exist so a day's numbers can be pasted into a spreadsheet without SQL. A cell
with no value is left empty, never written as 0.
"""

from __future__ import annotations

import csv
import io

from disconect import contract
from disconect.storage import sqlite


def _clean(value):
    """Integral floats print as integers, others rounded to 3 decimals; non-floats pass through."""
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 3)
    return value


def daily_long(conn: sqlite.Connection, metrics: list[str] | None = None, start: str | None = None,
               end: str | None = None, source_scope: str | None = None) -> list[dict]:
    """One row per (date, metric, source scope): the daily tables as plain records."""
    clauses, params = [], []
    if metrics:
        clauses.append(f"metric IN ({','.join('?' * len(metrics))})")
        params += list(metrics)
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if source_scope:
        clauses.append("source_scope = ?")
        params.append(source_scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = [{"date": date, "metric": metric, "unit": contract.unit_for(metric), "source_scope": scope,
             "value": _clean(value)}
            for date, metric, scope, value in conn.execute(
                f"SELECT date, metric, source_scope, value FROM daily_metrics {where} "
                "ORDER BY date, metric, source_scope", params)]
    rows += [{"date": date, "metric": metric, "unit": "label", "source_scope": scope, "value": label}
             for date, metric, scope, label in conn.execute(
                 f"SELECT date, metric, source_scope, label FROM daily_labels {where} "
                 "ORDER BY date, metric, source_scope", params)]
    rows.sort(key=lambda r: (r["date"], r["metric"], r["source_scope"]))
    return rows


def daily_wide_csv(conn: sqlite.Connection, **filters) -> str:
    """CSV with one row per date and one column per ``metric[source_scope]``; blanks are missing."""
    rows = daily_long(conn, **filters)
    columns = sorted({f"{r['metric']}[{r['source_scope']}]" for r in rows})
    by_date: dict[str, dict[str, object]] = {}
    for row in rows:
        by_date.setdefault(row["date"], {})[f"{row['metric']}[{row['source_scope']}]"] = row["value"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", *columns])
    for date in sorted(by_date):
        writer.writerow([date, *[by_date[date].get(column, "") for column in columns]])
    return buffer.getvalue()


def daily_long_csv(conn: sqlite.Connection, **filters) -> str:
    """CSV of :func:`daily_long`: date, metric, unit, source_scope, value."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["date", "metric", "unit", "source_scope", "value"])
    writer.writeheader()
    writer.writerows(daily_long(conn, **filters))
    return buffer.getvalue()


def samples_csv(conn: sqlite.Connection, metric: str, start: str | None = None,
                end: str | None = None) -> str:
    """CSV of raw samples for one sample-cadence metric: ts_utc, value, source_scope."""
    if contract.cadence_for(metric) != contract.CADENCE_SAMPLE:
        raise ValueError(f"{metric!r} is not a sample-cadence metric")
    clauses, params = ["metric = ?"], [metric]
    if start:
        clauses.append("ts_utc >= ?")
        params.append(start)
    if end:
        clauses.append("ts_utc < ?")
        params.append(end)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ts_utc", "value", "source_scope", "unit"])
    unit = contract.unit_for(metric)
    for ts_utc, value, scope in conn.execute(
            f"SELECT ts_utc, value, source_scope FROM metric_samples WHERE {' AND '.join(clauses)} "
            "ORDER BY ts_utc", params):
        writer.writerow([ts_utc, _clean(value), scope, unit])
    return buffer.getvalue()
