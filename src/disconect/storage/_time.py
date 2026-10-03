"""Time formatting shared by storage and ingest: one ISO shape everywhere."""

from __future__ import annotations

import datetime
import os

UTC = datetime.timezone.utc

#: TEST-ONLY clock pin (docs/serve-protocol.md): ``YYYY-MM-DDTHH:MM:SSZ``. Both cores honour it at
#: their one ``now()`` site so a differential run is deterministic. The desktop sidecar's env
#: allowlist never passes it on; an unparseable value is ignored.
NOW_ENV = "DISCONECT_NOW"


def iso_utc(moment: datetime.datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (the store's one shape)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; the store only accepts aware UTC moments")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: str) -> datetime.datetime:
    """Parse the store's ``YYYY-MM-DDTHH:MM:SSZ`` shape back into an aware datetime."""
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def now_utc() -> datetime.datetime:
    """The one clock the core reads: the real time, or the ``DISCONECT_NOW`` pin (tests only)."""
    pinned = os.environ.get(NOW_ENV)
    if pinned:
        try:
            return parse_iso_utc(pinned)
        except ValueError:
            pass
    return datetime.datetime.now(UTC)


def utc_now_iso() -> str:
    """Now, in the store's shape."""
    return iso_utc(now_utc())
