"""Local calendar dates from the watch's own UTC offsets.

Garmin keys every daily figure by the *local* date on the watch. FIT samples
are UTC. The bridge is the offsets the watch states in ``monitoring_info``,
``timestamp_correlation`` and ``skin_temp_overnight`` (``local_timestamp``
minus ``timestamp``). The resolver picks the stated offset nearest in time to
the moment being dated. With no offset known at all it falls back to UTC and
says so, rather than guessing a zone.
"""

from __future__ import annotations

import bisect
import datetime

from disconect.ingest.model import ClockOffset
from disconect.storage import iso_utc, parse_iso_utc
from disconect.storage import sqlite

UTC = datetime.timezone.utc


class ClockOffsets:
    """Sorted (moment, offset) pairs with nearest-neighbour lookup."""

    def __init__(self) -> None:
        self._moments: list[float] = []
        self._offsets: list[int] = []
        self.assumed_utc = 0  # how many dates were resolved without any known offset

    @classmethod
    def load(cls, conn: sqlite.Connection) -> ClockOffsets:
        """Offsets already persisted by earlier imports."""
        offsets = cls()
        for ts_utc, offset_s in conn.execute("SELECT ts_utc, offset_s FROM clock_offsets"):
            offsets.add(ClockOffset(parse_iso_utc(ts_utc), int(offset_s)))
        return offsets

    def __len__(self) -> int:
        return len(self._moments)

    def add(self, offset: ClockOffset) -> None:
        key = offset.ts_utc.timestamp()
        index = bisect.bisect_left(self._moments, key)
        if index < len(self._moments) and self._moments[index] == key:
            self._offsets[index] = offset.offset_s
            return
        self._moments.insert(index, key)
        self._offsets.insert(index, offset.offset_s)

    def extend(self, offsets: list[ClockOffset]) -> None:
        for offset in offsets:
            self.add(offset)

    def offset_at(self, moment: datetime.datetime) -> int | None:
        """The stated offset nearest to ``moment``, or None if none is known."""
        if not self._moments:
            return None
        key = moment.timestamp()
        index = bisect.bisect_left(self._moments, key)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(self._moments)]
        best = min(candidates, key=lambda i: abs(self._moments[i] - key))
        return self._offsets[best]

    def local_date(self, moment: datetime.datetime) -> str:
        """``YYYY-MM-DD`` on the watch's clock at ``moment``."""
        offset = self.offset_at(moment)
        if offset is None:
            self.assumed_utc += 1
            offset = 0
        return (moment.astimezone(UTC) + datetime.timedelta(seconds=offset)).date().isoformat()

    @staticmethod
    def persist(conn: sqlite.Connection, offsets: list[ClockOffset], device_id: str | None,
                raw_record_id: int | None) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO clock_offsets(ts_utc, offset_s, device_id, raw_record_id) "
            "VALUES(?, ?, ?, ?)",
            [(iso_utc(o.ts_utc), o.offset_s, device_id, raw_record_id) for o in offsets])
