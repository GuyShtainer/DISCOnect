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


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
            if not (_is_number(obj["t"]) and isinstance(obj["metric"], str)
                    and isinstance(obj["value"], int) and not isinstance(obj["value"], bool)):
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

    Raises ValueError for a record with no readings array, an empty one included.
    """
    readings = record.get("readings") if isinstance(record, dict) else None
    if not isinstance(readings, list) or not readings:
        raise ValueError("live record without readings")
    ordered, data = canonical_payload(readings)
    first, last = ordered["readings"][0][0], ordered["readings"][-1][0]
    decoded = Decoded(stream=STREAM, source_scope="device")
    decoded.start_utc = datetime.datetime.fromtimestamp(first, datetime.timezone.utc)
    decoded.end_utc = datetime.datetime.fromtimestamp(last, datetime.timezone.utc)
    return hashlib.sha256(data).hexdigest(), decoded
