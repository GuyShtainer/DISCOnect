"""Time formatting shared by storage and ingest: one ISO shape everywhere."""

from __future__ import annotations

import datetime
import os
import re

UTC = datetime.timezone.utc

#: TEST-ONLY clock pin (docs/serve-protocol.md): ``YYYY-MM-DDTHH:MM:SSZ``. Both cores honour it at
#: their one ``now()`` site so a differential run is deterministic. The desktop sidecar's env
#: allowlist never passes it on; an unparseable value is ignored.
NOW_ENV = "DISCONECT_NOW"


def iso_utc(moment: datetime.datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (the store's one shape)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; the store only accepts aware UTC moments")
    return moment.astimezone(UTC).strftime(ISO)


ISO = "%Y-%m-%dT%H:%M:%SZ"
#: The store's shape, exactly: ASCII digits in place, nothing around them. Text of this shape takes the
#: fast path below; everything else keeps ``strptime``'s acceptance and its error text (which the Rust
#: core mirrors), so the parser's behaviour is the same as before 7b-14, only ~10× faster per call.
_STORE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_strptime = datetime.datetime.strptime
_fromisoformat = datetime.datetime.fromisoformat


def parse_iso_utc(text: str) -> datetime.datetime:
    """Parse the store's ``YYYY-MM-DDTHH:MM:SSZ`` shape back into an aware datetime."""
    if _STORE_SHAPE.match(text):
        try:
            return _fromisoformat(text)  # aware UTC, equal to strptime's result
        except ValueError:
            pass  # an impossible date in the right shape: strptime raises the one error text
    return _strptime(text, ISO).replace(tzinfo=UTC)


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
