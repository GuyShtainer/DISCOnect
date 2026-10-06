"""Persist decoded facts: raw bytes first, canonical rows second, provenance always.

One transaction per source file, so a failure mid-file rolls back that file
only and the rest of the batch proceeds. A file whose bytes are already in
``raw_records`` is a duplicate and is skipped without decoding -- the same
FIT file arriving twice (USB pull, then a Connect export) is the normal case,
not an error.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import zlib
from collections.abc import Callable

from disconect.ingest import fit_wellness
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import Decoded, SleepSession
from disconect.redact import redact_text
from disconect.storage import iso_utc, parse_iso_utc, utc_now_iso
from disconect.storage import sqlite

IMPORTED = "imported"
DUPLICATE = "duplicate"
FAILED = "failed"
REPARSED = "reparsed"

#: Readings further apart than this are not consecutive: the watch recorded nothing in between
#: (off-wrist, powered down), so the change across the hole is not a step of the day.
STEP_GAP_MAX = datetime.timedelta(minutes=15)


def _steps(readings: list[tuple[datetime.datetime, float]]) -> list[float]:
    """Signed changes between consecutive readings of one local day, time-ordered, gaps skipped.
    The day's first reading has no predecessor: the step from the previous day is not counted."""
    ordered = sorted(readings)
    return [b - a for (t_a, a), (t_b, b) in zip(ordered, ordered[1:]) if t_b - t_a <= STEP_GAP_MAX]


def charged(readings: list[tuple[datetime.datetime, float]]) -> float:
    return sum(step for step in _steps(readings) if step > 0)


def drained(readings: list[tuple[datetime.datetime, float]]) -> float:
    return sum(-step for step in _steps(readings) if step < 0)


#: Reducers over one local day's (ts_utc, value) readings, by the name used in DERIVED_FROM_SAMPLES.
REDUCERS = {
    "mean": lambda r: sum(v for _, v in r) / len(r),
    "min": lambda r: min(v for _, v in r),
    "max": lambda r: max(v for _, v in r),
    "charged": charged,
    "drained": drained,
}



@dataclasses.dataclass
class ImportStats:
    transport: str
    files_seen: int = 0
    files_imported: int = 0
    files_duplicate: int = 0
    files_failed: int = 0
    records_written: int = 0
    ignored: int = 0
    streams: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    dropped: dict[str, int] = dataclasses.field(default_factory=dict)
    failures: list[dict[str, str]] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    dates_assumed_utc: int = 0
    derived_days: int = 0

    def status(self) -> str:
        if self.files_failed and not self.files_imported:
            return "failed"
        return "partial" if self.files_failed else "ok"

    def bump_stream(self, stream: str, key: str, count: int = 1) -> None:
        self.streams.setdefault(stream, {})[key] = self.streams.setdefault(stream, {}).get(key, 0) + count


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compress_verified(data: bytes) -> bytes:
    """zlib-compress and prove the round trip before the bytes are trusted to disk."""
    packed = zlib.compress(data, 6)
    if zlib.decompress(packed) != data:
        raise ValueError("compressed payload does not round-trip")
    return packed


class Writer:
    """Writes one import run. Construct inside ``open_for_write``."""

    def __init__(self, conn: sqlite.Connection, offsets: ClockOffsets, transport: str):
        self.conn = conn
        self.offsets = offsets
        self.stats = ImportStats(transport=transport)
        self.run_id: int | None = None
        self._interval_span: tuple[datetime.datetime, datetime.datetime] | None = None
        self._interval_devices: set[str | None] = set()
        self._sample_span: tuple[datetime.datetime, datetime.datetime] | None = None
        self._sample_devices: set[str | None] = set()

    # ---- run bookkeeping ----
    def begin_run(self) -> int:
        cursor = self.conn.execute(
            "INSERT INTO import_runs(started_at, transport, status) VALUES(?, ?, 'running')",
            (utc_now_iso(), self.stats.transport))
        self.run_id = int(cursor.lastrowid)
        return self.run_id

    def finish_run(self, error: str | None = None) -> None:
        stats = self.stats
        self.conn.execute(
            "UPDATE import_runs SET finished_at=?, status=?, files_seen=?, files_imported=?, "
            "files_duplicate=?, files_failed=?, records_written=?, error=? WHERE id=?",
            (utc_now_iso(), "failed" if error else stats.status(), stats.files_seen,
             stats.files_imported, stats.files_duplicate, stats.files_failed,
             stats.records_written, redact_text(error), self.run_id))

    # ---- provenance ----
    def _provenance(self, stream: str, stage: str, ok: bool, kind: str | None = None,
                    message: str | None = None, records: int = 0) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "INSERT OR IGNORE INTO stream_provenance(stream, updated_at) VALUES(?, ?)", (stream, now))
        if ok:
            self.conn.execute(
                f"UPDATE stream_provenance SET last_{stage}_ok_at=?, updated_at=?, "
                f"files_ok = files_ok + ?, records_written = records_written + ? WHERE stream=?",
                (now, now, 1 if stage == "write" else 0, records, stream))
        else:
            self.conn.execute(
                f"UPDATE stream_provenance SET last_{stage}_error_at=?, last_{stage}_error_kind=?, "
                f"last_{stage}_error_message=?, updated_at=?, files_failed = files_failed + 1 "
                f"WHERE stream=?",
                (now, kind, (redact_text(message) or "")[:500], now, stream))

    def record_parse_failure(self, stream: str, label: str, kind: str, message: str, *,
                             start_utc: datetime.datetime | None = None,
                             end_utc: datetime.datetime | None = None,
                             raw_record_id: int | None = None, payload_hash: str | None = None,
                             placeable: bool = True) -> None:
        """Record that one record's bytes could not be decoded, without raising.

        Bumps ``stats.files_failed``, appends to ``stats.failures``, marks the
        stream's parse provenance as failed, and writes an ``import_failures``
        row carrying the span that was knowable (``start_utc``/``end_utc``,
        or the retained record's own span via ``raw_record_id``) so the
        coverage ledger can show the days as ``failed`` rather than as days
        the source had nothing for. ``payload_hash`` (sha256 of the failing
        bytes) makes re-importing the same broken file record it once.
        ``placeable=False`` records the failure against no stream (the ledger
        counts it as unattributed). Shared by
        :meth:`write_fit`'s decode-failure branch and by reparse failures, so
        a decode error leaves the same trail regardless of which path hit it.
        """
        self.stats.files_failed += 1
        self.stats.failures.append({"file": label, "kind": kind, "error": redact_text(message)})
        self._provenance(stream, "parse", False, kind, message)
        self.conn.execute(
            "INSERT OR IGNORE INTO import_failures(run_id, stream, start_utc, end_utc, raw_record_id, "
            "payload_hash, kind, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.run_id, stream if placeable else None, iso_utc(start_utc) if start_utc else None,
             iso_utc(end_utc) if end_utc else None, raw_record_id, payload_hash, kind, utc_now_iso()))

    def last_raw_id(self) -> int:
        """Highest raw_records id right now (the relay marks what a run wrote)."""
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM raw_records").fetchone()[0])

    def record_export_range(self, stream: str, from_day: str, to_day: str) -> None:
        """Note the local-date window a source file *claims* to cover for ``stream``.

        Days inside a claimed window that end up with no record are days the
        source genuinely had nothing for; without the claim they would only
        be "not covered". Inclusive ``YYYY-MM-DD`` bounds.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO export_ranges(run_id, stream, from_day, to_day) VALUES(?,?,?,?)",
            (self.run_id, stream, from_day, to_day))

    # ---- raw retention ----
    def _store_raw(self, stream: str, source_key: str, source_scope: str, device_id: str | None,
                   start: datetime.datetime | None, end: datetime.datetime | None,
                   payload_kind: str, data: bytes, summary: dict,
                   origin: tuple[str, str] | None = None) -> int | None:
        """``origin`` = (transport, imported_at) of the record where it was first imported, kept
        when the bytes arrive through the relay so provenance survives the hop."""
        transport, imported_at = origin or (self.stats.transport, utc_now_iso())
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO raw_records(stream, source_key, source_scope, transport, "
            "device_id, start_utc, end_utc, payload_kind, payload, payload_hash, payload_bytes, "
            "decode_summary, imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stream, source_key, source_scope, transport, device_id,
             iso_utc(start) if start else None, iso_utc(end) if end else None, payload_kind,
             compress_verified(data), _sha256(data), len(data), json.dumps(summary, sort_keys=True),
             imported_at))
        return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def _is_duplicate(self, stream: str, source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM raw_records WHERE stream=? AND source_key=?", (stream, source_key)).fetchone()
        return row is not None

    # ---- canonical rows ----
    def _write_canonical(self, decoded: Decoded, raw_id: int) -> int:
        conn, scope, device = self.conn, decoded.source_scope, decoded.device_id
        written = 0
        if decoded.samples:
            conn.executemany(
                "INSERT OR REPLACE INTO metric_samples(metric, ts_utc, value, source_scope, "
                "device_id, raw_record_id) VALUES(?,?,?,?,?,?)",
                [(s.metric, iso_utc(s.ts_utc), s.value, scope, device, raw_id) for s in decoded.samples])
            written += len(decoded.samples)
            self._note_sample_span(decoded)
        for fact in decoded.daily:
            date = fact.date or self.offsets.local_date(fact.ts_utc)
            observed = iso_utc(fact.ts_utc) if fact.ts_utc else None
            written += self._upsert_daily("daily_metrics", "value", date, fact.metric, fact.value,
                                          observed, scope, device, raw_id)
        for label in decoded.labels:
            date = label.date or self.offsets.local_date(label.ts_utc)
            observed = iso_utc(label.ts_utc) if label.ts_utc else None
            written += self._upsert_daily("daily_labels", "label", date, label.metric, label.label,
                                          observed, scope, device, raw_id)
        if decoded.intervals:
            conn.executemany(
                "INSERT OR REPLACE INTO monitoring_intervals(ts_utc, activity_type, steps, cycles, "
                "active_time_s, active_calories_kcal, distance_m, intensity, source_scope, device_id, "
                "raw_record_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(iso_utc(i.ts_utc), i.activity_type, i.steps, i.cycles, i.active_time_s,
                  i.active_calories_kcal, i.distance_m, i.intensity, scope, device, raw_id)
                 for i in decoded.intervals])
            written += len(decoded.intervals)
            self._note_interval_span(decoded)
        if decoded.sleep is not None:
            written += self._write_sleep(decoded.sleep, scope, device, raw_id)
        for activity in decoded.activities:
            start = iso_utc(activity.start_utc)
            conn.execute(
                "INSERT OR REPLACE INTO activities(activity_id, start_utc, end_utc, sport, sub_sport, "
                "total_timer_s, total_elapsed_s, distance_m, calories_kcal, avg_hr, max_hr, "
                "avg_speed_mps, total_ascent_m, total_descent_m, source_scope, device_id, "
                "raw_record_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{start}|{scope}|{device or ''}", start,
                 iso_utc(activity.end_utc) if activity.end_utc else None, activity.sport,
                 activity.sub_sport, activity.total_timer_s, activity.total_elapsed_s,
                 activity.distance_m, activity.calories_kcal, activity.avg_hr, activity.max_hr,
                 activity.avg_speed_mps, activity.total_ascent_m, activity.total_descent_m,
                 scope, device, raw_id))
            written += 1
        if decoded.offsets:
            ClockOffsets.persist(conn, decoded.offsets, device, raw_id)
            written += len(decoded.offsets)
        return written

    def _upsert_daily(self, table: str, column: str, date: str, metric: str, value,
                      observed: str | None, scope: str, device: str | None, raw_id: int) -> int:
        """Keep the value observed latest for a (date, metric, scope, device) key.

        Files arrive in no particular order, so 'last write wins' would be
        wrong; 'latest observation wins' is what the watch itself shows.
        """
        row = self.conn.execute(
            f"SELECT id, observed_utc FROM {table} WHERE date=? AND metric=? AND source_scope=? "
            f"AND COALESCE(device_id, '') = ?", (date, metric, scope, device or "")).fetchone()
        if row is None:
            self.conn.execute(
                f"INSERT INTO {table}(date, metric, {column}, observed_utc, source_scope, device_id, "
                f"raw_record_id) VALUES(?,?,?,?,?,?,?)",
                (date, metric, value, observed, scope, device, raw_id))
            return 1
        existing_id, existing_observed = row[0], row[1]
        if observed is not None and existing_observed is not None and observed < existing_observed:
            return 0
        self.conn.execute(
            f"UPDATE {table} SET {column}=?, observed_utc=?, raw_record_id=? WHERE id=?",
            (value, observed, raw_id, existing_id))
        return 1

    def _write_sleep(self, sleep: SleepSession, scope: str, device: str | None, raw_id: int) -> int:
        if sleep.date is None:
            if sleep.end_utc is None:
                self.stats.dropped["sleep_without_date"] = self.stats.dropped.get("sleep_without_date", 0) + 1
                return 0
            sleep.date = self.offsets.local_date(sleep.end_utc)
        sleep_id = f"{sleep.date}|{scope}|{device or ''}"
        self.conn.execute("DELETE FROM sleep_stages WHERE sleep_id=?", (sleep_id,))
        self.conn.execute(
            "INSERT OR REPLACE INTO sleep_sessions(sleep_id, date, start_utc, end_utc, deep_s, "
            "light_s, rem_s, awake_s, unmeasurable_s, overall_score, quality_score, duration_score, "
            "recovery_score, deep_score, rem_score, light_score, awake_time_score, "
            "awakenings_count_score, combined_awake_score, restlessness_score, interruptions_score, "
            "awakenings_count, avg_stress, avg_spo2, lowest_spo2, avg_hr, avg_respiration, "
            "lowest_respiration, highest_respiration, retro, source_scope, device_id, raw_record_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sleep_id, sleep.date, iso_utc(sleep.start_utc) if sleep.start_utc else None,
             iso_utc(sleep.end_utc) if sleep.end_utc else None, sleep.deep_s, sleep.light_s,
             sleep.rem_s, sleep.awake_s, sleep.unmeasurable_s, sleep.overall_score,
             sleep.quality_score, sleep.duration_score, sleep.recovery_score, sleep.deep_score,
             sleep.rem_score, sleep.light_score, sleep.awake_time_score,
             sleep.awakenings_count_score, sleep.combined_awake_score, sleep.restlessness_score,
             sleep.interruptions_score, sleep.awakenings_count, sleep.avg_stress, sleep.avg_spo2,
             sleep.lowest_spo2, sleep.avg_hr, sleep.avg_respiration, sleep.lowest_respiration,
             sleep.highest_respiration, 1 if sleep.retro else 0, scope, device, raw_id))
        if sleep.stages:
            self.conn.executemany(
                "INSERT INTO sleep_stages(sleep_id, stage, start_utc, end_utc) VALUES(?,?,?,?)",
                [(sleep_id, s.stage, iso_utc(s.start_utc), iso_utc(s.end_utc)) for s in sleep.stages])
        written = 1 + len(sleep.stages)
        observed = iso_utc(sleep.end_utc) if sleep.end_utc else None
        if sleep.overall_score is not None:
            written += self._upsert_daily("daily_metrics", "value", sleep.date, "sleep_score",
                                          float(sleep.overall_score), observed, scope, device, raw_id)
        asleep = [s for s in (sleep.deep_s, sleep.light_s, sleep.rem_s) if s is not None]
        if asleep:
            written += self._upsert_daily("daily_metrics", "value", sleep.date, "sleep_duration",
                                          round(sum(asleep) / 60.0, 1), observed, scope, device, raw_id)
        return written

    def _note_sample_span(self, decoded: Decoded) -> None:
        moments = [s.ts_utc for s in decoded.samples]
        low, high = min(moments), max(moments)
        if self._sample_span is None:
            self._sample_span = (low, high)
        else:
            self._sample_span = (min(low, self._sample_span[0]), max(high, self._sample_span[1]))
        self._sample_devices.add(decoded.device_id)

    def _note_interval_span(self, decoded: Decoded) -> None:
        moments = [i.ts_utc for i in decoded.intervals]
        low, high = min(moments), max(moments)
        if self._interval_span is None:
            self._interval_span = (low, high)
        else:
            self._interval_span = (min(low, self._interval_span[0]), max(high, self._interval_span[1]))
        self._interval_devices.add(decoded.device_id)

    def _delete_canonical_for_raw(self, raw_id: int) -> None:
        """Remove every canonical row this raw record previously produced.

        Sleep stages carry no ``raw_record_id`` of their own -- they hang off
        ``sleep_sessions.sleep_id`` -- so sessions are looked up first and
        their stages deleted before the session rows themselves.
        """
        sleep_ids = [row[0] for row in self.conn.execute(
            "SELECT sleep_id FROM sleep_sessions WHERE raw_record_id=?", (raw_id,)).fetchall()]
        for sleep_id in sleep_ids:
            self.conn.execute("DELETE FROM sleep_stages WHERE sleep_id=?", (sleep_id,))
        for table in ("metric_samples", "daily_metrics", "daily_labels", "monitoring_intervals",
                     "activities", "clock_offsets", "sleep_sessions"):
            self.conn.execute(f"DELETE FROM {table} WHERE raw_record_id=?", (raw_id,))

    # ---- entry points ----
    def write_fit(self, data: bytes, label: str, origin: tuple[str, str] | None = None) -> str:
        """Retain and decode one FIT file. Returns IMPORTED, DUPLICATE or FAILED.

        ``label`` is a PII-free name used only in failure reports. ``origin`` (transport,
        imported_at) is set when the bytes came through the relay.
        """
        self.stats.files_seen += 1
        source_key = _sha256(data)
        if self._is_duplicate_fit(source_key):
            self.stats.files_duplicate += 1
            return DUPLICATE
        try:
            decoded = fit_wellness.decode_fit(data)
        except fit_wellness.FitDecodeError as exc:
            self.record_parse_failure(exc.stream or "fit:undecodable", label, exc.kind, str(exc),
                                      start_utc=exc.start_utc, end_utc=exc.end_utc, payload_hash=source_key,
                                      placeable=exc.stream is not None)
            return FAILED
        summary = {"messages": decoded.message_counts, "dropped": decoded.dropped,
                   "warnings": decoded.warnings[:10]}
        self._provenance(decoded.stream, "parse", True)
        try:
            self.conn.execute("BEGIN")
            raw_id = self._store_raw(decoded.stream, source_key, decoded.source_scope,
                                     decoded.device_id, decoded.start_utc, decoded.end_utc, "fit",
                                     data, summary, origin)
            if raw_id is None:
                self.conn.execute("ROLLBACK")
                self.stats.files_duplicate += 1
                return DUPLICATE
            written = self._write_canonical(decoded, raw_id)
            self.conn.execute("COMMIT")
        except sqlite.Error as exc:
            self._rollback()
            self.stats.files_failed += 1
            self.stats.failures.append({"file": label, "kind": "storage", "error": redact_text(str(exc))})
            self._provenance(decoded.stream, "write", False, "storage", str(exc))
            return FAILED
        self._provenance(decoded.stream, "write", True, records=written)
        self._account(decoded, written)
        return IMPORTED

    def _rollback(self) -> None:
        """End the open transaction -- unless the error already did (``RAISE(ROLLBACK)``, an I/O error), in
        which case a second ROLLBACK would raise "no transaction is active" and mask the original error."""
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def last_failure_is_storage(self) -> bool:
        """The latest recorded failure is a storage error (the write was refused), as opposed to a decode
        failure, which every device reaches identically. Call right after a write returned ``FAILED``."""
        return bool(self.stats.failures) and self.stats.failures[-1]["kind"] == "storage"

    def _is_duplicate_fit(self, source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM raw_records WHERE payload_kind='fit' AND source_key=?", (source_key,)).fetchone()
        return row is not None

    def write_json_record(self, stream: str, source_key: str, record: dict, decoded: Decoded,
                          label: str, origin: tuple[str, str] | None = None,
                          before_write: Callable[[], None] | None = None) -> str:
        """Retain one JSON record (a Connect export day/night) and its decoded facts.

        ``before_write`` (the relay's conflict hook) runs inside this record's own transaction, ahead of
        the raw row and canonical rows: it retires the stored record the new one replaces, so a storage
        error anywhere rolls the whole swap back (FAILED, the old record untouched). With a hook the
        stored row is expected to exist, so the duplicate pre-check is skipped.
        """
        self.stats.files_seen += 1
        if before_write is None and self._is_duplicate(stream, source_key):
            self.stats.files_duplicate += 1
            return DUPLICATE
        data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        summary = {"dropped": decoded.dropped, "warnings": decoded.warnings[:10]}
        try:
            self.conn.execute("BEGIN")
            if before_write is not None:
                before_write()
            raw_id = self._store_raw(stream, source_key, decoded.source_scope, decoded.device_id,
                                     decoded.start_utc, decoded.end_utc, "json", data, summary, origin)
            if raw_id is None:
                self.conn.execute("ROLLBACK")
                self.stats.files_duplicate += 1
                return DUPLICATE
            written = self._write_canonical(decoded, raw_id)
            self.conn.execute("COMMIT")
        except sqlite.Error as exc:
            self._rollback()
            self.stats.files_failed += 1
            self.stats.failures.append({"file": label, "kind": "storage", "error": redact_text(str(exc))})
            self._provenance(stream, "write", False, "storage", str(exc))
            return FAILED
        self._provenance(stream, "parse", True)
        self._provenance(stream, "write", True, records=written)
        self._account(decoded, written)
        return IMPORTED

    def _account(self, decoded: Decoded, written: int) -> None:
        self.stats.files_imported += 1
        self.stats.records_written += written
        self.stats.bump_stream(decoded.stream, "files")
        self.stats.bump_stream(decoded.stream, "records", written)
        for reason, count in decoded.dropped.items():
            self.stats.dropped[reason] = self.stats.dropped.get(reason, 0) + count
        for warning in decoded.warnings[:3]:
            if len(self.stats.warnings) < 20:
                self.stats.warnings.append(warning)

    # ---- derived dailies ----
    def derive_daily_steps(self) -> int:
        """Daily steps and distance from the cumulative monitoring counters written this run.

        Per local day and activity type the counter's maximum is that type's
        total; the day's total is the sum over types. The counters reset at the
        watch's local midnight and the record stamped *exactly* at midnight is
        the closing total of the day just ended, so a record is dated by the
        moment just before its timestamp (verified against Garmin Connect's
        daily totals on a real corpus). Written as source_scope 'local' because
        the watch never wrote the sum itself. Returns the day rows upserted.
        """
        if self._interval_span is None:
            return 0
        low = iso_utc(self._interval_span[0] - datetime.timedelta(days=1))
        high = iso_utc(self._interval_span[1] + datetime.timedelta(days=1))
        # Same rule as derive_daily_from_samples: only the local days this run's records fall in.
        second = datetime.timedelta(seconds=1)
        first_day, last_day = (self.offsets.local_date(m - second) for m in self._interval_span)
        days = 0
        for device in sorted(self._interval_devices, key=lambda name: name or ""):
            rows = self.conn.execute(
                "SELECT ts_utc, activity_type, steps, distance_m, raw_record_id FROM monitoring_intervals "
                "WHERE source_scope='device' AND COALESCE(device_id,'') = ? AND ts_utc BETWEEN ? AND ?",
                (device or "", low, high)).fetchall()
            per_day: dict[str, dict[str, dict[str, float]]] = {}
            latest: dict[str, tuple[str, int]] = {}
            for ts_text, activity_type, steps, distance, raw_id in rows:
                date = self.offsets.local_date(parse_iso_utc(ts_text) - second)
                if not first_day <= date <= last_day:
                    continue
                bucket = per_day.setdefault(date, {}).setdefault(activity_type, {})
                if steps is not None:
                    bucket["steps"] = max(bucket.get("steps", 0), steps)
                if distance is not None:
                    bucket["distance"] = max(bucket.get("distance", 0.0), distance)
                if date not in latest or ts_text > latest[date][0]:
                    latest[date] = (ts_text, raw_id)
            for date, by_type in per_day.items():
                observed, raw_id = latest[date]
                step_types = [b["steps"] for b in by_type.values() if "steps" in b]
                if step_types:
                    self._upsert_daily("daily_metrics", "value", date, "steps", float(sum(step_types)),
                                       observed, "local", device, raw_id)
                    days += 1
                distance_types = [b["distance"] for b in by_type.values() if "distance" in b]
                if distance_types:
                    self._upsert_daily("daily_metrics", "value", date, "distance",
                                       float(sum(distance_types)), observed, "local", device, raw_id)
        self.stats.derived_days = days
        return days

    # ---- derived dailies ----
    #: (sample metric, daily metric, reducer) derived per local day from the samples written this run.
    DERIVED_FROM_SAMPLES = (("stress", "stress_avg", "mean"), ("heart_rate", "heart_rate_min", "min"),
                            ("heart_rate", "heart_rate_max", "max"),
                            ("energy_reserve", "energy_reserve_high", "max"),
                            ("energy_reserve", "energy_reserve_low", "min"),
                            ("energy_reserve", "energy_reserve_charged", "charged"),
                            ("energy_reserve", "energy_reserve_drained", "drained"))

    def derive_daily_from_samples(self) -> int:
        """Daily stress average and heart-rate min/max from the device samples written this run.

        Written as source_scope 'local' (computed here, not by the watch), so
        the cloud's figure for the same day gets a device-side counterpart in
        the agreement report. Sentinel readings were dropped at decode time,
        so the average is over valid readings only. Returns day rows upserted.
        """
        if self._sample_span is None:
            return 0
        low = iso_utc(self._sample_span[0] - datetime.timedelta(days=1))
        high = iso_utc(self._sample_span[1] + datetime.timedelta(days=1))
        # The ±1-day query window holds every sample of the days this run touched, but only part
        # of the day before the first sample: that day keeps its earlier, complete figure.
        first_day, last_day = (self.offsets.local_date(m) for m in self._sample_span)
        days = 0
        for device in sorted(self._sample_devices, key=lambda name: name or ""):
            for sample_metric in sorted({item[0] for item in self.DERIVED_FROM_SAMPLES}):
                rows = self.conn.execute(
                    "SELECT ts_utc, value, raw_record_id FROM metric_samples WHERE metric=? AND "
                    "source_scope='device' AND COALESCE(device_id,'') = ? AND ts_utc BETWEEN ? AND ? "
                    "ORDER BY ts_utc",
                    (sample_metric, device or "", low, high)).fetchall()
                per_day: dict[str, list[tuple[datetime.datetime, float]]] = {}
                latest: dict[str, tuple[str, int]] = {}
                for ts_text, value, raw_id in rows:
                    moment = parse_iso_utc(ts_text)
                    date = self.offsets.local_date(moment)
                    if not first_day <= date <= last_day:
                        continue
                    per_day.setdefault(date, []).append((moment, value))
                    if date not in latest or ts_text > latest[date][0]:
                        latest[date] = (ts_text, raw_id)
                for source, daily_metric, reducer in self.DERIVED_FROM_SAMPLES:
                    if source != sample_metric:
                        continue
                    for date, readings in per_day.items():
                        observed, raw_id = latest[date]
                        self._upsert_daily("daily_metrics", "value", date, daily_metric,
                                           round(REDUCERS[reducer](readings), 3), observed, "local", device, raw_id)
                        days += 1
        return days

    def rewrite_canonical(self, raw_id: int, stream: str, decoded: Decoded, summary: dict) -> str:
        """Replace every canonical row ``raw_id`` produced with freshly decoded facts.

        The caller has already decoded successfully -- this records that as
        a 'parse' provenance event, then does only the 'write' side: delete
        what ``raw_id`` previously wrote, write ``decoded`` in its place, and
        refresh ``raw_records.decode_summary``. One transaction: a storage
        error rolls back to the previous canonical rows, leaving them
        intact, and is recorded in ``stats.failures``.

        Shared by :meth:`reparse_record` (one raw record redecoded on its
        own) and ``sources``' ``json:readiness`` batch reparse (many raw
        records redecoded together, because that stream's canonical value is
        chosen across all of them, not decided by any one in isolation).

        :returns: ``REPARSED`` or ``FAILED``.
        """
        self._provenance(stream, "parse", True)
        label = f"raw_record:{raw_id}"
        try:
            self.conn.execute("BEGIN")
            self._delete_canonical_for_raw(raw_id)
            written = self._write_canonical(decoded, raw_id)
            self.conn.execute("UPDATE raw_records SET decode_summary=? WHERE id=?",
                              (json.dumps(summary, sort_keys=True), raw_id))
            self.conn.execute("COMMIT")
        except sqlite.Error as exc:
            self._rollback()
            self.stats.files_failed += 1
            self.stats.failures.append({"file": label, "kind": "storage", "error": redact_text(str(exc))})
            self._provenance(stream, "write", False, "storage", str(exc))
            return FAILED
        self._provenance(stream, "write", True, records=written)
        self._account(decoded, written)
        return REPARSED

    def decode_raw(self, raw_id: int) -> tuple[str, Decoded, dict]:
        """Decode one retained raw record again with today's decoder, writing nothing.

        Returns ``(stream, decoded, summary)``. Raises ``ValueError`` for an
        unknown ``raw_id``, for ``json:readiness`` (a batch stream -- see
        ``sources.reparse_all``), for a stream with no registered decoder, or
        for a payload the decoder rejects; ``FitDecodeError`` propagates for a
        FIT payload that no longer parses.
        """
        row = self.conn.execute(
            "SELECT stream, payload_kind, payload FROM raw_records WHERE id=?", (raw_id,)).fetchone()
        if row is None:
            raise ValueError(f"no raw_records row with id={raw_id}")
        stream, payload_kind, blob = row
        from disconect.ingest import connect_export  # local: connect_export imports Writer at module scope
        if stream in connect_export.BATCH_STREAMS:
            raise ValueError(f"{stream} reparses as a batch -- use sources.reparse_all")
        data = zlib.decompress(blob)
        if payload_kind == "fit":
            decoded = fit_wellness.decode_fit(data)
            summary = {"messages": decoded.message_counts, "dropped": decoded.dropped,
                       "warnings": decoded.warnings[:10]}
            return stream, decoded, summary
        if payload_kind != "json":
            raise ValueError(f"unknown payload_kind {payload_kind!r}")
        decoder = connect_export.RECORD_DECODERS.get(stream)
        if decoder is None:
            raise ValueError(f"no reparse decoder registered for stream {stream!r}")
        result = decoder(json.loads(data))
        if result is None:
            raise ValueError("decoder produced no facts for this record")
        _source_key, decoded = result
        return stream, decoded, {"dropped": decoded.dropped, "warnings": decoded.warnings[:10]}

    def reparse_record(self, raw_id: int) -> str:
        """Redecode one retained raw record and replace the canonical rows it produced.

        A decode failure is recorded in ``stats.failures`` and returns
        ``FAILED`` rather than raising, so a caller looping over many raw ids
        needs no try/except of its own. An unknown ``raw_id`` or a batch
        stream is a caller bug and raises ``ValueError``.

        :returns: ``REPARSED`` or ``FAILED``.
        """
        label = f"raw_record:{raw_id}"
        try:
            stream, decoded, summary = self.decode_raw(raw_id)
        except fit_wellness.FitDecodeError as exc:
            stream = self.conn.execute("SELECT stream FROM raw_records WHERE id=?", (raw_id,)).fetchone()[0]
            self.record_parse_failure(stream, label, exc.kind, str(exc), raw_record_id=raw_id)
            return FAILED
        except ValueError as exc:
            if "no raw_records row" in str(exc) or "reparses as a batch" in str(exc):
                raise
            stream = self.conn.execute("SELECT stream FROM raw_records WHERE id=?", (raw_id,)).fetchone()[0]
            self.record_parse_failure(stream, label, "unrecognized_payload", str(exc), raw_record_id=raw_id)
            return FAILED
        except (zlib.error, json.JSONDecodeError, TypeError, KeyError) as exc:
            stream = self.conn.execute("SELECT stream FROM raw_records WHERE id=?", (raw_id,)).fetchone()[0]
            self.record_parse_failure(stream, label, "unrecognized_payload", f"{type(exc).__name__}: {exc}",
                                      raw_record_id=raw_id)
            return FAILED
        return self.rewrite_canonical(raw_id, stream, decoded, summary)
