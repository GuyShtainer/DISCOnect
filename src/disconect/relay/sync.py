"""Push and pull: what crosses the relay and how it is applied (pitch 10 v2).

Push  = every raw record and export range not yet marked in ``relay_seen`` / ``relay_seen_ranges``
        (pushed or received: pulled rows keep their origin transport, so the marks alone stop echoes) → one bundle (split at ~8 MB of payload) → ``relay.put``.
Pull  = ``relay.list(account)`` minus ``relay_bundles`` (a set difference: no watermark, late or
        out-of-order arrivals are fine) → ``unpack`` → verify every record's bytes against its
        hashes → **feed the normal Writer** as one import run with transport ``relay``: FIT first
        (clock-offset pre-pass, dedup by the bytes' sha256 alone), then JSON records through the
        same decoders the export import uses, then the touched JSON streams re-derived in content order (readiness as a batch),
        then the derived dailies. Ranges merge as a union. A bundle that fails authentication or
        validation is recorded ``rejected`` and retried on the next pull; it never blocks the rest.

Conflict rule (JSON streams keyed by date only): same (stream, source_key), different
payload_hash → the record whose decoded facts carry the later observed time wins (``end_utc``,
else the latest daily fact), ties by the larger payload_hash. A pure function of the two rows,
so every device converges whatever the arrival order. The loser's bytes go to ``raw_superseded``
and the decision to ``sync_conflicts``. A winning incoming record retires the stored loser inside its own
write transaction (``write_json_record(before_write=...)``): a storage error rolls the whole swap back, the
pull raises ``ConflictWriteFailed`` and the bundle stays ``applying`` until the next pull reapplies it. The relay's own bytes are never trusted before the AEAD
check, and ``Relay.put`` is called from exactly one place in this module with AEAD output.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
import zlib

from disconect import __version__
from disconect.ingest import connect_export, fit_wellness
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import Decoded
from disconect.ingest.sources import rederive_json
from disconect.ingest.writer import DUPLICATE, IMPORTED, Writer
from disconect.relay import bundle as bundle_module
from disconect.relay.bundle import BundleRejected, account_for, new_name, pack, unpack
from disconect.relay.folder import Relay
from disconect.storage import parse_iso_utc, sqlite, utc_now_iso

TRANSPORT_RELAY = "relay"
PUSH_BYTES_LIMIT = 8 * 1024 * 1024


@dataclasses.dataclass
class PushResult:
    bundles: list[str] = dataclasses.field(default_factory=list)
    records: int = 0
    ranges: int = 0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PullResult:
    applied: list[str] = dataclasses.field(default_factory=list)
    rejected: dict[str, str] = dataclasses.field(default_factory=dict)   # name -> reason
    records_new: int = 0
    records_duplicate: int = 0
    records_invalid: int = 0
    conflicts: int = 0
    ranges_new: int = 0
    gaps: list[dict] = dataclasses.field(default_factory=list)
    status: str = "ok"

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _device(conn: sqlite.Connection) -> tuple[str, int, str | None]:
    row = conn.execute("SELECT device_id, next_seq, last_bundle FROM relay_device WHERE id=1").fetchone()
    if row is None:
        device_id = secrets.token_hex(8)
        conn.execute("INSERT INTO relay_device(id, device_id, next_seq, last_bundle) VALUES(1, ?, 1, NULL)", (device_id,))
        return device_id, 1, None
    return row[0], int(row[1]), row[2]


# ---------------------------------------------------------------- push
def push(conn: sqlite.Connection, master: bytes, relay: Relay) -> PushResult:
    """Bundle every unseen local-origin record and range and put them on the relay."""
    account = account_for(master)
    result = PushResult()
    rows = conn.execute(
        "SELECT r.id, " + ", ".join("r." + c for c in bundle_module.RECORD_COLUMNS) + " FROM raw_records r "
        "LEFT JOIN relay_seen s ON s.raw_record_id = r.id WHERE s.raw_record_id IS NULL ORDER BY r.id").fetchall()
    ranges = conn.execute(
        "SELECT x.id, x.stream, x.from_day, x.to_day FROM export_ranges x "
        "LEFT JOIN relay_seen_ranges s ON s.export_range_id = x.id WHERE s.export_range_id IS NULL ORDER BY x.id").fetchall()
    if not rows and not ranges:
        return result
    batches: list[list] = [[]]
    size = 0
    for row in rows:
        payload_len = len(row[bundle_module.RECORD_COLUMNS.index("payload") + 1])
        if batches[-1] and size + payload_len > PUSH_BYTES_LIMIT:
            batches.append([])
            size = 0
        batches[-1].append(row)
        size += payload_len
    range_rows = [{"id": r[0], "stream": r[1], "from_day": r[2], "to_day": r[3]} for r in ranges]
    for index, batch in enumerate(batches):
        records = [dict(zip(bundle_module.RECORD_COLUMNS, row[1:])) for row in batch]
        bundle_ranges = range_rows if index == len(batches) - 1 else []
        device_id, seq, prev = _device(conn)
        name = new_name(account)
        header = {"format": bundle_module.FORMAT_VERSION, "core": __version__, "device_id": device_id,
                  "device_seq": seq, "prev": prev, "created_utc": utc_now_iso(),
                  "records": len(records), "ranges": len(bundle_ranges)}
        data = _seal(master, name, header, records, bundle_ranges)
        relay.put(name, data)   # the ONLY put in the codebase: its argument is AEAD output
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO relay_bundles(name, direction, status, device_id, device_seq, prev, created_utc, noted_at, "
                "bytes, records) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, "pushed", "applied", device_id, seq, prev, header["created_utc"], utc_now_iso(), len(data), len(records)))
            conn.executemany("INSERT OR IGNORE INTO relay_seen(raw_record_id, bundle, direction) VALUES(?,?,'pushed')",
                             [(row[0], name) for row in batch])
            conn.executemany("INSERT OR IGNORE INTO relay_seen_ranges(export_range_id, bundle, direction) VALUES(?,?,'pushed')",
                             [(r["id"], name) for r in bundle_ranges])
            conn.execute("UPDATE relay_device SET next_seq = ?, last_bundle = ? WHERE id=1", (seq + 1, name))
            conn.execute("COMMIT")
        except sqlite.Error:
            conn.execute("ROLLBACK")
            raise
        result.bundles.append(name)
        result.records += len(records)
        result.ranges += len(bundle_ranges)
    return result


def _seal(master: bytes, name: str, header: dict, records: list[dict], ranges: list[dict]) -> bytes:
    return pack(master, name, header, records, ranges)


# ---------------------------------------------------------------- pull
def _verify(record: dict) -> bytes | None:
    """The decompressed bytes if they match the record's hashes, else None."""
    try:
        data = zlib.decompress(record["payload"])
    except zlib.error:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if digest != record["payload_hash"] or len(data) != record["payload_bytes"]:
        return None
    if record["payload_kind"] == "fit" and digest != record["source_key"]:
        return None
    if record["payload_kind"] not in ("fit", "json") or record["stream"] is None or record["source_key"] is None:
        return None
    return data


def _observed(decoded: Decoded) -> str:
    """The latest moment the decoded facts speak for (ISO), '' when none."""
    moments = [decoded.end_utc] + [f.ts_utc for f in decoded.daily if f.ts_utc is not None]
    moments = [m for m in moments if m is not None]
    return max(moments).isoformat() if moments else ""


def _decode_json(stream: str, data: bytes, row: dict | None = None) -> tuple[dict, Decoded] | None:
    record = json.loads(data)
    if stream in connect_export.BATCH_STREAMS:
        # decoded as a batch later (rederive_json); the raw row keeps the sender's span and device
        row = row or {}
        return record, Decoded(stream=stream, source_scope=row.get("source_scope") or "vendor_cloud",
                               device_id=row.get("device_id"),
                               start_utc=parse_iso_utc(row["start_utc"]) if row.get("start_utc") else None,
                               end_utc=parse_iso_utc(row["end_utc"]) if row.get("end_utc") else None)
    decoder = connect_export.RECORD_DECODERS.get(stream)
    if decoder is None:
        return None
    result = decoder(record)
    if result is None:
        return None
    return record, result[1]


@dataclasses.dataclass(frozen=True)
class _Conflict:
    """The decision on a same-key, different-bytes pair: pure, writes nothing."""

    incoming_wins: bool
    rule: str
    winner: str
    loser: str
    existing: tuple   # (id, payload, payload_hash, payload_kind, transport, imported_at) of the stored row


class ConflictWriteFailed(RuntimeError):
    """A conflict's winner could not be stored. Everything the decision wrote was rolled back and the
    losing record is still in place; the bundle stays ``applying`` so the next pull applies it again."""


def _decide_conflict(conn: sqlite.Connection, record: dict, incoming: Decoded) -> _Conflict | None:
    """Same key, different bytes -> who wins. ``None`` when no stored row differs from the incoming bytes
    (no row at all, or the very same bytes): nothing to decide."""
    existing = conn.execute(
        "SELECT id, payload, payload_hash, payload_kind, transport, imported_at FROM raw_records WHERE stream=? AND source_key=?",
        (record["stream"], record["source_key"])).fetchone()
    if existing is None or existing[2] == record["payload_hash"]:
        return None
    ex_payload, ex_hash = existing[1], existing[2]
    try:
        ex_decoded = _decode_json(record["stream"], zlib.decompress(ex_payload))
    except Exception:  # noqa: BLE001 - an undecodable existing record loses by definition
        ex_decoded = None
    if record["stream"] in connect_export.BATCH_STREAMS:
        # a batch stream's record carries no time of its own (the stored row's span is not
        # the facts' time), so the protocol's rule is the hash alone -- both sides must agree
        ex_observed = in_observed = ""
    else:
        ex_observed = _observed(ex_decoded[1]) if ex_decoded else ""
        in_observed = _observed(incoming)
    if in_observed != ex_observed:
        incoming_wins, rule = in_observed > ex_observed, "observed_utc"
    else:
        incoming_wins, rule = record["payload_hash"] > ex_hash, "payload_hash"
    winner, loser = (record["payload_hash"], ex_hash) if incoming_wins else (ex_hash, record["payload_hash"])
    return _Conflict(incoming_wins, rule, winner, loser, tuple(existing))


def _note_conflict(conn: sqlite.Connection, record: dict, conflict: _Conflict, bundle_name: str) -> None:
    conn.execute("INSERT INTO sync_conflicts(stream, source_key, winner_hash, loser_hash, rule, bundle, decided_at) "
                 "VALUES(?,?,?,?,?,?,?)", (record["stream"], record["source_key"], conflict.winner, conflict.loser,
                                           conflict.rule, bundle_name, utc_now_iso()))


def _record_incoming_loser(conn: sqlite.Connection, record: dict, conflict: _Conflict, bundle_name: str) -> None:
    """The incoming record lost: keep its bytes in ``raw_superseded`` with the decision, one transaction."""
    conn.execute("BEGIN")
    try:
        _note_conflict(conn, record, conflict, bundle_name)
        conn.execute("INSERT INTO raw_superseded(stream, source_key, payload_kind, payload, payload_hash, transport, "
                     "imported_at, superseded_at, bundle) VALUES(?,?,?,?,?,?,?,?,?)",
                     (record["stream"], record["source_key"], record["payload_kind"], record["payload"], record["payload_hash"],
                      record["transport"], record["imported_at"], utc_now_iso(), bundle_name))
        conn.execute("COMMIT")
    except sqlite.Error:
        conn.execute("ROLLBACK")
        raise


def _loser_writes(conn: sqlite.Connection, writer: Writer, record: dict, conflict: _Conflict, bundle_name: str):
    """The pre-write hook for an incoming winner: every write that retires the stored loser. It runs
    inside ``write_json_record``'s own transaction, before the winner's raw row and canonical rows, so a
    storage error anywhere rolls the decision back together with the winner's write."""
    ex_id, ex_payload, ex_hash, ex_kind, ex_transport, ex_imported = conflict.existing

    def hook() -> None:
        _note_conflict(conn, record, conflict, bundle_name)
        conn.execute("INSERT INTO raw_superseded(stream, source_key, payload_kind, payload, payload_hash, transport, "
                     "imported_at, superseded_at, bundle) VALUES(?,?,?,?,?,?,?,?,?)",
                     (record["stream"], record["source_key"], ex_kind, ex_payload, ex_hash, ex_transport, ex_imported,
                      utc_now_iso(), bundle_name))
        writer._delete_canonical_for_raw(ex_id)
        # rows that point at the losing record (a past decode failure, relay bookkeeping) go first
        conn.execute("DELETE FROM import_failures WHERE raw_record_id=?", (ex_id,))
        conn.execute("DELETE FROM relay_seen WHERE raw_record_id=?", (ex_id,))
        conn.execute("DELETE FROM raw_records WHERE id=?", (ex_id,))

    return hook


def pull(conn: sqlite.Connection, master: bytes, relay: Relay) -> PullResult:
    """Fetch and apply every bundle on the relay this store has not applied yet."""
    account = account_for(master)
    result = PullResult()
    known = {row[0] for row in conn.execute("SELECT name FROM relay_bundles WHERE status='applied'").fetchall()}
    half_applied = {row[0] for row in conn.execute("SELECT name FROM relay_bundles WHERE status='applying'").fetchall()}
    pending = [name for name in relay.list(account) if name not in known]
    if not pending:
        _report_gaps(conn, result)
        return result
    writer = Writer(conn, ClockOffsets.load(conn), TRANSPORT_RELAY)
    writer.begin_run()
    applied: list[tuple] = []
    reparse_streams: set[str] = set()
    try:
        for name in pending:
            try:
                blob = relay.get(name)
                header, records, ranges = unpack(master, name, blob)
            except (BundleRejected, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", type(exc).__name__)
                result.rejected[name] = reason
                writer.stats.files_failed += 1
                conn.execute("INSERT OR REPLACE INTO relay_bundles(name, direction, status, noted_at, reason) "
                             "VALUES(?, 'pulled', 'rejected', ?, ?)", (name, utc_now_iso(), reason))
                continue
            if name in half_applied:
                # a crash cut the previous pull before its derive step: its records are in, the derived
                # rows may not be — re-derive those streams in full (the pitch's breaker path)
                reparse_streams.update(r["stream"] for r in records)
            conn.execute("INSERT OR REPLACE INTO relay_bundles(name, direction, status, noted_at, bytes, records) "
                         "VALUES(?, 'pulled', 'applying', ?, ?, ?)", (name, utc_now_iso(), len(blob), len(records)))
            fits = [(r, d) for r in records if r["payload_kind"] == "fit" and (d := _verify(r)) is not None]
            jsons = [(r, d) for r in records if r["payload_kind"] == "json" and (d := _verify(r)) is not None]
            result.records_invalid += len(records) - len(fits) - len(jsons)
            for _record, data in fits:
                writer.offsets.extend(fit_wellness.scan_clock_offsets(data))
            for record, data in fits:
                before = writer.last_raw_id()
                outcome = writer.write_fit(data, f"relay:{name[-8:]}", origin=(record["transport"], record["imported_at"]))
                _mark(conn, writer, before, name, outcome, result)
                if outcome == DUPLICATE:
                    _mark_existing(conn, record, name)
            for record, data in jsons:
                try:
                    decoded = _decode_json(record["stream"], data, record)
                except (ValueError, TypeError):
                    decoded = None
                if decoded is None:
                    result.records_invalid += 1
                    continue
                record_dict, decoded_facts = decoded
                conflict = _decide_conflict(conn, record, decoded_facts)
                hook = None
                if conflict is not None and not conflict.incoming_wins:
                    _record_incoming_loser(conn, record, conflict, name)
                    result.conflicts += 1
                    continue
                if conflict is not None:
                    hook = _loser_writes(conn, writer, record, conflict, name)
                elif conn.execute("SELECT 1 FROM raw_records WHERE stream=? AND source_key=?",
                                  (record["stream"], record["source_key"])).fetchone():
                    # the same bytes are already here (a local import or an earlier bundle)
                    if conn.execute("SELECT 1 FROM sync_conflicts WHERE bundle=? AND source_key=? AND stream=?",
                                    (name, record["source_key"], record["stream"])).fetchone():
                        result.conflicts += 1
                    else:
                        result.records_duplicate += 1
                        _mark_existing(conn, record, name)
                    continue
                before = writer.last_raw_id()
                outcome = writer.write_json_record(record["stream"], record["source_key"], record_dict, decoded_facts,
                                                   f"relay:{name[-8:]}", origin=(record["transport"], record["imported_at"]),
                                                   before_write=hook)
                if hook is not None:
                    if outcome != IMPORTED:
                        raise ConflictWriteFailed(f"{record['stream']} winner not stored ({outcome}); decision rolled back")
                    result.conflicts += 1
                _mark(conn, writer, before, name, outcome, result)
            for item in ranges:
                before = conn.total_changes
                writer.record_export_range(item["stream"], item["from_day"], item["to_day"])
                row = conn.execute("SELECT id FROM export_ranges WHERE stream=? AND from_day=? AND to_day=?",
                                   (item["stream"], item["from_day"], item["to_day"])).fetchone()
                if row and conn.total_changes > before:
                    result.ranges_new += 1
                if row:
                    conn.execute("INSERT OR IGNORE INTO relay_seen_ranges(export_range_id, bundle, direction) VALUES(?,?,'pulled')",
                                 (row[0], name))
            applied.append((name, header, len(blob), len(records)))
        # daily rows are a pure function of the raw set: every JSON stream a record was written to
        # (new, or a conflict winner that replaced the loser) is rebuilt in content order
        rederive_json(conn, writer, list(writer.stats.streams))
        writer.derive_daily_steps()
        writer.derive_daily_from_samples()
        if reparse_streams:
            from disconect.ingest import sources  # noqa: PLC0415 - avoid an import cycle at module load
            sources.reparse_all(conn, streams=sorted(reparse_streams))
        for name, header, size, count in applied:
            conn.execute(
                "INSERT OR REPLACE INTO relay_bundles(name, direction, status, device_id, device_seq, prev, created_utc, noted_at, "
                "bytes, records) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, "pulled", "applied", header.get("device_id"), header.get("device_seq"), header.get("prev"),
                 header.get("created_utc"), utc_now_iso(), size, count))
            result.applied.append(name)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        writer.finish_run(error=f"{type(exc).__name__}: {exc}")
        raise
    writer.stats.dates_assumed_utc = writer.offsets.assumed_utc
    writer.finish_run()
    conn.commit()
    if result.rejected:
        result.status = "partial"
    _report_gaps(conn, result)
    return result


def _mark(conn: sqlite.Connection, writer: Writer, before: int, name: str, outcome: str, result: PullResult) -> None:
    if outcome == IMPORTED:
        result.records_new += 1
        for (raw_id,) in conn.execute("SELECT id FROM raw_records WHERE id > ?", (before,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO relay_seen(raw_record_id, bundle, direction) VALUES(?,?,'pulled')", (raw_id, name))
    elif outcome == DUPLICATE:
        result.records_duplicate += 1
    else:
        result.records_invalid += 1


def _mark_existing(conn: sqlite.Connection, record: dict, name: str) -> None:
    """The same bytes were already here (a local import or an earlier bundle): never push them back."""
    row = conn.execute("SELECT id FROM raw_records WHERE stream=? AND source_key=? AND payload_hash=?",
                       (record["stream"], record["source_key"], record["payload_hash"])).fetchone()
    if row is None and record["payload_kind"] == "fit":
        row = conn.execute("SELECT id FROM raw_records WHERE payload_kind='fit' AND source_key=?",
                           (record["source_key"],)).fetchone()
    if row is not None:
        conn.execute("INSERT OR IGNORE INTO relay_seen(raw_record_id, bundle, direction) VALUES(?,?,'pulled')", (row[0], name))


def _report_gaps(conn: sqlite.Connection, result: PullResult) -> None:
    """A device's chain (device_seq, prev) with a missing link means the relay dropped a middle bundle."""
    rows = conn.execute("SELECT device_id, device_seq, prev, name FROM relay_bundles WHERE status='applied' "
                        "AND device_id IS NOT NULL ORDER BY device_id, device_seq").fetchall()
    by_device: dict[str, list] = {}
    for device_id, seq, prev, name in rows:
        by_device.setdefault(device_id, []).append((seq, prev, name))
    for device_id, chain in by_device.items():
        names = {name for _s, _p, name in chain}
        for seq, prev, _name in chain:
            if prev is not None and prev not in names:
                result.gaps.append({"chain": device_id, "device_seq": seq, "missing": prev})


def status(conn: sqlite.Connection) -> dict:
    """Counts only: bundles pushed/pulled/rejected, records seen, conflicts, chain gaps."""
    counts = {row[0] + "_" + row[1]: row[2] for row in conn.execute(
        "SELECT direction, status, count(*) FROM relay_bundles GROUP BY 1, 2").fetchall()}
    unsent = conn.execute("SELECT count(*) FROM raw_records r LEFT JOIN relay_seen s ON s.raw_record_id=r.id "
                          "WHERE s.raw_record_id IS NULL").fetchone()[0]
    result = PullResult()
    _report_gaps(conn, result)
    return {"bundles": counts, "records_unsent": unsent,
            "records_seen": conn.execute("SELECT count(*) FROM relay_seen").fetchone()[0],
            "conflicts": conn.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0],
            "superseded": conn.execute("SELECT count(*) FROM raw_superseded").fetchone()[0],
            "gaps": result.gaps}


def forget_relay_state(conn: sqlite.Connection) -> None:
    """After a master rotation: a new account and key, so every record is pushed again under them.
    Conflict and superseded history is kept (it is local history, not relay state)."""
    for table in ("relay_bundles", "relay_seen", "relay_seen_ranges", "relay_device"):
        conn.execute(f"DELETE FROM {table}")
