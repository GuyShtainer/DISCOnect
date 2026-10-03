"""Canonical JSON shape of a ``Decoded`` — the oracle's answer the Rust ``fit`` module must match.

Shared by the fixture generator (``gen_core_fixtures.py``), the parity tests and the Rust crate's
tests (which parse the ``.expected.json`` files). Instants are ``YYYY-MM-DDTHH:MM:SSZ``; floats
stay JSON numbers (Python ``repr`` round-trips exactly through serde_json); None is null.
"""

from __future__ import annotations

import dataclasses
import datetime

from disconect.ingest.model import Decoded


def _iso(moment: datetime.datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(obj):
    if dataclasses.is_dataclass(obj):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime.datetime):
        return _iso(obj)
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in sorted(obj.items())}
    return obj


def decoded_to_json(decoded: Decoded) -> dict:
    """Everything the writer consumes, in a stable order; ``warnings`` as a count only."""
    out = _plain(decoded)
    out["warnings"] = len(decoded.warnings)
    out["record_count"] = decoded.record_count()
    return out
