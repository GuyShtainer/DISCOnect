"""Live-link session files (``live-*.jsonl``) as one retained raw record each; nothing is derived.

A live file holds one JSON object per line: a reading ``{"t": unix seconds, "metric": str,
"value": int}`` or a status line (a ``status`` or ``stop`` key). The record keeps the readings
only, so the bytes can be re-derived the day a fold exists (bet 9b-2).
"""

from __future__ import annotations

import datetime
import hashlib
import json

from disconect.ingest.model import Decoded

STREAM = "json:live"


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
