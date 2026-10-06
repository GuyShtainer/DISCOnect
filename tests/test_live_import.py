"""Bet 9b slice 1: a live-link session file is kept as one `json:live` raw record; the decoder derives nothing
(the 9b-2 fold writes `live`-scope samples from the retained records, counted apart here)."""

import datetime
import hashlib
import json
import zlib

import pytest

from disconect import cli, storage
from disconect.ingest import connect_export, live, sources
from test_import import UTC, _monitoring_day

T0 = 1750000000.0  # synthetic: 2025-06-15T15:06:40Z
LIVE_LINES = [
    {"status": "scanning"},
    {"t": T0 + 2, "metric": "steps", "value": 40},
    {"t": T0 + 1, "metric": "heart_rate", "value": 71},
    {"t": T0 + 1, "metric": "heart_rate", "value": 70},
    {"status": "stopped", "stop": "LinkClosed"},
]
#: Readings sorted by (t, metric, value), status dropped, compact separators, sorted keys.
PINNED_PAYLOAD = (b'{"readings":[[1750000001.0,"heart_rate",70],[1750000001.0,"heart_rate",71],'
                  b'[1750000002.0,"steps",40]]}')


def _write_lines(path, lines):
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _monitoring_fit(path):
    path.write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, tzinfo=UTC), 8000))
    return path


def _import(path, db_path, transport=None):
    with storage.open_for_write(db_path, "test") as conn:
        return sources.import_path(path, conn, transport=transport)


def _live_rows(db_path):
    conn = storage.open_read_only(db_path)
    return conn.execute("SELECT stream, source_key, source_scope, transport, device_id, start_utc, end_utc, "
                        "payload_kind, payload, payload_hash FROM raw_records WHERE stream='json:live'").fetchall()


def _canonical_counts(db_path):
    conn = storage.open_read_only(db_path)
    # the 9b-2 fold writes `live`-scope samples; everything else must stay as it was
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}" + (" WHERE source_scope != 'live'" if t == "metric_samples" else "")).fetchone()[0]
            for t in ("metric_samples", "daily_metrics", "daily_labels", "monitoring_intervals", "activities")}


def test_live_file_becomes_one_pinned_raw_record(tmp_path, db_path):
    path = _write_lines(tmp_path / "live-20250615T150640Z.jsonl", LIVE_LINES)
    stats = _import(path, db_path)
    assert (stats.files_imported, stats.files_failed) == (1, 0)
    (row,) = _live_rows(db_path)
    stream, key, scope, transport, device, start, end, kind, blob, digest = row
    assert (stream, scope, transport, device, kind) == ("json:live", "device", "ble", None, "json")
    assert (start, end) == ("2025-06-15T15:06:41Z", "2025-06-15T15:06:42Z")
    assert zlib.decompress(blob) == PINNED_PAYLOAD
    assert key == digest == hashlib.sha256(PINNED_PAYLOAD).hexdigest()
    assert not any(_canonical_counts(db_path).values())


def test_same_file_twice_is_duplicate(tmp_path, db_path):
    _import(_write_lines(tmp_path / "live-a.jsonl", LIVE_LINES), db_path)
    again = _write_lines(tmp_path / "live-b.jsonl", list(reversed(LIVE_LINES)))  # same readings, other order
    stats = _import(again, db_path)
    assert (stats.files_imported, stats.files_duplicate) == (0, 1)
    assert len(_live_rows(db_path)) == 1


def test_status_only_file_is_skipped_with_a_counted_reason(tmp_path, db_path):
    path = _write_lines(tmp_path / "live-empty.jsonl", [{"status": "scanning"}, {"status": "stopped", "stop": "x"}])
    stats = _import(path, db_path)
    assert stats.dropped == {sources.DROPPED_LIVE_EMPTY: 1}
    assert (stats.files_imported, stats.files_failed) == (0, 0)
    assert _live_rows(db_path) == []


def test_a_file_cut_off_mid_line_keeps_its_whole_lines_and_is_counted(tmp_path, db_path):
    """The lab killed mid-write leaves a partial last line: the session before it is imported, once,
    as the same pinned record the whole file gives, and the loss is counted (9b-2 review O1)."""
    (tmp_path / "whole").mkdir()
    (tmp_path / "cut").mkdir()
    whole = _write_lines(tmp_path / "whole" / "live-x.jsonl", LIVE_LINES)
    path = tmp_path / "cut" / "live-x.jsonl"
    path.write_bytes(whole.read_bytes() + b'{"t": 1750000003.0, "metric": "heart_')
    assert live.read_live_file(path.read_bytes()) == (live.parse_live_file(whole.read_bytes()), True)
    assert live.parse_live_file(path.read_bytes()) is None
    stats = _import(path, db_path)
    assert (stats.files_seen, stats.files_imported, stats.files_failed) == (1, 1, 0)
    assert stats.dropped == {sources.DROPPED_LIVE_CUT_OFF: 1}
    assert zlib.decompress(_live_rows(db_path)[0][8]) == PINNED_PAYLOAD
    assert _import(whole, db_path).files_duplicate == 1   # the whole file is the same record
    # the whole lines must pass the live rule, and at least one must precede the partial line
    assert live.read_live_file(b'{"t": 1, "metric": "x", "value": 1}\n{"frame": "0a0b"}\n{"t": 2, "metr') is None
    assert live.read_live_file(b'{"t": 2, "metr') is None
    assert live.read_live_file(b'{"frame": "0a0b"}\n') is None   # well-formed and not live stays not live
    assert live.read_live_file(b'{"status": "scanning"}\n{"t": 2, "metr') == ([], True)
    assert live.read_live_file(b'{"status": "scanning"}\n{"t": 1, "metric": "x", "value": 1}\r\n   \n{"t":') \
        == ([[1, "x", 1]], True)


def test_frame_log_and_malformed_files_are_not_live_files(tmp_path):
    lines = [{"status": "scanning"}, {"frame": "0a0b", "dir": "rx"}]
    assert live.parse_live_file("\n".join(json.dumps(x) for x in lines).encode()) is None
    assert live.parse_live_file(b'{"t": 1, "metric": "steps", "value": 1.5}\n') is None   # value must be an int
    assert live.parse_live_file(b'{"t": 1, "metric": "steps", "value": true}\n') is None
    assert live.parse_live_file(b"") is None
    assert live.parse_live_file(b"\x00\x01\xff") is None
    assert list(sources.iter_live_files(_write_lines(tmp_path / "frames.jsonl", lines))) == []


def test_frame_log_file_falls_through_to_the_fit_path(tmp_path, db_path):
    stats = _import(_write_lines(tmp_path / "frames.jsonl", [{"frame": "0a0b"}]), db_path)
    assert stats.files_failed == 1 and _live_rows(db_path) == []


def test_a_fit_file_is_not_a_live_file(tmp_path, db_path):
    fit = _monitoring_fit(tmp_path / "A1.fit")
    assert list(sources.iter_live_files(fit)) == []
    stats = _import(fit, db_path)
    assert stats.files_imported == 1 and _live_rows(db_path) == []


def test_mixed_folder_imports_the_live_file_and_the_fit_file(tmp_path, db_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    _write_lines(folder / "live-x.jsonl", LIVE_LINES)
    _write_lines(folder / "notes.jsonl", [{"frame": "0a0b"}])   # not live, not FIT: the FIT pass ignores it
    _monitoring_fit(folder / "A1.fit")
    stats = _import(folder, db_path)
    assert (stats.files_imported, stats.files_failed) == (2, 0)
    assert len(_live_rows(db_path)) == 1 and "json:live" in stats.streams


def test_ble_transport_is_allowed_everywhere(tmp_path, db_path):
    from disconect import serve
    assert serve.TRANSPORTS["ble"] == "ble"
    path = _write_lines(tmp_path / "live-t.jsonl", LIVE_LINES)
    assert cli.main(["--db", str(db_path), "import", "--transport", "ble", str(path)]) == cli.EXIT_OK
    assert _live_rows(db_path)[0][3] == "ble"


def test_decoder_is_registered_and_derives_nothing():
    record, _data = live.canonical_payload([[T0, "steps", 1]])
    _key, decoded = connect_export.RECORD_DECODERS["json:live"](record)
    assert decoded.record_count() == 0 and decoded.stream == "json:live" and decoded.device_id is None
    with pytest.raises(ValueError):
        live.decode_live_record({"readings": []})


def test_reparse_all_keeps_the_live_record_and_derives_nothing(tmp_path, db_path):
    _import(_write_lines(tmp_path / "live-r.jsonl", LIVE_LINES), db_path)
    _import(_monitoring_fit(tmp_path / "A1.fit"), db_path)
    before_rows, before_counts = _live_rows(db_path), _canonical_counts(db_path)
    with storage.open_for_write(db_path, "test") as conn:
        stats = sources.reparse_all(conn)
        only = sources.reparse_all(conn, streams=["json:live"])
    assert stats.files_failed == 0 and only.files_failed == 0 and only.files_seen == 1
    assert _live_rows(db_path) == before_rows
    assert _canonical_counts(db_path) == before_counts


def test_status_lists_the_stream_with_count_and_span(tmp_path, db_path, capsys):
    _import(_write_lines(tmp_path / "live-s.jsonl", LIVE_LINES), db_path)
    assert cli.main(["--db", str(db_path), "status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if row.strip().startswith("json:live"))
    assert " 1 raw records" in line and "2025-06-15" in line


def test_relay_carries_the_live_record_and_the_peer_stores_the_bytes(tmp_path):
    from disconect.relay import sync
    from disconect.relay.folder import FolderRelay
    master, relay = bytes(range(32)), FolderRelay(tmp_path / "relay")
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    _import(_write_lines(tmp_path / "live-p.jsonl", LIVE_LINES), first)
    _import(_monitoring_fit(tmp_path / "A1.fit"), second)
    before = _canonical_counts(second)
    with storage.open_for_write(first, "sync") as conn:
        sync.push(conn, master, relay)
    with storage.open_for_write(second, "sync") as conn:
        result = sync.pull(conn, master, relay)
    assert (result.records_new, result.records_invalid) == (1, 0)
    (row,) = _live_rows(second)
    assert zlib.decompress(row[8]) == PINNED_PAYLOAD and row[3] == "ble"
    assert _canonical_counts(second) == before
    # the pull itself folds the live minutes: a pull-only device gets the same rows (9b-2 review S2)
    folded = "SELECT metric, ts_utc, value FROM metric_samples WHERE source_scope='live' ORDER BY metric, ts_utc"
    with storage.open_read_only(first) as a, storage.open_read_only(second) as b:
        assert a.execute(folded).fetchall() == b.execute(folded).fetchall() != []


def test_a_live_file_with_a_millisecond_stamp_is_not_a_live_file(tmp_path):
    bound = live.T_LIMIT
    assert live.parse_live_file(json.dumps({"t": bound - 1, "metric": "steps", "value": 1}).encode()) == [[bound - 1, "steps", 1]]
    assert live.parse_live_file(json.dumps({"t": 0, "metric": "steps", "value": 1}).encode()) == [[0, "steps", 1]]
    for t in (bound, 1750000000123, -1, -0.5):
        line = json.dumps({"t": t, "metric": "steps", "value": 1}).encode()
        assert live.parse_live_file(line) is None, t
    assert live.parse_live_file(b'{"t": NaN, "metric": "steps", "value": 1}\n') is None
    # the folder import carries on: such a file falls through to the FIT path instead of aborting the sweep
    _write_lines(tmp_path / "live-ms.jsonl", [{"t": 1750000000123, "metric": "steps", "value": 1}])
    assert list(sources.iter_live_files(tmp_path)) == []


@pytest.mark.parametrize("reading", [[T0, "hr", 1.5], [T0, "hr", True], [True, "hr", 1], [T0, 7, 1], [T0, "hr"],
                                     [T0, "hr", 1, 2], "abc", [1750000000123, "hr", 1], [-1, "hr", 1]])
def test_decode_live_record_refuses_a_reading_the_file_check_would_refuse(reading):
    with pytest.raises(ValueError):
        live.decode_live_record({"readings": [[T0, "heart_rate", 70], reading]})


def test_decode_live_record_takes_well_formed_readings():
    key, decoded = live.decode_live_record({"readings": [[T0 + 1, "steps", 40], [T0, "heart_rate", 70]]})
    assert len(key) == 64 and decoded.start_utc < decoded.end_utc


def test_relay_counts_a_malformed_live_record_invalid_instead_of_aborting(tmp_path):
    from disconect.relay import sync
    from disconect.relay.folder import FolderRelay
    master, relay = bytes(range(32)), FolderRelay(tmp_path / "relay")
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    _import(_write_lines(tmp_path / "live-p.jsonl", LIVE_LINES), first)
    bad = json.dumps({"readings": [[T0, "heart_rate", 70], [T0]]}, sort_keys=True, separators=(",", ":")).encode()
    with storage.open_for_write(first, "test") as conn:
        conn.execute("UPDATE raw_records SET payload=?, payload_hash=?, payload_bytes=? WHERE stream='json:live'",
                     (zlib.compress(bad), hashlib.sha256(bad).hexdigest(), len(bad)))
    _import(_monitoring_fit(tmp_path / "A1.fit"), second)
    with storage.open_for_write(first, "sync") as conn:
        sync.push(conn, master, relay)
    with storage.open_for_write(second, "sync") as conn:
        result = sync.pull(conn, master, relay)
    assert (result.records_new, result.records_invalid) == (0, 1)
    assert _live_rows(second) == []
