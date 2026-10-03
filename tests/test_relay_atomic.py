# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bet 12a: the relay's conflict write is one transaction, and a storage error is an error.

A conflict winner used to be written in three commits (decision, loser delete, winner insert); a storage
error on the last one left the loser gone, turned into ``FAILED`` and still marked the bundle ``applied``,
so two devices diverged silently. Now the loser-side writes ride inside the winner's own transaction and
the pull fails, leaving the bundle ``applying`` for the next pull. Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from disconect import storage
from disconect.relay import sync
from disconect.relay.folder import FolderRelay
from test_converge_10b import _assert_reparse_quiet, _daily
from test_import import _uds
from test_relay import MASTER, _fingerprint, _import, _pull, _push

EARLY_STEPS, LATE_STEPS = 4000, 8000


def _store_with(root: pathlib.Path, record: dict) -> pathlib.Path:
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir(parents=True)
    (agg / "UDSFile_2025-06-15_2025-06-15.json").write_text(json.dumps([record]))
    (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()
    return root


def _pair(tmp_path: pathlib.Path):
    """Device a holds the early observation, device b the late one: the late one wins everywhere."""
    early = _uds("2025-06-15", EARLY_STEPS, 50)
    early["wellnessEndTimeGmt"] = "2025-06-15T12:00:00.0"
    late = _uds("2025-06-15", LATE_STEPS, 50)
    late["wellnessEndTimeGmt"] = "2025-06-15T21:00:00.0"
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, _store_with(tmp_path / "x", early))
    _import(b, _store_with(tmp_path / "y", late))
    relay = FolderRelay(tmp_path / "relay")
    _push(a, relay), _push(b, relay)
    return a, b, relay


def _hash_of(db: pathlib.Path) -> str:
    conn = storage.open_read_only(db)
    try:
        return conn.execute("SELECT payload_hash FROM raw_records WHERE stream='json:uds'").fetchone()[0]
    finally:
        conn.close()


def _count(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def _steps(conn) -> int:
    return conn.execute("SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'").fetchone()[0]


def test_failed_winner_write_rolls_the_whole_conflict_back_and_the_next_pull_converges(tmp_path):
    a, b, relay = _pair(tmp_path)
    incoming = _hash_of(b)
    loser = _hash_of(a)
    with storage.open_for_write(a, "sync") as conn:
        conn.execute("CREATE TEMP TRIGGER inject BEFORE INSERT ON raw_records "
                     f"WHEN NEW.payload_hash='{incoming}' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sync.RecordWriteFailed):
            sync.pull(conn, MASTER, relay)
        assert not conn.in_transaction
        assert conn.execute("SELECT payload_hash FROM raw_records WHERE stream='json:uds'").fetchall() == [(loser,)], \
            "the loser is still stored"
        assert _steps(conn) == EARLY_STEPS, "and still feeds its daily row"
        assert _count(conn, "SELECT count(*) FROM sync_conflicts") == 0
        assert _count(conn, "SELECT count(*) FROM raw_superseded") == 0
        assert _count(conn, "SELECT count(*) FROM relay_seen s JOIN raw_records r ON r.id=s.raw_record_id "
                            f"WHERE r.payload_hash='{incoming}'") == 0
        assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applying"]
        conn.execute("DROP TRIGGER inject")
    retry = _pull(a, relay)
    assert retry.status == "ok" and retry.conflicts == 1 and len(retry.applied) == 1
    _pull(b, relay)
    assert _fingerprint(a) == _fingerprint(b)
    conn = storage.open_read_only(a)
    assert _steps(conn) == LATE_STEPS
    assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applied"]
    conn.close()
    _assert_reparse_quiet(a)


def test_failed_loser_record_leaves_no_transaction_open(tmp_path):
    a, b, relay = _pair(tmp_path)
    with storage.open_for_write(b, "sync") as conn:   # b holds the winner: a's bundle is the loser
        conn.execute("CREATE TEMP TRIGGER inject BEFORE INSERT ON raw_superseded BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(storage.sqlite.Error):
            sync.pull(conn, MASTER, relay)
        assert not conn.in_transaction, "the incoming-loses branch rolled back"
        assert _count(conn, "SELECT count(*) FROM sync_conflicts") == 0
        assert _steps(conn) == LATE_STEPS
        conn.execute("DROP TRIGGER inject")
        conn.execute("INSERT INTO import_runs(started_at, transport, status) VALUES('now', 'probe', 'ok')")   # a following write works
        conn.commit()
    again = _pull(b, relay)
    assert again.conflicts == 1 and again.status == "ok"
    conn = storage.open_read_only(b)
    assert _count(conn, "SELECT count(*) FROM sync_conflicts") == 1 and _count(conn, "SELECT count(*) FROM raw_superseded") == 1
    conn.close()



# ---- opus review of 12a: F1-F5 ------------------------------------------------------------------

def _rec(day: str, steps: int, end_hour: str) -> dict:
    record = _uds(day, steps, 50)
    record["wellnessEndTimeGmt"] = f"{day}T{end_hour}:00:00.0"
    return record


def _store_many(root: pathlib.Path, records: list[dict]) -> pathlib.Path:
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir(parents=True)
    for record in records:
        day = record["calendarDate"]
        (agg / f"UDSFile_{day}_{day}.json").write_text(json.dumps([record]))
    (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()
    return root


def _seen(conn, payload_hash: str) -> int:
    return _count(conn, "SELECT count(*) FROM relay_seen s JOIN raw_records r ON r.id=s.raw_record_id "
                        f"WHERE r.payload_hash='{payload_hash}'")


def _unsent(conn) -> int:
    return sync.status(conn)["records_unsent"]


def _trigger(db: pathlib.Path, sql: str) -> None:
    with storage.open_for_write(db, "test") as conn:
        conn.execute(sql)
        conn.commit()


def test_f1_winner_is_marked_seen_when_the_loser_held_the_max_rowid(tmp_path):
    """The loser is the only (so the max-id) row: the winner reuses its rowid, and ``id > before`` marked nothing."""
    a, b, relay = _pair(tmp_path)
    incoming = _hash_of(b)
    result = _pull(a, relay)
    assert result.conflicts == 1 and result.status == "ok"
    conn = storage.open_read_only(a)
    assert _seen(conn, incoming) == 1, "the winner is relay_seen (it came from the relay)"
    assert _unsent(conn) == 0, "a clean conflict pull leaves nothing to push back"
    conn.close()
    again = _push(a, relay)
    assert again.records == 0, "the winner is not echoed to the relay"


def _pair_with_second_day(tmp_path: pathlib.Path):
    """Device a holds the early 06-15 record (rowid 1) and a 06-16 one (rowid 2); b holds the late 06-15."""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _import(a, _store_many(tmp_path / "x", [_rec("2025-06-15", EARLY_STEPS, "12"), _rec("2025-06-16", 3000, "21")]))
    _import(b, _store_many(tmp_path / "y", [_rec("2025-06-15", LATE_STEPS, "21")]))
    relay = FolderRelay(tmp_path / "relay")
    _push(a, relay), _push(b, relay)
    conn = storage.open_read_only(a)
    loser_id = conn.execute("SELECT id FROM raw_records WHERE source_key LIKE '%2025-06-15%'").fetchone()[0]
    assert loser_id < conn.execute("SELECT max(id) FROM raw_records").fetchone()[0], "the loser is not the max rowid"
    conn.close()
    return a, b, relay


def test_f2_retried_pull_marks_the_already_stored_winner_seen(tmp_path):
    """The winner commits, then its relay_seen insert is refused: the retry finds it stored and must still mark it."""
    a, b, relay = _pair_with_second_day(tmp_path)
    incoming = _hash_of(b)
    with storage.open_for_write(a, "sync") as conn:
        conn.execute("CREATE TEMP TRIGGER inject BEFORE INSERT ON relay_seen WHEN "
                     f"(SELECT payload_hash FROM raw_records WHERE id=NEW.raw_record_id)='{incoming}' "
                     "BEGIN SELECT RAISE(ABORT,'crash after commit'); END")
        with pytest.raises(storage.sqlite.Error):
            sync.pull(conn, MASTER, relay)
        assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applying"]
        conn.execute("DROP TRIGGER inject")
    retry = _pull(a, relay)
    assert retry.status == "ok" and len(retry.applied) == 1
    conn = storage.open_read_only(a)
    assert _seen(conn, incoming) == 1, "the retry marked the winner seen"
    assert _count(conn, "SELECT count(*) FROM sync_conflicts") == 1, "no duplicate conflict row"
    assert _count(conn, "SELECT count(*) FROM raw_superseded") == 1
    assert _unsent(conn) == 0
    conn.close()


@pytest.mark.parametrize("abort", ["ABORT", "ROLLBACK"])
def test_f3_a_plain_record_storage_failure_keeps_the_bundle_applying(tmp_path, abort):
    """A refused write of a non-conflict record used to end ``applied`` with nothing retried: permanent divergence.
    ``RAISE(ROLLBACK)`` ends the transaction itself, so the writer's own ROLLBACK must not mask the error (F5)."""
    feeder, puller, relay = tmp_path / "f.db", tmp_path / "p.db", FolderRelay(tmp_path / "relay")
    _import(feeder, _store_with(tmp_path / "y", _rec("2025-06-15", LATE_STEPS, "21")))
    _push(feeder, relay)
    with storage.open_for_write(puller, "test"):
        pass   # an empty store: the record is not a conflict, just a write
    incoming = _hash_of(feeder)
    with storage.open_for_write(puller, "sync") as conn:
        conn.execute("CREATE TEMP TRIGGER inject BEFORE INSERT ON raw_records "
                     f"WHEN NEW.payload_hash='{incoming}' BEGIN SELECT RAISE({abort},'injected'); END")
        with pytest.raises(sync.RecordWriteFailed):
            sync.pull(conn, MASTER, relay)
        assert not conn.in_transaction
        assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applying"]
        assert _count(conn, "SELECT count(*) FROM raw_records") == 0
        conn.execute("DROP TRIGGER inject")
    retry = _pull(puller, relay)
    assert retry.status == "ok" and retry.records_new == 1 and len(retry.applied) == 1
    assert _daily(puller) == _daily(feeder), "the daily rows converged after the retry"
    conn = storage.open_read_only(puller)
    assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applied"]
    conn.close()


def _ring_stores(tmp_path: pathlib.Path):
    """Three devices. The feeder's 06-14 record loses at the puller and its 06-15 record wins there."""
    feeder, puller, third = (tmp_path / f"{name}.db" for name in ("feeder", "puller", "third"))
    _import(feeder, _store_many(tmp_path / "f", [_rec("2025-06-14", 1000, "12"), _rec("2025-06-15", LATE_STEPS, "21")]))
    _import(puller, _store_many(tmp_path / "p", [_rec("2025-06-14", 2000, "21"), _rec("2025-06-15", EARLY_STEPS, "12")]))
    _import(third, _store_many(tmp_path / "t", [_rec("2025-06-16", 3000, "21")]))
    relay = FolderRelay(tmp_path / "relay")
    for db in (feeder, puller, third):
        _push(db, relay)
    conn = storage.open_read_only(feeder)
    winner = conn.execute("SELECT payload_hash FROM raw_records WHERE source_key LIKE '%2025-06-15%'").fetchone()[0]
    conn.close()
    return feeder, puller, third, relay, winner


def _history(db: pathlib.Path) -> tuple[int, int]:
    conn = storage.open_read_only(db)
    try:
        return _count(conn, "SELECT count(*) FROM sync_conflicts"), _count(conn, "SELECT count(*) FROM raw_superseded")
    finally:
        conn.close()


def test_f4_a_reapplied_bundle_does_not_duplicate_its_incoming_loser_rows(tmp_path):
    """The failing bundle carries an incoming loser ahead of the failing winner: the retry re-decides the loser."""
    feeder, puller, third, relay, winner = _ring_stores(tmp_path)
    with storage.open_for_write(puller, "sync") as conn:
        conn.execute("CREATE TEMP TRIGGER inject BEFORE INSERT ON raw_records "
                     f"WHEN NEW.payload_hash='{winner}' BEGIN SELECT RAISE(ABORT,'x'); END")
        with pytest.raises(sync.RecordWriteFailed):
            sync.pull(conn, MASTER, relay)
        conn.execute("DROP TRIGGER inject")
    _pull(puller, relay)
    _pull(third, relay)
    for _ in range(3):
        for db in (feeder, puller, third):
            _push(db, relay)
        for db in (feeder, puller, third):
            _pull(db, relay)
    histories = {db.stem: _history(db) for db in (feeder, puller, third)}
    assert len({counts for counts in histories.values()}) == 1, histories
    assert histories["puller"][0] == histories["puller"][1], "one superseded row per conflict row"
    assert _daily(feeder) == _daily(puller) == _daily(third)
