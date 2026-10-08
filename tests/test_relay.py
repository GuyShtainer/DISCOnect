"""The blind relay: bundle crypto, folder adapter, push/pull through the Writer, convergence, operator view."""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import secrets
import zlib

import pytest
from cryptography.exceptions import InvalidTag

from disconect import coverage, storage
from disconect.ingest import sources
from disconect.relay import bundle, sync
from disconect.relay.folder import NAME, FolderRelay
from test_import import OFFSET, UTC, _build_export, _monitoring_day, _readiness, _uds

MASTER = bytes(range(32))
OTHER = bytes(range(1, 33))


def _store(path: pathlib.Path) -> None:
    with storage.open_for_write(path, "test"):
        pass


def _import(db_path: pathlib.Path, source: pathlib.Path):
    with storage.open_for_write(db_path, "test") as conn:
        return sources.import_path(source, conn)


def _push(db_path, relay, master=MASTER):
    with storage.open_for_write(db_path, "sync") as conn:
        return sync.push(conn, master, relay)


def _pull(db_path, relay, master=MASTER):
    with storage.open_for_write(db_path, "sync") as conn:
        return sync.pull(conn, master, relay)


def _keys(db_path) -> set[tuple[str, str, str]]:
    conn = storage.open_read_only(db_path)
    try:
        return set(conn.execute("SELECT stream, source_key, payload_hash FROM raw_records").fetchall())
    finally:
        conn.close()


def _fingerprint(db_path) -> dict:
    """Counts a second device must reproduce: raw keys, ranges, daily rows per (metric, scope), ledger."""
    conn = storage.open_read_only(db_path)
    try:
        return {
            "raw": set(conn.execute("SELECT stream, source_key, payload_hash, source_scope, transport, device_id, start_utc, end_utc, "
                                    "payload_bytes, imported_at FROM raw_records").fetchall()),
            "ranges": set(conn.execute("SELECT stream, from_day, to_day FROM export_ranges").fetchall()),
            "daily": set(conn.execute("SELECT metric, source_scope, count(*) FROM daily_metrics GROUP BY 1, 2").fetchall()),
            "samples": conn.execute("SELECT count(*) FROM metric_samples").fetchone()[0],
            "sleep": conn.execute("SELECT count(*) FROM sleep_sessions").fetchone()[0],
            "ledger": {(m, s): coverage.day_statuses(conn, m, s, "2025-06-13", "2025-06-18")
                       for m, s in (("steps", "local"), ("steps", "vendor_cloud"), ("sleep_score", "device"),
                                    ("resting_heart_rate", "vendor_cloud"), ("training_readiness", "vendor_cloud"))},
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- bundle format
def test_padme_rounds_up_with_a_floor():
    assert bundle.padme(10) == 64 * 1024 and bundle.padme(64 * 1024) == 64 * 1024
    assert bundle.padme(64 * 1024 + 1) > 64 * 1024 + 1
    big = bundle.padme(1_000_003)
    assert big >= 1_000_003 and (big - 1_000_003) / 1_000_003 < 0.07, "Padmé overhead stays small"


def test_round_trip_and_every_defect_is_rejected():
    name = bundle.new_name(bundle.account_for(MASTER))
    record = {"stream": "fit:monitoring_b", "source_key": "a" * 64, "source_scope": "device", "transport": "usb",
              "device_id": "7", "start_utc": None, "end_utc": None, "payload_kind": "fit", "payload": zlib.compress(b"x"),
              "payload_hash": "a" * 64, "payload_bytes": 1, "imported_at": "2025-07-01T00:00:00Z"}
    header = {"format": 1, "device_id": "d", "device_seq": 1, "prev": None, "created_utc": "2025-07-01T00:00:00Z"}
    blob = bundle.pack(MASTER, name, header, [record], [{"stream": "json:uds", "from_day": "2025-06-01", "to_day": "2025-06-30"}])
    assert len(blob) == 1 + 32 + 12 + 16 + 64 * 1024, "a tiny bundle pads to the 64 KiB floor plus the envelope"
    out_header, records, ranges = bundle.unpack(MASTER, name, blob)
    assert out_header["device_seq"] == 1 and records == [record] and ranges[0]["stream"] == "json:uds"
    with pytest.raises(bundle.BundleRejected, match="authentication_failed"):
        bundle.unpack(OTHER, name, blob)
    with pytest.raises(bundle.BundleRejected, match="authentication_failed"):
        bundle.unpack(MASTER, bundle.new_name(bundle.account_for(MASTER)), blob)   # renamed object
    flipped = bytearray(blob)
    flipped[200] ^= 0x01
    with pytest.raises(bundle.BundleRejected, match="authentication_failed"):
        bundle.unpack(MASTER, name, bytes(flipped))
    with pytest.raises(bundle.BundleRejected, match="truncated|authentication_failed"):
        bundle.unpack(MASTER, name, blob[:-40])
    with pytest.raises(bundle.BundleRejected, match="unknown_format"):
        bundle.unpack(MASTER, name, b"\x02" + blob[1:])
    with pytest.raises(ValueError):
        bundle.pack(OTHER, name, header, [], [])   # a name under someone else's account


def test_folder_relay_ignores_strangers_and_temp_files(tmp_path):
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    account = bundle.account_for(MASTER)
    name = bundle.new_name(account)
    relay.put(name, b"abc")
    (tmp_path / "relay" / account / ".tmp-deadbeef").write_bytes(b"partial")
    (tmp_path / "relay" / account / "README.txt").write_text("hi")
    assert relay.list(account) == [name] and relay.get(name) == b"abc"
    assert relay.list(bundle.account_for(OTHER)) == []
    with pytest.raises(ValueError):
        relay.put("../escape", b"x")
    assert all(NAME.match(n) for n in relay.list(account))


# ---------------------------------------------------------------- push / pull
def test_folder_relay_delete_removes_one_object_and_validates_the_name(tmp_path):
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    account = "ab" * 32
    one, two = f"{account}/{'a' * 32}", f"{account}/{'b' * 32}"
    relay.put(one, b"1")
    relay.put(two, b"2")
    relay.delete(one)
    assert relay.list(account) == [two] and relay.get(two) == b"2"
    with pytest.raises(FileNotFoundError):
        relay.delete(one)
    with pytest.raises(ValueError, match="bad object name"):
        relay.delete("../escape")
    assert relay.get(two) == b"2", "a refused name touches nothing"


def test_two_desktops_with_split_data_converge_and_nothing_echoes(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    _build_export(export)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "x.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 16, 21, 0, tzinfo=UTC), 3000, serial=7))
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, export)
    _import(b, extra)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    pa = _push(a, relay)
    pb = _push(b, relay)
    assert len(pa.bundles) == 1 and pa.records > 0 and pa.ranges == 2, "UDS + sleep windows (the readiness file has none)"
    assert len(pb.bundles) == 1 and pb.records == 1
    ra = _pull(a, relay)
    rb = _pull(b, relay)
    assert ra.status == "ok" and rb.status == "ok"
    assert ra.records_new == 1 and ra.records_duplicate == 0, "A applies only B's record; its own bundle is known"
    assert rb.records_new == pa.records and rb.ranges_new == 2
    fa, fb = _fingerprint(a), _fingerprint(b)
    assert fa == fb, "identical raw keys, ranges, daily counts, samples, sleep and coverage ledger"
    # no echo: a second push from either side sends nothing
    assert _push(a, relay).bundles == [] and _push(b, relay).bundles == []
    # idempotent: a re-pull changes nothing
    again = _pull(a, relay)
    assert again.applied == [] and again.records_new == 0 and _fingerprint(a) == fa
    # the incremental apply equals a full reparse on B
    with storage.open_for_write(b, "test") as conn:
        sources.reparse_all(conn)
    assert _fingerprint(b) == fb
    # relay-origin records keep their origin transport, and the pull is one 'relay' import run
    conn = storage.open_read_only(b)
    transports = {r[0] for r in conn.execute("SELECT DISTINCT transport FROM raw_records").fetchall()}
    assert transports == {"connect_export", "drop"}
    assert conn.execute("SELECT count(*) FROM import_runs WHERE transport='relay' AND status='ok'").fetchone()[0] == 1
    conn.close()


def test_late_and_out_of_order_bundles_are_applied_and_rejected_ones_retried(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "1.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC), 1000))
    _import(a, folder)
    _store(b)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    first = _push(a, relay).bundles[0]
    account = bundle.account_for(MASTER)
    # a stranger's garbage object and a truncated copy arrive before B pulls
    garbage = bundle.new_name(account)
    relay.put(garbage, secrets.token_bytes(70_000))
    r = _pull(b, relay)
    assert r.applied == [first] and set(r.rejected) == {garbage} and r.status == "partial"
    # a late bundle (older data, pushed after) is applied on the next pull; the garbage is retried and rejected again: pull() never holds (only the desktop auto run does)
    (folder / "0.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 12, 21, 0, tzinfo=UTC), 500))
    _import(a, folder)
    second = _push(a, relay).bundles[0]
    r2 = _pull(b, relay)
    assert r2.applied == [second] and set(r2.rejected) == {garbage}
    assert _keys(a) == _keys(b)
    with storage.open_for_write(b, "test") as conn:
        report = sync.status(conn)
    assert report["bundles"] == {"pulled_applied": 2, "pulled_rejected": 1} and report["gaps"] == []
    # the rejected object adds no chain row; A's chain is the one row, and it is not B's own
    (chain,) = report["chains"]
    assert (chain["bundles"], chain["last_seq"], chain["self"]) == (2, 2, False)
    assert chain["last_at"] is not None


def test_conflict_rule_is_order_independent_and_keeps_the_loser(tmp_path):
    early = _uds("2025-06-15", 4000, 50)
    early["wellnessEndTimeGmt"] = "2025-06-15T12:00:00.0"
    late = _uds("2025-06-15", 8000, 50)
    late["wellnessEndTimeGmt"] = "2025-06-15T21:00:00.0"

    def export_with(root: pathlib.Path, record: dict) -> None:
        agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
        agg.mkdir(parents=True)
        (agg / "UDSFile_2025-06-15_2025-06-15.json").write_text(json.dumps([record]))
        (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()

    outcomes = []
    for order, (first, second) in enumerate(((early, late), (late, early))):
        base = tmp_path / f"o{order}"
        x, y = base / "x", base / "y"
        export_with(x, first)
        export_with(y, second)
        a, b = base / "a.db", base / "b.db"
        _import(a, x)
        _import(b, y)
        relay = FolderRelay(base / "relay", create_root=True)
        _push(a, relay)
        _push(b, relay)
        ra, rb = _pull(a, relay), _pull(b, relay)
        assert ra.conflicts + rb.conflicts >= 1
        conn_a, conn_b = storage.open_read_only(a), storage.open_read_only(b)
        steps_a = conn_a.execute("SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'").fetchone()[0]
        steps_b = conn_b.execute("SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'").fetchone()[0]
        superseded = conn_a.execute("SELECT count(*) FROM raw_superseded").fetchone()[0] + conn_b.execute("SELECT count(*) FROM raw_superseded").fetchone()[0]
        rule = {r[0] for r in conn_a.execute("SELECT rule FROM sync_conflicts").fetchall()} | {r[0] for r in conn_b.execute("SELECT rule FROM sync_conflicts").fetchall()}
        conn_a.close(), conn_b.close()
        assert steps_a == steps_b == 8000, "the later observation wins on both, whatever the arrival order"
        assert superseded == 2 and rule == {"observed_utc"}, "each store keeps the loser it saw"
        outcomes.append(_keys(a))
        assert _keys(a) == _keys(b)
    assert outcomes[0] == outcomes[1]


def test_readiness_conflicts_converge_by_hash_alone(tmp_path):
    """A batch-stream record has no observed time of its own; two devices holding different bytes
    under one readiness key must pick the same winner (the larger hash) and then stay quiet."""
    first = _readiness("2025-06-15", "2025-06-15T04:00:00.0", "AFTER_WAKEUP_RESET", 70)
    second = dict(first, score=75)

    def export_with(root: pathlib.Path, record: dict) -> None:
        metrics = root / "DI_CONNECT" / "DI-Connect-Metrics"
        metrics.mkdir(parents=True)
        (metrics / "TrainingReadinessDTO_111_111_111.json").write_text(json.dumps([record]))
        (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()

    for order, (x_rec, y_rec) in enumerate(((first, second), (second, first))):
        base = tmp_path / f"o{order}"
        export_with(base / "x", x_rec)
        export_with(base / "y", y_rec)
        a, b = base / "a.db", base / "b.db"
        _import(a, base / "x")
        _import(b, base / "y")
        relay = FolderRelay(base / "relay", create_root=True)
        _push(a, relay), _push(b, relay)
        ra, rb = _pull(a, relay), _pull(b, relay)
        assert ra.conflicts == rb.conflicts == 1, "both sides record the one decision"
        conn_a, conn_b = storage.open_read_only(a), storage.open_read_only(b)
        hashes = [c.execute("SELECT payload_hash FROM raw_records WHERE stream='json:readiness'").fetchall() for c in (conn_a, conn_b)]
        scores = [c.execute("SELECT value FROM daily_metrics WHERE metric='training_readiness'").fetchall() for c in (conn_a, conn_b)]
        rules = {r[0] for c in (conn_a, conn_b) for r in c.execute("SELECT rule FROM sync_conflicts").fetchall()}
        conn_a.close(), conn_b.close()
        assert hashes[0] == hashes[1] and len(hashes[0]) == 1 and scores[0] == scores[1]
        canonical = [hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest() for r in (first, second)]
        assert hashes[0][0][0] == max(canonical), "the larger hash wins on both sides"
        assert rules == {"payload_hash"}
        # a second round must find nothing to swap: the winner is a fixed point
        _push(a, relay), _push(b, relay)
        ra2, rb2 = _pull(a, relay), _pull(b, relay)
        assert ra2.conflicts == rb2.conflicts == 0 and ra2.records_new == rb2.records_new == 0
        assert _keys(a) == _keys(b)


def test_relay_ignores_an_object_whose_name_ends_in_a_newline(tmp_path):
    """``re.match`` with ``$`` accepted ``<name>\\n``; a stranger with folder access could make every
    pull report a rejected bundle. The name must match in full."""
    root = tmp_path / "x"
    _build_export(root)
    a = tmp_path / "a.db"
    _import(a, root)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay)
    account = next((tmp_path / "relay").iterdir())
    stray = account / (secrets.token_hex(16) + "\n")
    stray.write_bytes(b"\x00" * 64)
    b = tmp_path / "b.db"
    _import(b, root)
    result = _pull(b, relay)
    assert result.rejected == {} and result.status == "ok", "the stray is not an object at all"
    assert len(relay.list(account.name)) == 1


def test_fit_bytes_under_another_stream_label_are_one_record(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "1.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC), 1000))
    _import(a, folder)
    _import(b, folder)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    # an older core on A labelled the same bytes differently
    with storage.open_for_write(a, "test") as conn:
        conn.execute("UPDATE raw_records SET stream='fit:legacy'")
    _push(a, relay)
    r = _pull(b, relay)
    assert r.records_new == 0 and r.records_duplicate == 1
    conn = storage.open_read_only(b)
    assert conn.execute("SELECT count(*) FROM raw_records").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM relay_seen").fetchone()[0] == 1, "marked: B will never push it back"
    conn.close()
    assert _push(b, relay).bundles == []


def _export_with_uds(root: pathlib.Path, record: dict) -> None:
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir(parents=True)
    (agg / f"UDSFile_{record['calendarDate']}_{record['calendarDate']}.json").write_text(json.dumps([record]))
    (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()


def _relabel(db_path: pathlib.Path, old: str, new: str) -> None:
    """Simulate a newer build on this device: its records travel under a stream this build does not know."""
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("UPDATE raw_records SET stream=? WHERE stream=?", (new, old))


def test_a_record_of_a_stream_this_build_cannot_decode_is_kept_for_a_later_decoder(tmp_path, monkeypatch):
    from disconect.ingest import connect_export
    x = tmp_path / "x"
    _export_with_uds(x, _uds("2025-06-15", 4000, 50))
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, x)
    _relabel(a, "json:uds", "json:future")
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay)
    _store(b)
    r = _pull(b, relay)
    assert (r.records_new, r.records_invalid, r.records_kept, r.status) == (0, 0, 1, "ok")
    conn = storage.open_read_only(b)
    row = conn.execute("SELECT id, stream, source_key, payload_hash, source_scope, transport, device_id, start_utc, end_utc, "
                       "decode_summary FROM raw_records").fetchone()
    assert row[1:3] == ("json:future", "2025-06-15") and row[4:6] == ("vendor_cloud", "connect_export")
    assert row[7] and row[8], "the sender's span travels with the bytes, so the ledger can place the failure"
    assert json.loads(row[9]) == {"kept": "no_decoder"}
    assert conn.execute("SELECT count(*) FROM daily_metrics").fetchone()[0] == 0, "nothing derived from bytes nobody decoded"
    failure = conn.execute("SELECT raw_record_id, stream, kind, payload_hash FROM import_failures").fetchone()
    assert tuple(failure) == (row[0], "json:future", "unrecognized_payload", row[3])
    assert conn.execute("SELECT direction FROM relay_seen WHERE raw_record_id=?", (row[0],)).fetchone()[0] == "pulled"
    conn.close()
    assert _push(b, relay).bundles == [], "a pulled record is never pushed back, kept or not"
    assert _pull(b, relay).records_kept == 0, "idempotent: the bundle is applied once"
    # a replay on this build leaves the record waiting and the ledger entry in place; it is not a failed run
    with storage.open_for_write(b, "test") as conn:
        stats = sources.reparse_all(conn)
    assert stats.files_failed == 0 and any("json:future" in w for w in stats.warnings)
    conn = storage.open_read_only(b)
    assert conn.execute("SELECT count(*) FROM import_failures").fetchone()[0] == 1
    conn.close()
    # the decoder arrives (a later build): the kept bytes become facts, the failure row goes
    monkeypatch.setitem(connect_export.RECORD_DECODERS, "json:future", connect_export.decode_uds_record)
    with storage.open_for_write(b, "test") as conn:
        stats = sources.reparse_all(conn)
    assert stats.files_failed == 0
    conn = storage.open_read_only(b)
    steps = conn.execute("SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'").fetchone()
    assert steps[0] == 4000.0
    assert conn.execute("SELECT count(*) FROM import_failures").fetchone()[0] == 0
    conn.close()


def test_two_versions_of_an_undecodable_record_converge_by_hash_in_both_orders(tmp_path):
    early = _uds("2025-06-15", 4000, 50)
    late = _uds("2025-06-15", 8000, 50)
    late["wellnessEndTimeGmt"] = "2025-06-15T23:00:00.0"
    kept = []
    for order, (first, second) in enumerate(((early, late), (late, early))):
        base = tmp_path / f"o{order}"
        x, y = base / "x", base / "y"
        _export_with_uds(x, first)
        _export_with_uds(y, second)
        a, b, c = base / "a.db", base / "b.db", base / "c.db"
        _import(a, x)
        _import(b, y)
        _relabel(a, "json:uds", "json:future")
        _relabel(b, "json:uds", "json:future")
        relay = FolderRelay(base / "relay", create_root=True)
        _push(a, relay)
        _push(b, relay)
        _store(c)
        rc = _pull(c, relay)
        assert rc.conflicts == 1 and rc.records_kept >= 1, "a winner that replaced the stored loser counts as kept too"
        conn = storage.open_read_only(c)
        assert conn.execute("SELECT count(*) FROM raw_records").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM raw_superseded").fetchone()[0] == 1, "the loser's bytes are kept too"
        assert {r[0] for r in conn.execute("SELECT rule FROM sync_conflicts").fetchall()} == {"payload_hash"}
        assert conn.execute("SELECT count(*) FROM import_failures").fetchone()[0] == 1, "one trail for the record that stayed"
        conn.close()
        kept.append(_keys(c))
    assert kept[0] == kept[1], "without a decoder neither side has a time, so the hash decides on every device"


def test_rotation_re_pushes_under_the_new_account(tmp_path):
    a = tmp_path / "a.db"
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "1.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, 0, tzinfo=UTC), 1000))
    _import(a, folder)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    old = _push(a, relay).bundles
    with storage.open_for_write(a, "sync") as conn:
        sync.forget_relay_state(conn)
    new = _push(a, relay, master=OTHER).bundles
    assert len(old) == len(new) == 1 and old[0].split("/")[0] != new[0].split("/")[0]
    assert relay.list(bundle.account_for(OTHER)) == new


# ---------------------------------------------------------------- the operator's view
def test_operator_sees_nothing_usable(tmp_path, monkeypatch):
    a = tmp_path / "a.db"
    export = tmp_path / "export"
    export.mkdir()
    _build_export(export)
    _import(a, export)
    canary = secrets.token_bytes(32)
    canary_b64 = __import__("base64").b64encode(canary).decode()
    with storage.open_for_write(a, "test") as conn:
        conn.execute("INSERT INTO raw_records(stream, source_key, source_scope, transport, payload_kind, payload, payload_hash, "
                     "payload_bytes, imported_at) VALUES('json:canary','canary','vendor_cloud','drop','json',?,?,?,'2025-07-01T00:00:00Z')",
                     (zlib.compress(canary), __import__("hashlib").sha256(canary).hexdigest(), len(canary)))
        source_values = [r[0] for r in conn.execute("SELECT DISTINCT stream FROM raw_records")] + \
                        [r[0] for r in conn.execute("SELECT DISTINCT source_key FROM raw_records")] + \
                        [r[0] for r in conn.execute("SELECT DISTINCT payload_hash FROM raw_records")] + ["2025-06-15"]
        source_values = [v for v in source_values if len(v) >= 8]  # a one-char value occurs in any random bytes
    # (iii) every byte string handed to the relay authenticates as AEAD output under the master
    real_put = FolderRelay.put
    handed = []

    def checking_put(self, name, data):
        bundle.unpack(MASTER, name, data)   # raises if it is not our AEAD output
        handed.append((name, len(data)))
        real_put(self, name, data)

    monkeypatch.setattr(FolderRelay, "put", checking_put)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay)
    assert handed, "push went through the single put"
    objects = [p for p in (tmp_path / "relay").rglob("*") if p.is_file()]
    assert objects and len(objects) == len(handed)
    for path in objects:
        data = path.read_bytes()
        rel = str(path.relative_to(tmp_path / "relay"))
        # (ii) names and sizes
        assert NAME.match(rel), rel
        assert len(data) - (1 + 32 + 12 + 16) == bundle.padme(len(data) - (1 + 32 + 12 + 16)), "Padmé-sized"
        # (i) the canary and every known plaintext value are absent, raw or base64; nothing inflates
        assert canary not in data and canary_b64.encode() not in data
        for value in source_values:
            assert value.encode() not in data, value
        for offset in range(0, len(data), 1):
            with pytest.raises(zlib.error):
                zlib.decompress(data[offset:offset + 4096])
        # (iv) a different master fails on every object
        with pytest.raises(bundle.BundleRejected, match="authentication_failed"):
            bundle.unpack(OTHER, rel, data)
        # tripwire only: entropy
        counts = [0] * 256
        for byte in data:
            counts[byte] += 1
        import math
        entropy = -sum(c / len(data) * math.log2(c / len(data)) for c in counts if c)
        assert entropy > 7.9
    assert not [p for p in (tmp_path / "relay").rglob("*") if p.is_file() and not NAME.match(str(p.relative_to(tmp_path / "relay")))], \
        "no index, manifest or other file on the relay"


# ---------------------------------------------------------------- review fixes (2026-10-02)
def _drop(tmp_path, name, *days):
    folder = tmp_path / name
    folder.mkdir()
    for i, day in enumerate(days):
        (folder / f"{i}.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, day, 21, 0, tzinfo=UTC), 1000 + day))
    return folder


def test_three_devices_in_a_ring_converge_without_echo(tmp_path):
    dbs = [tmp_path / f"{n}.db" for n in "abc"]
    for db, day in zip(dbs, (10, 12, 14)):
        _import(db, _drop(tmp_path, f"d{day}", day))
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    for db in dbs:
        _push(db, relay)
    for _round in range(2):
        for db in dbs:
            _pull(db, relay)
            _push(db, relay)
    assert len(relay.list(bundle.account_for(MASTER))) == 3, "three bundles, nothing echoed"
    keys = [_keys(db) for db in dbs]
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) == 3


def test_conflict_against_a_record_with_a_past_decode_failure_does_not_wedge(tmp_path):
    early = _uds("2025-06-15", 4000, 50)
    early["wellnessEndTimeGmt"] = "2025-06-15T12:00:00.0"
    late = _uds("2025-06-15", 8000, 50)
    late["wellnessEndTimeGmt"] = "2025-06-15T21:00:00.0"
    for root, record in ((tmp_path / "x", early), (tmp_path / "y", late)):
        agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
        agg.mkdir(parents=True)
        (agg / "UDSFile_2025-06-15_2025-06-15.json").write_text(json.dumps([record]))
        (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, tmp_path / "x")
    _import(b, tmp_path / "y")
    with storage.open_for_write(a, "test") as conn:   # a past reparse failure points at the losing record
        raw_id = conn.execute("SELECT id FROM raw_records").fetchone()[0]
        conn.execute("INSERT INTO import_failures(run_id, stream, raw_record_id, kind, recorded_at) VALUES(1, 'json:uds', ?, 'x', 'now')", (raw_id,))
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay), _push(b, relay)
    r = _pull(a, relay)
    assert r.conflicts == 1 and r.status == "ok"
    conn = storage.open_read_only(a)
    assert conn.execute("SELECT count(*) FROM raw_records").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'").fetchone()[0] == 8000
    assert conn.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM import_failures WHERE raw_record_id IS NOT NULL").fetchone()[0] == 0
    conn.close()
    again = _pull(a, relay)
    assert again.applied == [] and again.conflicts == 0


def test_crash_after_the_record_loop_is_repaired_by_the_next_pull(tmp_path, monkeypatch):
    export = tmp_path / "export"
    export.mkdir()
    _build_export(export)
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, export)
    _store(b)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay)
    from disconect.ingest.writer import Writer
    real = Writer.derive_daily_steps

    def boom(self):
        raise RuntimeError("power cut")

    monkeypatch.setattr(Writer, "derive_daily_steps", boom)
    with pytest.raises(RuntimeError):
        _pull(b, relay)
    monkeypatch.setattr(Writer, "derive_daily_steps", real)
    conn = storage.open_read_only(b)
    assert conn.execute("SELECT status FROM relay_bundles").fetchone()[0] == "applying"
    assert conn.execute("SELECT count(*) FROM daily_metrics WHERE metric='steps' AND source_scope='local'").fetchone()[0] == 0, \
        "the derive step never ran"
    conn.close()
    r = _pull(b, relay)
    assert len(r.applied) == 1
    assert _fingerprint(a) == _fingerprint(b), "the re-applied bundle re-derived its streams"


def test_a_half_applied_bundle_rejected_on_refetch_keeps_its_marker_and_re_derives_on_the_next_good_copy(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    _build_export(export)
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, export)
    _store(b)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    name = _push(a, relay).bundles[0]
    _pull(b, relay)
    with storage.open_for_write(b, "test") as conn:
        for table in ("daily_metrics", "daily_labels", "metric_samples", "monitoring_intervals",
                      "sleep_stages", "sleep_sessions", "clock_offsets"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE relay_bundles SET status='applying'")
    path = tmp_path / "relay" / name
    good = path.read_bytes()
    bad = bytearray(good)
    bad[len(bad) // 2] ^= 0xFF
    path.write_bytes(bytes(bad))
    r = _pull(b, relay)
    assert r.applied == [] and name in r.rejected
    conn = storage.open_read_only(b)
    assert [x[0] for x in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applying"]
    conn.close()
    counts = sync.status(storage.open_read_only(b))["bundles"]
    assert counts.get("pulled_applying") == 1 and "pulled_rejected" not in counts
    path.write_bytes(good)
    r = _pull(b, relay)
    assert len(r.applied) == 1
    assert _fingerprint(a) == _fingerprint(b), "the re-applied bundle re-derived its streams"


def test_a_poison_bundle_and_a_renamed_copy_are_rejected_without_blocking_the_rest(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, _drop(tmp_path, "d", 14))
    _store(b)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    account = bundle.account_for(MASTER)
    header = json.dumps({"t": "h", "format": 1, "device_id": "zz", "device_seq": 1, "prev": None, "created_utc": "x"})
    poison = bundle.new_name(account)
    relay.put(poison, bundle.seal_lines(MASTER, poison, [header, json.dumps({"t": "x", "stream": "s", "from_day": "a"})]))
    bad_header = bundle.new_name(account)
    relay.put(bad_header, bundle.seal_lines(MASTER, bad_header, [json.dumps({"t": "h", "format": 1, "device_seq": "1"})]))
    not_a_dict = bundle.new_name(account)
    relay.put(not_a_dict, bundle.seal_lines(MASTER, not_a_dict, [header, "[1,2]"]))
    good = _push(a, relay).bundles[0]
    renamed = bundle.new_name(account)
    relay.put(renamed, relay.get(good))                       # the operator copies an object to a new name
    r = _pull(b, relay)
    assert r.applied == [good] and r.status == "partial"
    assert r.rejected == {poison: "bad_bundle", bad_header: "bad_header", not_a_dict: "bad_bundle", renamed: "authentication_failed"}
    assert _keys(a) == _keys(b)
    conn = storage.open_read_only(b)
    assert conn.execute("SELECT status FROM import_runs WHERE transport='relay'").fetchone()[0] == "partial"
    conn.close()


def test_oversized_object_is_rejected_before_it_is_read(tmp_path):
    from disconect.relay import folder as folder_module
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    name = bundle.new_name(bundle.account_for(MASTER))
    relay.put(name, b"x")
    (tmp_path / "relay" / name).write_bytes(b"\0" * (folder_module.MAX_OBJECT + 1))
    with pytest.raises(folder_module.TooLarge):
        relay.get(name)


def test_cli_sync_on_encrypted_stores_bootstraps_an_empty_device(tmp_path, capsys, monkeypatch):
    import os
    from disconect import cli
    from disconect.storage import keys
    monkeypatch.setattr(cli, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli, "_show_words_once", lambda master: False)

    def run(argv, passphrase):
        os.environ[keys.PASSPHRASE_ENV] = passphrase
        storage._unlocked.clear(); keys.forget_session()
        return cli.main(argv)
    export = tmp_path / "export"
    export.mkdir()
    _build_export(export)
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    assert run(["--db", str(a), "key", "init"], "a-strong-scratch-passphrase") == cli.EXIT_OK
    assert run(["--db", str(a), "import", str(export)], "a-strong-scratch-passphrase") == cli.EXIT_OK
    relay_dir = tmp_path / "relay"
    assert run(["--db", str(a), "sync", "push", "--relay", str(relay_dir)], "a-strong-scratch-passphrase") == cli.EXIT_OK
    # B: no database at all; recover the key from A's words, then pull
    master_a = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(a)), "a-strong-scratch-passphrase")
    os.environ[keys.RECOVERY_WORDS_ENV] = keys.words_for(master_a)
    assert run(["--db", str(b), "key", "recover"], "another-strong-passphrase") == cli.EXIT_OK
    assert run(["--db", str(b), "sync", "pull", "--relay", str(relay_dir)], "another-strong-passphrase") == cli.EXIT_OK
    assert storage.is_encrypted_file(b) is True
    os.environ[keys.PASSPHRASE_ENV] = "a-strong-scratch-passphrase"
    fa = _fingerprint(a)
    os.environ[keys.PASSPHRASE_ENV] = "another-strong-passphrase"
    assert fa == _fingerprint(b)
    capsys.readouterr()
    assert run(["--db", str(b), "--json", "sync", "status"], "another-strong-passphrase") == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["bundles"] == {"pulled_applied": 1} and report["records_unsent"] == 0
    # the chains list is the one place a writer id appears (as `chain`), with counts only, and B is not its writer
    assert "device_id" not in json.dumps(report)
    (chain,) = report["chains"]
    assert (chain["bundles"], chain["last_seq"], chain["self"]) == (1, 1, False) and chain["records"] > 0
    # the text form of `sync status` closes the gap count with the chain count
    capsys.readouterr()
    assert run(["--db", str(b), "sync", "status"], "another-strong-passphrase") == cli.EXIT_OK
    assert "; gaps 0; chains 1; last push never," in capsys.readouterr().out
    # a plaintext store cannot sync
    c = tmp_path / "c.db"
    _store(c)
    os.environ.pop(keys.PASSPHRASE_ENV, None)
    assert cli.main(["--db", str(c), "sync", "push", "--relay", str(relay_dir)]) == cli.EXIT_LOCKED
    # forget, then push again publishes everything under the same account
    assert run(["--db", str(b), "sync", "forget"], "another-strong-passphrase") == cli.EXIT_OK
    assert run(["--db", str(b), "sync", "push", "--relay", str(relay_dir)], "another-strong-passphrase") == cli.EXIT_OK
    assert len(FolderRelay(relay_dir, create_root=True).list(bundle.account_for(master_a))) == 2


def test_a_live_record_born_on_the_phone_lands_on_the_mac_byte_for_byte(tmp_path):
    phone, mac = tmp_path / "phone.db", tmp_path / "mac.db"
    live_file = tmp_path / "live-20250615T150640Z.jsonl"
    live_file.write_text("".join(json.dumps(line) + "\n" for line in [
        {"status": "scanning"}, {"t": 1750000002.0, "metric": "steps", "value": 40},
        {"t": 1750000001.0, "metric": "heart_rate", "value": 71}, {"status": "stopped", "stop": "LinkClosed"}]))
    with storage.open_for_write(phone, "test") as conn:
        sources.import_path(live_file, conn, transport="ble")
    _store(mac)
    columns = ("stream, source_key, source_scope, transport, device_id, start_utc, end_utc, payload_kind, "
               "payload, payload_hash")

    def rows(db):
        conn = storage.open_read_only(db)
        try:
            return conn.execute(f"SELECT {columns} FROM raw_records ORDER BY stream, source_key").fetchall()
        finally:
            conn.close()
    born = rows(phone)
    assert len(born) == 1 and born[0][0] == "json:live" and born[0][3] == "ble"
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    assert _push(phone, relay).records == 1
    assert _pull(mac, relay).records_new == 1
    assert rows(mac) == born
    conn = storage.open_read_only(mac)
    try:
        assert conn.execute("SELECT count(*) FROM import_runs WHERE transport='relay'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM raw_records WHERE transport='ble'").fetchone()[0] == 1
    finally:
        conn.close()


def _chain_rows(db_path) -> list[dict]:
    with storage.open_for_write(db_path, "sync") as conn:
        return sync.status(conn)["chains"]


def test_status_never_mints_a_writer_id(tmp_path):
    a = tmp_path / "a.db"
    _store(a)
    assert _chain_rows(a) == []
    conn = storage.open_read_only(a)
    try:
        assert conn.execute("SELECT count(*) FROM relay_device").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_forget_leaves_the_old_writer_id_listed_and_the_new_chain_counts_what_it_received(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for db, day, n in ((a, 12, 500), (b, 13, 700)):
        _store(db)
        drop = tmp_path / f"drop{day}"
        drop.mkdir()
        (drop / "x.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, day, 21, 0, tzinfo=UTC), n))
        _import(db, drop)
    relay = FolderRelay(tmp_path / "relay", create_root=True)
    _push(a, relay), _push(b, relay), _pull(a, relay), _pull(b, relay)
    (old_a,) = [c for c in _chain_rows(a) if c["self"]]
    assert (old_a["bundles"], old_a["records"]) == (1, 1)
    # A forgets (its relay tables go, the relay objects stay), pushes again and pulls
    with storage.open_for_write(a, "sync") as conn:
        sync.forget_relay_state(conn)
    _push(a, relay), _pull(a, relay), _pull(b, relay)
    for db in (a, b):
        rows = {c["chain"]: c for c in _chain_rows(db)}
        assert len(rows) == 3, "B's chain and both of A's"
        assert rows[old_a["chain"]]["self"] is False, "A's old id is another writer, on A as well as on B"
        assert (rows[old_a["chain"]]["bundles"], rows[old_a["chain"]]["records"]) == (1, 1)
    (new_a,) = [c for c in _chain_rows(a) if c["self"]]
    assert new_a["chain"] != old_a["chain"]
    # the new chain carries what A had received as well as what it made: `records` is what a writer's bundles carried
    assert (new_a["bundles"], new_a["records"]) == (1, 2)
    assert {c["chain"]: c for c in _chain_rows(b)}[new_a["chain"]] == {**new_a, "self": False}
