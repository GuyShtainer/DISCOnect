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
from test_converge_10b import _assert_reparse_quiet
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
        with pytest.raises(sync.ConflictWriteFailed):
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

