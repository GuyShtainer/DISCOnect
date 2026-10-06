"""Live-link session files (``live-*.jsonl``) as one retained raw record each, and the fold (bet 9b-2).

A live file holds one JSON object per line: a reading ``{"t": unix seconds, "metric": str,
"value": int}`` or a status line (a ``status`` or ``stop`` key). The record keeps the readings
only; the decoder itself derives nothing. The fold (:func:`fold_records`) is a pure function of
every ``json:live`` record in the store, run by ``Writer.derive_live_samples`` after each import,
pull and reparse: readings of the five sample-cadence metrics become one ``metric_samples`` row
per UTC minute at source scope ``live``, sentinels dropped with the FIT decoder's rules, readings
de-duplicated across records by ``(t, metric, value)``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math

from disconect.ingest.model import Decoded

STREAM = "json:live"

#: The source scope of the folded rows (``contract.SOURCE_SCOPES``); raw rows keep ``device``.
SCOPE = "live"

#: Metrics the fold keeps, with the FIT decoder's sentinel rule (a reading that fails it is dropped
#: under the named reason, prefixed ``live_``). ``steps`` and unknown names are ignored by the fold.
SENTINEL_RULES: dict[str, tuple[str, object]] = {
    "heart_rate": ("heart_rate_zero", lambda v: v > 0),
    "stress": ("stress_sentinel", lambda v: v >= 0),
    "respiration_rate": ("respiration_sentinel", lambda v: v >= 0),
    "spo2": ("spo2_off_wrist_or_zero", lambda v: v > 0),
    "energy_reserve": ("energy_reserve_out_of_range", lambda v: 0 <= v <= 100),
}


#: Exclusive upper bound of a reading's ``t`` (unix seconds, 10000-01-01Z): a millisecond stamp falls outside.
T_LIMIT = 253402300800


def _is_time(value: object) -> bool:
    """A number (never a bool) in ``0 <= t < T_LIMIT``; also False for NaN."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < T_LIMIT


def _is_reading(t: object, metric: object, value: object) -> bool:
    """The shape of a reading: time in range, metric text, value an int (never a bool)."""
    return _is_time(t) and isinstance(metric, str) and isinstance(value, int) and not isinstance(value, bool)


def parse_live_file(data: bytes) -> list[list] | None:
    """The readings ``[[t, metric, value], ...]`` of a live file, or None when ``data`` is not one.

    Every non-empty line must be a JSON object that is a reading or a status/stop line; a
    file with no non-empty line is not a live file. A status-only file returns ``[]``.
    """
    try:
        lines = [line for line in data.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError:
        return None
    if not lines:
        return None
    readings: list[list] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        if {"t", "metric", "value"} <= obj.keys():
            if not _is_reading(obj["t"], obj["metric"], obj["value"]):
                return None
            readings.append([obj["t"], obj["metric"], obj["value"]])
        elif "status" not in obj and "stop" not in obj:
            return None
    return readings


def canonical_payload(readings: list[list]) -> tuple[dict, bytes]:
    """(record, bytes): the readings sorted by ``(t, metric, value)``, compact sorted-key JSON."""
    record = {"readings": sorted(readings, key=lambda r: (r[0], r[1], r[2]))}
    return record, json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_live_record(record: dict) -> tuple[str, Decoded] | None:
    """``{"readings": [...]}`` -> (source_key, Decoded) with the span and no canonical rows.

    Raises ValueError for a record with no readings array (an empty one included) or a reading
    that is not ``[t, metric, value]`` as ``parse_live_file`` takes it (the Rust core's ``is_reading``).
    """
    readings = record.get("readings") if isinstance(record, dict) else None
    if not isinstance(readings, list) or not readings:
        raise ValueError("live record without readings")
    for reading in readings:
        if not (isinstance(reading, list) and len(reading) == 3 and _is_reading(*reading)):
            raise ValueError("live record with a malformed reading")
    ordered, data = canonical_payload(readings)
    first, last = ordered["readings"][0][0], ordered["readings"][-1][0]
    decoded = Decoded(stream=STREAM, source_scope="device")
    decoded.start_utc = datetime.datetime.fromtimestamp(first, datetime.timezone.utc)
    decoded.end_utc = datetime.datetime.fromtimestamp(last, datetime.timezone.utc)
    return hashlib.sha256(data).hexdigest(), decoded


def minute_floor(t: float) -> int:
    """The UTC minute a reading belongs to, as unix seconds: ``floor(t) - floor(t) mod 60``."""
    whole = math.floor(t)
    return whole - whole % 60


def lower_median(values: list[int]) -> int:
    """The lower median: element ``(n - 1) // 2`` of the ascending order."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def fold_records(records: list[tuple[int, list[list]]]) -> tuple[list[tuple[str, str, int, int]], dict[str, int]]:
    """Thin the readings of every live record into one sample per metric and UTC minute.

    ``records`` is ``(raw_id, readings)`` in content order (``sources._raw_ids_by_stream``).
    Returns ``(rows, dropped)``: rows ``(metric, ts_utc, value, raw_id)`` sorted by (metric, ts_utc),
    where ``value`` is the lower median of the minute's valid readings and ``raw_id`` the record
    that contributed the minute's first reading in content order; ``dropped`` counts sentinel
    readings by reason. A reading is one ``(float(t), metric, value)`` triple however many records
    carry it (a partial file stored before the full one), so two overlapping records fold as one.
    """
    seen: set[tuple[float, str, int]] = set()
    minutes: dict[tuple[str, int], tuple[int, list[int]]] = {}
    dropped: dict[str, int] = {}
    for raw_id, readings in records:
        for t, metric, value in readings:
            rule = SENTINEL_RULES.get(metric)
            if rule is None:
                continue
            key = (float(t), metric, value)
            if key in seen:
                continue
            seen.add(key)
            reason, valid = rule
            if not valid(value):
                dropped[f"live_{reason}"] = dropped.get(f"live_{reason}", 0) + 1
                continue
            bucket = minutes.get((metric, minute_floor(t)))
            if bucket is None:
                minutes[(metric, minute_floor(t))] = (raw_id, [value])
            else:
                bucket[1].append(value)
    rows = []
    for (metric, minute), (raw_id, values) in sorted(minutes.items()):
        ts_utc = datetime.datetime.fromtimestamp(minute, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append((metric, ts_utc, lower_median(values), raw_id))
    return rows, dropped
