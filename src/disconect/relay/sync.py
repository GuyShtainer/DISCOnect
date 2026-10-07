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
pull raises ``RecordWriteFailed`` and the bundle stays ``applying`` until the next pull reapplies it (so does
a plain record whose write fails with a storage error; a decode failure does not, every device reaches it
identically). The relay's own bytes are never trusted before the AEAD check, and ``Relay.put`` is called from exactly one place in this module with AEAD output.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import secrets
import zlib

from disconect import __version__
from disconect.ingest import connect_export, fit_wellness
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.model import Decoded
from disconect.ingest.sources import rederive_json
from disconect.ingest.writer import DUPLICATE, FAILED, IMPORTED, KEPT, Writer
from disconect.relay import bundle as bundle_module
from disconect.relay import config as relay_config
from disconect.relay.bundle import BundleRejected, account_for, new_name, pack, unpack
from disconect.relay.folder import FolderRelay, Relay
from disconect.storage import parse_iso_utc, sqlite, utc_now_iso

TRANSPORT_RELAY = "relay"
PUSH_BYTES_LIMIT = 8 * 1024 * 1024


@dataclasses.dataclass
class PushResult:
    bundles: list[str] = dataclasses.field(default_factory=list)
    records: int = 0
    ranges: int = 0
    partial: bool = False   # no site could take a bundle (or there is no site): the push stopped there, the rest stays unsent

    def as_dict(self) -> dict:
        return {"bundles": self.bundles, "records": self.records, "ranges": self.ranges}


@dataclasses.dataclass
class PullResult:
    applied: list[str] = dataclasses.field(default_factory=list)
    rejected: dict[str, str] = dataclasses.field(default_factory=dict)   # name -> reason
    records_new: int = 0
    records_duplicate: int = 0
    records_invalid: int = 0
    conflicts: int = 0
    ranges_new: int = 0
    records_repaired: int = 0   # stored copies whose bytes no longer matched their hash, refetched from the relay
    records_kept: int = 0       # bytes of a stream this build has no decoder for, retained for a later reparse
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
#: Never more than this many bundles are re-put on one site in one run (the rest is ``behind``).
HEAL_BUNDLES_PER_SITE = 16
#: Nor more than this many packed bytes (a first bundle over it still goes).
HEAL_BYTES_PER_SITE = 64 * 1024 * 1024


@dataclasses.dataclass
class Site:
    """One open relay of a run; ``report`` is the index of its :class:`SiteReport` (an entry that did not open has a
    report and no site)."""

    id: str
    kind: str
    relay: Relay
    report: int


@dataclasses.dataclass
class SiteReport:
    """What one site did in one run: ``sync.run``'s ``sites`` rows. ``error`` is a stable site word (:func:`site_word`, plus
    the words :func:`open_sites` adds), never a path, an address or an OS text."""

    id: str
    kind: str
    pushed: int = 0
    healed: int = 0
    behind: int = 0
    pulled: int = 0
    rejected: int = 0
    error: str | None = None
    label: str = ""     # the entry's label ("" when it has none): user text, never a path

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "pushed": self.pushed, "healed": self.healed, "behind": self.behind,
                "pulled": self.pulled, "rejected": self.rejected, "error": self.error}

    def fail(self, error: Exception) -> None:
        if self.error is None:
            self.error = site_word(error)


def site_word(error: Exception) -> str:
    """The stable word a site report carries for a failure (the twin of the Rust core's ``RelayError::site_word``; it
    never holds a path, an address or an OS text):

    =======================================================  ===============
    failure                                                  word
    =======================================================  ===============
    ``TooLarge`` (``reason`` ``too_large``)                  ``too_large``
    a bad object name (``ValueError``)                       ``bad_name``
    an error marked ``transient`` (the network relay's)      ``unreachable``
    ``FileNotFoundError``, ``NotADirectoryError``            ``missing``
    ``PermissionError``                                      ``no_permission``
    any other ``OSError`` (or a failed re-pack)              ``io_error``
    =======================================================  ===============

    :func:`open_sites` adds ``unavailable``, ``same_relay``, ``bad_url``, ``unsupported_transport`` and ``not_auto`` (a
    ``lan`` site of an ``auto`` run: skipped by rule, never opened; not a failure of the run). The stored
    ``rejected`` reason of a bundle keeps :func:`reason_of`."""
    if getattr(error, "reason", None) == "too_large":
        return "too_large"
    if isinstance(error, ValueError):
        return "bad_name"
    if getattr(error, "transient", False):
        return "unreachable"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "missing"
    if isinstance(error, PermissionError):
        return "no_permission"
    return "io_error"


def reason_of(error: Exception) -> str:
    """The reason word of a failed relay call: the exception's ``reason`` attribute, else its class name."""
    return getattr(error, "reason", type(error).__name__)


def is_transient(error: Exception) -> bool:
    """The relay, not the object, is the problem: a pull takes that site out of the run and leaves the name pending
    instead of recording it ``rejected``. The twin of the Rust core's ``RelayError::is_transient``, which is true for
    the network relay's unreachable / unauthorized / unverified / status failures only — a folder relay's I/O error
    is never transient on either core. This core has no network relay, so only an error that marks itself
    (``error.transient = True``; the network twin's reason word is ``OSError``) takes the transient path."""
    return bool(getattr(error, "transient", False))


@dataclasses.dataclass
class SiteSpec:
    """A relay to open: from a ``RelayEntry`` or from a per-call ``relays`` element."""

    id: str
    kind: str            # "folder" | "lan"
    value: str           # a folder path or a LAN url ("" for an unavailable per-call entry without one)
    unavailable: bool = False   # the shell could not reach the folder: reported, never opened
    create_root: bool = False   # a missing root is opened anyway and the first put creates it (the entry this device serves)
    label: str = ""             # the entry's label; echoed in the site's report
    check_address: bool = False   # a ``lan`` address must also pass the pairing joiner's class: set for per-call entries
    not_auto: bool = False        # a ``lan`` site of an ``auto`` run (19a): reported ``not_auto``, never opened

    @classmethod
    def from_entry(cls, entry: relay_config.RelayEntry) -> SiteSpec:
        return cls(entry.id, entry.kind, entry.value, False, entry.serve, entry.label, False)


def _folder_identity(root: pathlib.Path) -> tuple[int, int] | None:
    """Who a folder is: the root's (device, inode), so two paths to one folder (a symlink, ``/private/var``) are one relay."""
    try:
        info = os.stat(root)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if os.path.isdir(root) else None


def open_sites(specs: list[SiteSpec], master: bytes) -> tuple[list[Site], list[SiteReport]]:
    """Open every relay of a run. One report per spec, in the specs' order; a spec that cannot be used carries its
    reason word (``unavailable``, ``same_relay``, ``bad_url``, ``unsupported_transport`` for a ``lan`` entry, which
    this core cannot open, ``not_auto`` for a ``lan`` entry of an ``auto`` run) and has no site. Never an error, and nothing is created."""
    sites: list[Site] = []
    reports: list[SiteReport] = []
    folders: list[tuple[int, int]] = []
    urls: list[str] = []
    for spec in specs:
        report = SiteReport(spec.id, spec.kind, label=spec.label)
        word: str | None = None
        relay: Relay | None = None
        if spec.not_auto:
            word = "not_auto"
        elif spec.unavailable:
            word = "unavailable"
        elif spec.kind == "folder":
            root = pathlib.Path(spec.value).expanduser()
            identity = _folder_identity(root)
            if identity is None and spec.create_root and not root.exists():
                relay = FolderRelay(root, create_root=True)
            elif identity is None:
                word = "unavailable"
            elif identity in folders:
                word = "same_relay"
            else:
                folders.append(identity)
                relay = FolderRelay(root, create_root=spec.create_root)
        else:
            base = relay_config.lan_base_url(spec.value)
            if base is None or (spec.check_address and not relay_config.lan_address_class_ok(base)):
                word = "bad_url"
            elif base in urls:
                word = "same_relay"
            else:
                urls.append(base)
                word = "unsupported_transport"
        if relay is not None:
            sites.append(Site(spec.id, spec.kind, relay, len(reports)))
        else:
            report.error = word
        reports.append(report)
    return sites, reports


#: A relay of one call and the index of its report.
Target = tuple[Relay, int]


def _targets(sites: list[Site]) -> list[Target]:
    return [(site.relay, site.report) for site in sites]


def push(conn: sqlite.Connection, master: bytes, relay: Relay) -> PushResult:
    """Bundle every unseen local-origin record and range and put them on the relay (one site, errors raised)."""
    reports = [SiteReport("default", "folder")]
    faults: list[Exception | None] = [None]
    result = _push_core(conn, master, [(relay, 0)], reports, faults)
    if faults[0] is not None:
        raise faults[0]
    return result


def push_all(conn: sqlite.Connection, master: bytes, sites: list[Site], reports: list[SiteReport]) -> PushResult:
    """:func:`push` over every site: a new bundle is packed once and put on each, booked when one took it; then each
    site that is still sound is healed (the names this device pushed that its listing lacks are re-packed and put
    again, within the per-site budget). A site's failure is its report's ``error``, never the run's."""
    return _push_core(conn, master, _targets(sites), reports, [None] * len(sites))


def push_strict(conn: sqlite.Connection, master: bytes, sites: list[Site], reports: list[SiteReport]) -> PushResult:
    """:func:`push_all` for a list of one: the site's first failure is raised as the error, its report is filled all the same."""
    faults: list[Exception | None] = [None] * len(sites)
    result = _push_core(conn, master, _targets(sites), reports, faults)
    first = next((fault for fault in faults if fault is not None), None)
    if first is not None:
        raise first
    return result


def _push_core(conn: sqlite.Connection, master: bytes, relays: list[Target], reports: list[SiteReport],
               faults: list[Exception | None]) -> PushResult:
    result = PushResult()
    _push_new(conn, master, relays, reports, faults, result)
    _heal(conn, master, relays, reports, faults)
    return result


def _push_new(conn: sqlite.Connection, master: bytes, relays: list[Target], reports: list[SiteReport],
              faults: list[Exception | None], result: PushResult) -> None:
    account = account_for(master)
    rows = conn.execute(
        "SELECT r.id, " + ", ".join("r." + c for c in bundle_module.RECORD_COLUMNS) + " FROM raw_records r "
        "LEFT JOIN relay_seen s ON s.raw_record_id = r.id WHERE s.raw_record_id IS NULL ORDER BY r.id").fetchall()
    ranges = conn.execute(
        "SELECT x.id, x.stream, x.from_day, x.to_day FROM export_ranges x "
        "LEFT JOIN relay_seen_ranges s ON s.export_range_id = x.id WHERE s.export_range_id IS NULL ORDER BY x.id").fetchall()
    if not rows and not ranges:
        return
    if all(fault is not None for fault in faults):
        # no site can take a bundle (this also holds for no site at all): nothing is packed, nothing is booked
        result.partial = True
        return
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
        took = 0
        for position, (relay, report) in enumerate(relays):
            if faults[position] is not None:
                continue
            try:
                relay.put(name, data)   # one of two puts in the codebase (the heal's is the other): the argument is AEAD output
            except (OSError, ValueError) as exc:
                reports[report].fail(exc)
                faults[position] = exc
            else:
                took += 1
                reports[report].pushed += 1
        if took == 0:
            # nothing after an unbooked bundle may be booked: prev and seq stay a chain
            result.partial = True
            return
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


def _heal(conn: sqlite.Connection, master: bytes, relays: list[Target], reports: list[SiteReport],
          faults: list[Exception | None]) -> None:
    """What a site is missing: the names this device pushed (in sequence order) that its listing does not show are
    packed again from the stored header and the rows still linked to the name (a record a later conflict retired is
    simply absent) and put under the same name. At most ``HEAL_BUNDLES_PER_SITE`` bundles and ``HEAL_BYTES_PER_SITE``
    packed bytes per site per run; the rest is the report's ``behind``. The re-pack is not byte-identical to the
    first copy (fresh nonce, possibly fewer rows); every reader keys by name, sequence and record identity. Only names
    under the current account are re-packed (after a key rotation whose forget failed, the old account's rows stay);
    the listing is :meth:`FolderRelay.list_present` (an evicted cloud placeholder counts as present); a name that
    cannot be packed is the site's ``io_error``."""
    if all(fault is not None for fault in faults):
        return
    account = account_for(master)
    pushed = conn.execute("SELECT name, device_id, device_seq, prev, created_utc FROM relay_bundles "
                          "WHERE direction='pushed' AND status='applied' AND name LIKE ? ORDER BY device_seq",
                          (account + "/%",)).fetchall()
    if not pushed:
        return
    for position, (relay, report) in enumerate(relays):
        if faults[position] is not None:
            continue
        try:
            listing = set(getattr(relay, "list_present", relay.list)(account))
        except (OSError, ValueError) as exc:
            reports[report].fail(exc)
            faults[position] = exc
            continue
        missing = [row for row in pushed if row[0] not in listing]
        spent = 0
        for at, (name, device_id, seq, prev, created) in enumerate(missing):
            rest = len(missing) - at
            if reports[report].healed >= HEAL_BUNDLES_PER_SITE:
                reports[report].behind = rest
                break
            records, ranges = _linked_rows(conn, name)
            header = {"format": bundle_module.FORMAT_VERSION, "core": __version__, "device_id": device_id,
                      "device_seq": seq, "prev": prev, "created_utc": created,
                      "records": len(records), "ranges": len(ranges)}
            try:
                data = _seal(master, name, header, records, ranges)
            except Exception:   # one name that cannot be packed is this site's io_error, never the run's
                error = OSError("pack")
                reports[report].fail(error)
                faults[position] = error
                break
            if reports[report].healed > 0 and spent + len(data) > HEAL_BYTES_PER_SITE:
                reports[report].behind = rest
                break
            try:
                relay.put(name, data)
            except (OSError, ValueError) as exc:
                reports[report].fail(exc)
                faults[position] = exc
                break
            spent += len(data)
            reports[report].healed += 1


def _linked_rows(conn: sqlite.Connection, name: str) -> tuple[list[dict], list[dict]]:
    """The records and ranges still linked to the pushed bundle ``name`` (``relay_seen``, ``relay_seen_ranges``), in the
    order a push packs them."""
    records = [dict(zip(bundle_module.RECORD_COLUMNS, row)) for row in conn.execute(
        "SELECT " + ", ".join("r." + c for c in bundle_module.RECORD_COLUMNS) + " FROM raw_records r "
        "JOIN relay_seen s ON s.raw_record_id = r.id WHERE s.bundle = ? ORDER BY r.id", (name,)).fetchall()]
    ranges = [{"stream": r[0], "from_day": r[1], "to_day": r[2]} for r in conn.execute(
        "SELECT x.stream, x.from_day, x.to_day FROM export_ranges x "
        "JOIN relay_seen_ranges s ON s.export_range_id = x.id WHERE s.bundle = ? ORDER BY x.id", (name,)).fetchall()]
    return records, ranges


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


def _has_decoder(stream: str) -> bool:
    """This build can decode records of ``stream`` (a batch stream or a registered per-record decoder)."""
    return stream in connect_export.BATCH_STREAMS or stream in connect_export.RECORD_DECODERS


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


class RecordWriteFailed(RuntimeError):
    """A pulled record could not be stored (a storage error, not a decode failure). Whatever the write
    covered was rolled back -- for a conflict winner that includes the retirement of the losing record,
    which is still in place. The bundle stays ``applying`` so the next pull applies it again."""


def _decide_conflict(conn: sqlite.Connection, record: dict, incoming: Decoded | None) -> _Conflict | None:
    """Same key, different bytes -> who wins. ``None`` when no stored row differs from the incoming bytes
    (no row at all, or the very same bytes): nothing to decide. ``incoming=None`` is a record of a stream
    this build cannot decode: neither side has a time, so the rule is the hash alone (as for a batch
    stream) -- the one rule a device without the decoder can apply."""
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
    if incoming is None or record["stream"] in connect_export.BATCH_STREAMS:
        # a batch stream's record carries no time of its own (the stored row's span is not
        # the facts' time), so the protocol's rule is the hash alone -- both sides must agree;
        # a stream with no decoder here has no time either
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
        if conn.in_transaction:   # RAISE(ROLLBACK) and I/O errors have already ended it
            conn.execute("ROLLBACK")
        raise


def _conflict_recorded(conn: sqlite.Connection, bundle_name: str, record: dict) -> bool:
    """This bundle's decision on this record is already in ``sync_conflicts`` (a reapplied bundle)."""
    return conn.execute("SELECT 1 FROM sync_conflicts WHERE bundle=? AND source_key=? AND stream=?",
                        (bundle_name, record["source_key"], record["stream"])).fetchone() is not None


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


def _repair_damaged(conn: sqlite.Connection, master: bytes, relays: list[Target], result: PullResult) -> None:
    """Every stored record the relay has carried (``relay_seen``) is checked against its hash; a copy whose
    bytes no longer inflate to it is refetched from the bundle that carried it and the streams it feeds are
    re-derived. Without this a damaged copy stays damaged forever: a peer never pushes a pulled record back,
    and the conflict rule, meeting bytes that do not decode, would hand the key to whatever arrives next.
    The first site whose copy gets and unpacks is the one used. A bundle no site has leaves the record as it
    is (nothing fails)."""
    damaged: dict[str, list[tuple[int, str, str]]] = {}
    for raw_id, payload, digest, stream, bundle_name in conn.execute(
            "SELECT r.id, r.payload, r.payload_hash, r.stream, s.bundle FROM raw_records r "
            "JOIN relay_seen s ON s.raw_record_id = r.id ORDER BY r.id").fetchall():
        try:
            intact = hashlib.sha256(zlib.decompress(payload)).hexdigest() == digest
        except zlib.error:
            intact = False
        if not intact:
            damaged.setdefault(bundle_name, []).append((raw_id, digest, stream))
    streams: set[str] = set()
    for bundle_name, wanted in damaged.items():
        found = None
        for relay, _report in relays:
            try:
                found = unpack(master, bundle_name, relay.get(bundle_name))
                break
            except (BundleRejected, OSError, ValueError):
                continue
        if found is None:
            continue
        _header, records, _ranges = found
        by_hash = {r["payload_hash"]: r for r in records if _verify(r) is not None}
        for raw_id, digest, stream in wanted:
            record = by_hash.get(digest)
            if record is None:
                continue
            conn.execute("UPDATE raw_records SET payload=? WHERE id=?", (record["payload"], raw_id))
            conn.execute("DELETE FROM import_failures WHERE raw_record_id=?", (raw_id,))
            result.records_repaired += 1
            streams.add(stream)
    if streams:
        conn.commit()
        from disconect.ingest import sources  # noqa: PLC0415 - avoid an import cycle at module load
        sources.reparse_all(conn, streams=sorted(streams), force=True)


def pull(conn: sqlite.Connection, master: bytes, relay: Relay) -> PullResult:
    """:func:`pull_all` over one site, its failure raised as an error (the CLI's ``--relay``, pairing, tests)."""
    reports = [SiteReport("default", "folder")]
    faults: list[Exception | None] = [None]
    result = _pull_core(conn, master, [(relay, 0)], reports, faults)
    if faults[0] is not None:
        raise faults[0]
    return result


def pull_all(conn: sqlite.Connection, master: bytes, sites: list[Site], reports: list[SiteReport]) -> PullResult:
    """Fetch and apply every bundle any site lists that this store has not applied: the union of the listings, each
    name tried on every site that lists it, in site order, until one unpacks (``rejected`` only when all fail). A
    site that cannot be listed contributes nothing; a transient failure skips that site for the rest of the run and
    leaves its names pending (the run is ``partial``). Failures are the reports' ``error``, never the run's."""
    return _pull_core(conn, master, _targets(sites), reports, [None] * len(sites))


def pull_strict(conn: sqlite.Connection, master: bytes, sites: list[Site], reports: list[SiteReport]) -> PullResult:
    """:func:`pull_all` for a list of one: the site's first failure is raised as the error, its report is filled all the same."""
    faults: list[Exception | None] = [None] * len(sites)
    result = _pull_core(conn, master, _targets(sites), reports, faults)
    first = next((fault for fault in faults if fault is not None), None)
    if first is not None:
        raise first
    return result


def _pull_core(conn: sqlite.Connection, master: bytes, relays: list[Target], reports: list[SiteReport],
               faults: list[Exception | None]) -> PullResult:
    """Stored copies the relays carried are verified first and repaired from them when damaged (``records_repaired``)."""
    account = account_for(master)
    result = PullResult()
    _repair_damaged(conn, master, relays, result)
    known = {row[0] for row in conn.execute("SELECT name FROM relay_bundles WHERE status='applied'").fetchall()}
    half_applied = {row[0] for row in conn.execute("SELECT name FROM relay_bundles WHERE status='applying'").fetchall()}
    # the union of the listings in first-seen order (site order, then listing order), each name with its sites
    pending: dict[str, list[int]] = {}
    for position, (relay, report) in enumerate(relays):
        try:
            listing = relay.list(account)
        except (OSError, ValueError) as exc:
            reports[report].fail(exc)
            faults[position] = exc
            continue
        for name in listing:
            if name not in known:
                pending.setdefault(name, []).append(position)
    if any(fault is not None for fault in faults):
        result.status = "partial"
    if not pending:
        _report_gaps(conn, result)
        return result
    writer = Writer(conn, ClockOffsets.load(conn), TRANSPORT_RELAY)
    writer.begin_run()
    applied: list[tuple] = []
    reparse_streams: set[str] = set()
    try:
        skipped = [False] * len(relays)   # sites a transient failure took out of this run
        for name, holders in pending.items():
            fetched = None
            failed = 0
            first_reason: str | None = None
            for position in holders:
                if skipped[position]:
                    continue
                relay, report = relays[position]
                try:
                    blob = relay.get(name)
                except (OSError, ValueError) as exc:
                    if is_transient(exc):
                        # the relay, not the object, is the problem: this site is out for the run, the name stays
                        # pending if no other site has it
                        skipped[position] = True
                        reports[report].fail(exc)
                        if faults[position] is None:
                            faults[position] = exc
                        continue
                    reports[report].rejected += 1
                    failed += 1
                    if first_reason is None:
                        first_reason = reason_of(exc)
                    continue
                try:
                    unpacked = unpack(master, name, blob)
                except BundleRejected as exc:
                    reports[report].rejected += 1
                    failed += 1
                    if first_reason is None:
                        first_reason = reason_of(exc)
                    continue
                reports[report].pulled += 1
                fetched = (blob, unpacked)
                break
            if fetched is None:
                if failed == len(holders):
                    reason = first_reason or ""
                    result.rejected[name] = reason
                    writer.stats.files_failed += 1
                    conn.execute("INSERT OR REPLACE INTO relay_bundles(name, direction, status, noted_at, reason) "
                                 "VALUES(?, 'pulled', 'rejected', ?, ?)", (name, utc_now_iso(), reason))
                else:
                    # a site that may hold a good copy was out of reach: nothing is booked, the next pull retries
                    result.status = "partial"
                continue
            blob, (header, records, ranges) = fetched
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
                if outcome == FAILED and writer.last_failure_is_storage():
                    raise RecordWriteFailed(f"{record['stream']} file not stored ({outcome}); nothing it wrote is kept")
                _mark(conn, writer, before, name, outcome, result)
                if outcome == DUPLICATE:
                    _mark_existing(conn, record, name)
            for record, data in jsons:
                known = _has_decoder(record["stream"])
                if known:
                    try:
                        decoded = _decode_json(record["stream"], data, record)
                    except (ValueError, TypeError):
                        decoded = None
                    if decoded is None:
                        result.records_invalid += 1
                        continue
                    record_dict, decoded_facts = decoded
                else:
                    # a stream a newer build published: the bytes are kept (no canonical rows) for the
                    # day a decoder arrives -- dropping them would lose them for good
                    record_dict, decoded_facts = None, None
                conflict = _decide_conflict(conn, record, decoded_facts)
                hook = None
                if conflict is not None and not conflict.incoming_wins:
                    if not _conflict_recorded(conn, name, record):   # a reapplied bundle decided this already
                        _record_incoming_loser(conn, record, conflict, name)
                    result.conflicts += 1
                    continue
                if conflict is not None:
                    hook = _loser_writes(conn, writer, record, conflict, name)
                elif conn.execute("SELECT 1 FROM raw_records WHERE stream=? AND source_key=?",
                                  (record["stream"], record["source_key"])).fetchone():
                    # the same bytes are already here (a local import or an earlier bundle)
                    if _conflict_recorded(conn, name, record):
                        # a reapplied bundle: the winner is stored, its relay_seen row may not be
                        result.conflicts += 1
                        _mark_existing(conn, record, name)
                    else:
                        result.records_duplicate += 1
                        _mark_existing(conn, record, name)
                    continue
                before = writer.last_raw_id()
                if known:
                    outcome = writer.write_json_record(record["stream"], record["source_key"], record_dict, decoded_facts,
                                                       f"relay:{name[-8:]}", origin=(record["transport"], record["imported_at"]),
                                                       before_write=hook)
                else:
                    outcome = writer.keep_undecodable_record(record["stream"], record["source_key"], record, data,
                                                             f"relay:{name[-8:]}",
                                                             origin=(record["transport"], record["imported_at"]),
                                                             before_write=hook)
                if outcome == FAILED and writer.last_failure_is_storage() or hook is not None and outcome not in (IMPORTED, KEPT):
                    raise RecordWriteFailed(f"{record['stream']} record not stored ({outcome}); nothing it wrote is kept")
                if hook is not None:
                    result.conflicts += 1
                _mark(conn, writer, before, name, outcome, result)
                if hook is not None:
                    # the winner may have reused the retired loser's rowid (``id > before`` misses it)
                    _mark_existing(conn, record, name)
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
        writer.derive_live_samples()
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
    if outcome in (IMPORTED, KEPT):
        if outcome == IMPORTED:
            result.records_new += 1
        else:
            result.records_kept += 1
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


#: The content view of the conflict history: one row per version of a record that lost, whatever the
#: arrival order and however many bundles carried it. ``sync_conflicts`` itself is the per-bundle journal
#: (it keeps a reapplied bundle idempotent), so its row count depends on arrival -- never compare that.
CONFLICTS_BY_CONTENT = "SELECT count(*) FROM (SELECT DISTINCT stream, source_key, loser_hash FROM sync_conflicts)"
SUPERSEDED_BY_CONTENT = "SELECT count(*) FROM (SELECT DISTINCT stream, source_key, payload_hash FROM raw_superseded)"


def status(conn: sqlite.Connection) -> dict:
    """Counts, plus the time of the newest applied bundle in each direction: bundles pushed/pulled/rejected,
    records seen, conflicts, chain gaps. ``conflicts`` and ``superseded`` count by content (the versions that
    lost), so converged devices report the same numbers."""
    counts = {row[0] + "_" + row[1]: row[2] for row in conn.execute(
        "SELECT direction, status, count(*) FROM relay_bundles GROUP BY 1, 2").fetchall()}
    unsent = conn.execute("SELECT count(*) FROM raw_records r LEFT JOIN relay_seen s ON s.raw_record_id=r.id "
                          "WHERE s.raw_record_id IS NULL").fetchone()[0]
    result = PullResult()
    _report_gaps(conn, result)
    # the newest applied bundle per direction (``noted_at`` of this store's own booking): a push or pull that
    # moved nothing books no bundle, so these are "last data sent / received", not "last attempt"
    last = {row[0]: row[1] for row in conn.execute(
        "SELECT direction, max(noted_at) FROM relay_bundles WHERE status='applied' GROUP BY 1").fetchall()}
    return {"bundles": counts, "records_unsent": unsent,
            "records_seen": conn.execute("SELECT count(*) FROM relay_seen").fetchone()[0],
            "conflicts": conn.execute(CONFLICTS_BY_CONTENT).fetchone()[0],
            "superseded": conn.execute(SUPERSEDED_BY_CONTENT).fetchone()[0],
            "gaps": result.gaps, "last_pushed_at": last.get("pushed"), "last_pulled_at": last.get("pulled")}


def forget_relay_state(conn: sqlite.Connection) -> None:
    """After a master rotation: a new account and key, so every record is pushed again under them.
    Conflict and superseded history is kept (it is local history, not relay state)."""
    for table in ("relay_bundles", "relay_seen", "relay_seen_ranges", "relay_device"):
        conn.execute(f"DELETE FROM {table}")
