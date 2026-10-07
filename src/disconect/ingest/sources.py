"""Turn a path into an import: detect what it is, run the passes, report.

Accepted inputs:

* a Connect account export -- the outer zip, the extracted folder, or its
  ``DI_CONNECT`` folder;
* a folder of FIT files (a ``GARMIN/`` tree pulled over USB, a Gadgetbridge
  export folder, a drop folder) or a single ``.fit`` file, or a zip of them.

Two passes over FIT bytes: first collect every UTC offset the files state, so
local dates resolve consistently regardless of file order; then decode and
write. Decoding twice is cheap next to the certainty it buys.
"""

from __future__ import annotations

import json
import pathlib
import zipfile
import zlib
from collections.abc import Callable, Collection, Iterator

from disconect.ingest import connect_export, fit_wellness, live
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import Decoded
from disconect.ingest.writer import ImportStats, Writer
from disconect.redact import redact_text
from disconect.storage import sqlite, utc_now_iso

TRANSPORT_CONNECT_EXPORT = "connect_export"
TRANSPORT_DROP = "drop"
TRANSPORT_BLE = "ble"
DROPPED_LIVE_EMPTY = "live_file_without_readings"
DROPPED_LIVE_CUT_OFF = "live_file_cut_off"

#: ``progress(done, total_or_None, note)``; ``note`` is the write outcome (``imported`` | ``duplicate`` | ``failed``) in the FIT phase and the live phase (which also says `skipped` for a live file with no readings) and the stream name (``json:...``) in the export-JSON phase — never a file name or path.
ProgressCallback = Callable[[int, int | None, str], None]
#: ``cancel()`` is asked after each file, right after ``progress``: True stops the import there (the file that was
#: being read is kept, nothing later is read, the phases not yet started are skipped, the run is booked ``cancelled``).
CancelCheck = Callable[[], bool]


def iter_fit_files(path: pathlib.Path) -> Iterator[tuple[str, bytes]]:
    """(label, bytes) for every ``.fit`` under ``path`` (file, folder, or zip)."""
    path = pathlib.Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if not info.is_dir() and info.filename.lower().endswith(".fit"):
                    yield connect_export.mask_label(info.filename), archive.read(info)
        return
    if path.is_file():
        yield connect_export.mask_label(path.name), path.read_bytes()
        return
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() == ".fit":
            yield connect_export.mask_label(str(candidate.relative_to(path))), candidate.read_bytes()


def iter_live_files(path: pathlib.Path) -> Iterator[tuple[str, list[list], bool]]:
    """(label, readings, cut_off) for every live-link ``.jsonl`` under ``path`` (a file or a folder).

    A status-only file yields an empty readings list; a file cut off mid-line yields the readings
    of its whole lines with ``cut_off`` True. Files that fail the live rule are not yielded, so
    the caller can treat them as it does today.
    """
    path = pathlib.Path(path)
    if path.is_file():
        candidates = [path] if path.suffix.lower() == ".jsonl" else []
    elif path.is_dir():
        candidates = [c for c in sorted(path.rglob("*.jsonl")) if c.is_file()]
    else:
        candidates = []
    for candidate in candidates:
        parsed = live.read_live_file(candidate.read_bytes())
        if parsed is not None:
            readings, cut_off = parsed
            yield connect_export.mask_label(candidate.name), readings, cut_off


def _import_live_batch(files: list[tuple[str, list[list], bool]], writer: Writer,
                       progress: ProgressCallback | None = None, cancel: CancelCheck | None = None) -> None:
    """One ``json:live`` raw record per file; a file without readings is counted and skipped, a
    file cut off mid-line is counted and its whole lines imported."""
    for done, (label, readings, cut_off) in enumerate(files, start=1):
        if cut_off:
            writer.stats.dropped[DROPPED_LIVE_CUT_OFF] = writer.stats.dropped.get(DROPPED_LIVE_CUT_OFF, 0) + 1
        if not readings:
            writer.stats.dropped[DROPPED_LIVE_EMPTY] = writer.stats.dropped.get(DROPPED_LIVE_EMPTY, 0) + 1
            outcome = "skipped"
        else:
            record, _data = live.canonical_payload(readings)
            source_key, decoded = live.decode_live_record(record)
            outcome = writer.write_json_record(live.STREAM, source_key, record, decoded, label,
                                               origin=(TRANSPORT_BLE, utc_now_iso()))
        if progress is not None:
            progress(done, len(files), outcome)
        if cancel is not None and cancel():
            writer.stats.cancelled = True
            return


def _import_fit_batch(files: list[tuple[str, bytes]], writer: Writer,
                      progress: ProgressCallback | None = None, cancel: CancelCheck | None = None) -> None:
    for _label, data in files:
        writer.offsets.extend(fit_wellness.scan_clock_offsets(data))
    for done, (label, data) in enumerate(files, start=1):
        stream = writer.write_fit(data, label)
        if progress is not None:
            progress(done, len(files), stream)
        if cancel is not None and cancel():
            writer.stats.cancelled = True   # the batch's own derivations still run for what was written
            break
    writer.derive_daily_steps()
    writer.derive_daily_from_samples()


def import_path(path: pathlib.Path, conn: sqlite.Connection, transport: str | None = None,
                progress: ProgressCallback | None = None, cancel: CancelCheck | None = None) -> ImportStats:
    """Import whatever ``path`` is into ``conn`` (a writable connection) and report.

    ``progress(done, total, note)`` is called after each file; ``total`` is None when the
    count is not known up front (the JSON files of a Connect export). ``cancel()`` is asked
    after each file (see :data:`CancelCheck`): a True answer ends the import after that file,
    skips the phases not yet started and books the run ``cancelled`` (``stats.cancelled``).
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such file or folder: {path}")
    is_export = connect_export.looks_like_export(path)
    transport = transport or (TRANSPORT_CONNECT_EXPORT if is_export else TRANSPORT_DROP)
    writer = Writer(conn, ClockOffsets.load(conn), transport)
    writer.begin_run()
    try:
        if is_export:
            fits = list(connect_export.collect_fit_members(path))
            _import_fit_batch(fits, writer, progress, cancel)
            if not writer.stats.cancelled:
                connect_export.import_connect_export(path, writer, progress, cancel)
            if not writer.stats.cancelled:
                rederive_json(conn, writer, _stored_json_streams(conn))
                writer.derive_live_samples()
        else:
            live_files = list(iter_live_files(path))
            _import_live_batch(live_files, writer, progress, cancel)
            fit_files = [] if path.is_file() and live_files else list(iter_fit_files(path))
            if not writer.stats.cancelled:
                _import_fit_batch(fit_files, writer, progress, cancel)
            writer.derive_live_samples()   # also after a cancel: the live files written get their samples
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller
        writer.finish_run(error=f"{type(exc).__name__}: {exc}")
        raise
    writer.stats.dates_assumed_utc = writer.offsets.assumed_utc
    writer.finish_run()
    return writer.stats


def _stored_json_streams(conn: sqlite.Connection) -> list[str]:
    """Every ``json:*`` stream that has a raw record in the store, sorted."""
    return [stream for (stream,) in conn.execute(
        "SELECT DISTINCT stream FROM raw_records WHERE stream LIKE 'json:%' ORDER BY stream")]


def _raw_ids_by_stream(conn: sqlite.Connection, streams: list[str] | None) -> list[tuple[int, str]]:
    """(id, stream) for every raw record in an order that depends on content alone.

    Oldest ``start_utc`` first (NULL first), ties by ``payload_hash``, ``stream``, ``source_key``:
    never the local ``id``, so two stores holding the same raw set fold it identically.
    """
    order = "ORDER BY start_utc, payload_hash, stream, source_key"
    if streams:
        placeholders = ",".join("?" * len(streams))
        query = f"SELECT id, stream FROM raw_records WHERE stream IN ({placeholders}) {order}"
        return conn.execute(query, tuple(streams)).fetchall()
    return conn.execute(f"SELECT id, stream FROM raw_records {order}").fetchall()


def _decode_readiness_batch(conn: sqlite.Connection) -> tuple[list[tuple[int, Decoded, dict]], list[tuple[int, str, str]]]:
    """Decode every ``json:readiness`` raw record together.

    Returns ``(decoded, unreadable)``: ``(raw_id, decoded, summary)`` per record the batch
    chose facts for or not, and ``(raw_id, kind, message)`` per record whose stored bytes fail
    to parse as JSON (left out of the batch; it does not block the rest).

    The day's canonical value is the morning reset when the day has one,
    else the latest update -- a choice ``connect_export.decode_readiness_records``
    can only make by looking at a whole day's records at once, so every
    readiness raw record in the store is fed to it together, never alone.
    Records are fed in ``payload_hash`` order so its dedup on ``timestamp`` does not
    depend on arrival order.
    """
    rows = conn.execute(
        "SELECT id, source_key, payload FROM raw_records WHERE stream='json:readiness' "
        "ORDER BY payload_hash, id").fetchall()
    records = []
    raw_id_by_key: dict[str, int] = {}
    unreadable: list[tuple[int, str, str]] = []
    for raw_id, source_key, blob in rows:
        try:
            record = json.loads(zlib.decompress(blob))
        except Exception as exc:  # noqa: BLE001 - one bad record must not block the rest of the batch
            unreadable.append((raw_id, type(exc).__name__, str(exc)))
            continue
        records.append(record)
        raw_id_by_key[source_key] = raw_id
    decoded_records = []
    for source_key, _record, decoded in connect_export.decode_readiness_records(records):
        raw_id = raw_id_by_key.get(source_key)
        if raw_id is None:
            continue  # decoder derived a key with no matching stored record; nothing to update
        decoded_records.append((raw_id, decoded, {"dropped": decoded.dropped, "warnings": decoded.warnings[:10]}))
    return decoded_records, unreadable


def _reparse_readiness(conn: sqlite.Connection, writer: Writer) -> None:
    """Redecode every ``json:readiness`` raw record together and replace the rows each produced."""
    decoded_records, unreadable = _decode_readiness_batch(conn)
    for raw_id, kind, message in unreadable:
        writer.record_parse_failure("json:readiness", f"raw_record:{raw_id}", kind, message, raw_record_id=raw_id)
    for raw_id, decoded, summary in decoded_records:
        writer.rewrite_canonical(raw_id, "json:readiness", decoded, summary)


def rederive_json(conn: sqlite.Connection, writer: Writer, streams: list[str]) -> None:
    """Make the daily rows of the JSON ``streams`` a pure function of the raw records stored now.

    Run at the end of every import and every pull (bet 10b): devices that converged on the
    same raw set must converge on the same days, whatever order the records arrived in. An
    export import passes every JSON stream in the store, not just the ones it wrote this run:
    each record commits on its own, so a retry of an interrupted import sees the finished
    streams as all-duplicate and would never re-derive them. In one
    transaction inside the caller's run (no ``import_runs`` row of its own, no import
    statistics or provenance bumped -- the records were just counted when they were written)
    the canonical rows those streams' raw records produced are deleted and rebuilt record by
    record in the content order of ``_raw_ids_by_stream``, from a clean slate, so a record a
    relay supersession removed stops contributing and a runner-up it hid contributes again.

    ``json:readiness`` is the one batch stream: when it is among ``streams`` it is always
    redecoded together (see ``_decode_readiness_batch``) after its rows were cleared, never
    per record and never over stale rows. Non-JSON names in ``streams`` are ignored; FIT
    streams and the FIT-derived ``local`` rows are untouched (``_delete_canonical`` is scoped
    to the raw records of ``streams``).

    Everything is decoded *before* anything is deleted. A stored record that no longer decodes
    keeps the rows it has and is not reported here (``reparse_all`` is the tool that records
    and repairs such failures): an unrelated import must not lose data or turn ``partial``.
    """
    scope = sorted({stream for stream in streams if stream.startswith("json:")})
    if not scope:
        return
    assumed = writer.offsets.assumed_utc
    writer.offsets = ClockOffsets.load(conn)  # what a reparse would see
    writer.offsets.assumed_utc = assumed
    # a re-derive re-counts nothing the import already counted; cannot trigger today because
    # every JSON fact and sleep record carries a date
    dropped_before = dict(writer.stats.dropped)
    decodable: list[tuple[int, Decoded]] = []
    undecodable: set[int] = set()
    for raw_id, stream in _raw_ids_by_stream(conn, scope):
        if stream in connect_export.BATCH_STREAMS:
            continue
        try:
            decodable.append((raw_id, writer.decode_raw(raw_id)[1]))
        except (ValueError, zlib.error, TypeError, KeyError):
            undecodable.add(raw_id)
    readiness: list[tuple[int, Decoded, dict]] = []
    if "json:readiness" in scope:
        readiness, unreadable = _decode_readiness_batch(conn)
        undecodable.update(raw_id for raw_id, _kind, _message in unreadable)
    conn.execute("BEGIN")
    try:
        _delete_canonical(conn, scope, keep=undecodable)
        for raw_id, decoded in decodable:
            writer._write_canonical(decoded, raw_id)
        for raw_id, decoded, summary in readiness:
            writer._write_canonical(decoded, raw_id)
            conn.execute("UPDATE raw_records SET decode_summary=? WHERE id=?",
                         (json.dumps(summary, sort_keys=True), raw_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        writer.stats.dropped = dropped_before


def _decodable_stream(stream: str) -> bool:
    """A stream ``reparse`` can replay on this build: FIT, a batch stream, or one with a record decoder."""
    return stream.startswith("fit:") or stream in connect_export.BATCH_STREAMS or stream in connect_export.RECORD_DECODERS


def _dry_run(writer: Writer, rows: list[tuple[int, str]]) -> list[dict]:
    """Decode every record in scope without writing; return the failures.

    Runs before anything is cleared so a broken decoder (or a stream nobody
    registered a decoder for) cannot turn a replay into data loss.
    """
    failures = []
    for raw_id, stream in rows:
        try:
            if stream in connect_export.BATCH_STREAMS:
                blob = writer.conn.execute("SELECT payload FROM raw_records WHERE id=?", (raw_id,)).fetchone()[0]
                json.loads(zlib.decompress(blob))
            else:
                writer.decode_raw(raw_id)
        except Exception as exc:  # noqa: BLE001 - every failure is reported, none aborts the check
            failures.append({"file": f"raw_record:{raw_id}", "stream": stream,
                             "kind": getattr(exc, "kind", type(exc).__name__),
                             "error": redact_text(str(exc))})
    return failures


def _delete_canonical(conn: sqlite.Connection, streams: list[str] | None, keep: Collection[int] = frozenset()) -> None:
    """Delete every canonical row the raw records in scope produced; the caller owns the transaction.

    The rows of the raw record ids in ``keep`` are left alone.

    Derived rows (daily steps, source 'local') point at a monitoring raw record
    and are cleared with it; ``derive_daily_steps`` rebuilds them afterwards.
    """
    if streams is None:
        scope_sql, params = "raw_record_id IS NOT NULL", ()
    else:
        marks = ",".join("?" * len(streams))
        scope_sql = f"raw_record_id IN (SELECT id FROM raw_records WHERE stream IN ({marks}))"
        params = tuple(streams)
    if keep:
        scope_sql += f" AND raw_record_id NOT IN ({','.join('?' * len(keep))})"
        params += tuple(keep)
    conn.execute("DELETE FROM sleep_stages WHERE sleep_id IN "
                 f"(SELECT sleep_id FROM sleep_sessions WHERE {scope_sql})", params)
    for table in ("metric_samples", "daily_metrics", "daily_labels", "monitoring_intervals",
                  "activities", "clock_offsets", "sleep_sessions"):
        conn.execute(f"DELETE FROM {table} WHERE {scope_sql}", params)


def _clear_canonical(conn: sqlite.Connection, streams: list[str] | None) -> None:
    """``_delete_canonical`` in one transaction."""
    conn.execute("BEGIN")
    try:
        _delete_canonical(conn, streams)
        conn.execute("COMMIT")
    except sqlite.Error:
        conn.execute("ROLLBACK")
        raise


def _clear_failures(conn: sqlite.Connection, raw_ids: list[int]) -> None:
    """Forget earlier reparse failures of records about to be redecoded; the new pass re-records them."""
    for start in range(0, len(raw_ids), 500):
        chunk = raw_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM import_failures WHERE raw_record_id IN ({marks})", tuple(chunk))


def reparse_all(conn: sqlite.Connection, streams: list[str] | None = None,
                force: bool = False) -> ImportStats:
    """Redecode every retained raw record and replace the canonical rows it produced.

    This is the promise ``raw_records`` retention exists to keep: a decoder
    fix replays bytes already on disk instead of re-pulling them from the
    watch (a sync consumes the watch's files, so a re-pull may simply be
    impossible).

    Raw records are processed in content order (``start_utc, payload_hash, stream, source_key``), FIT records first,
    so a clock offset a FIT reparse corrects is in effect before any
    JSON-stream local date is resolved from it (``ClockOffsets`` is reloaded
    from the database once the FIT pass finishes). ``json:readiness`` is
    never reparsed record-by-record: its canonical value is chosen by
    comparing every readiness raw record in the store to each other (see
    ``connect_export.decode_readiness_records``), so it is redecoded as one
    batch instead, whenever ``streams`` is ``None`` or includes it.

    Before anything is redecoded, every canonical row produced by the raw
    records in scope is deleted in one transaction, and the scope is then
    rebuilt record by record. That makes the outcome independent of
    processing order: a daily key several raw records contribute to (many
    monitoring files per day, for instance) is re-decided by
    ``_upsert_daily``'s "latest observation wins" rule from a clean slate,
    exactly as on a fresh import -- including the case where a decoder fix
    makes the former winner stop emitting the fact. The cost is that a crash
    mid-run leaves the scope partly rebuilt; the raw bytes are untouched, so
    the remedy is simply to run reparse again.

    Every record in scope is decoded once *before* the clearing step. If any
    fails, nothing is touched and the run finishes as ``failed`` with the
    failures listed -- fix the decoder, then reparse. ``force=True`` proceeds
    anyway, accepting that rows of records that fail to decode are lost.

    :param conn: a writable connection already holding the write lock.
    :param streams: restrict to these ``raw_records.stream`` values;
        ``None`` (the default) reparses every stream.
    :param force: rebuild even when the dry decode pass reports failures.
    :returns: stats shaped like an import run, with ``transport="reparse"``.
    """
    writer = Writer(conn, ClockOffsets.load(conn), "reparse")
    writer.begin_run()
    try:
        rows = _raw_ids_by_stream(conn, streams)
        waiting = sorted({stream for _raw_id, stream in rows if not _decodable_stream(stream)})
        if waiting:
            # relayed bytes of a stream this build has no decoder for (relay ``records_kept``): not a
            # broken decoder, so they neither block the replay nor lose their ``import_failures`` trail
            rows = [(raw_id, stream) for raw_id, stream in rows if _decodable_stream(stream)]
            writer.stats.warnings.append(f"records of {', '.join(waiting)} wait for a decoder this build lacks")
        writer.stats.files_seen = len(rows)
        failures = _dry_run(writer, rows)
        if failures and not force:
            writer.stats.failures = failures
            writer.stats.files_failed = len(failures)
            writer.finish_run(error=f"{len(failures)} record(s) fail to decode; nothing was changed "
                                    "(fix the decoder or pass force)")
            return writer.stats
        _clear_canonical(conn, streams)
        _clear_failures(conn, [raw_id for raw_id, _stream in rows])
        fit_ids = [raw_id for raw_id, stream in rows if stream.startswith("fit:")]
        json_ids = [raw_id for raw_id, stream in rows
                   if stream != "json:readiness" and not stream.startswith("fit:")]
        for raw_id in fit_ids:
            writer.reparse_record(raw_id)
        writer.offsets = ClockOffsets.load(conn)  # pick up any offset a FIT reparse just corrected
        for raw_id in json_ids:
            writer.reparse_record(raw_id)
        if streams is None or "json:readiness" in streams:
            _reparse_readiness(conn, writer)
        writer.derive_daily_steps()
        writer.derive_daily_from_samples()
        writer.derive_live_samples()
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller
        writer.finish_run(error=f"{type(exc).__name__}: {exc}")
        raise
    writer.finish_run()
    return writer.stats
