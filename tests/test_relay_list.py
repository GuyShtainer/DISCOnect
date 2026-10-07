# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bet 19d: a device with several relays (the Python twin of ``disconect-core/tests/relay_list_test.rs``). A push lands
on every site, an emptied site is healed from the store, a pull is the union of the listings with a fall-through to
the next site, and ``open_sites`` reports what it could not open. Synthetic data only, scratch stores."""

from __future__ import annotations

import errno
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

from disconect import cli, storage
from disconect.relay import config as relay_config
from disconect.relay import sync
from disconect.relay.bundle import account_for, unpack
from disconect.relay.config import RelayEntry
from disconect.relay.folder import FolderRelay
from disconect.relay.sync import SiteReport, SiteSpec, open_sites, pull_all, push_all
from test_import import _uds
from test_relay import MASTER, _import

ACCOUNT = account_for(MASTER)


# ---------------------------------------------------------------- config forms
def test_the_legacy_forms_read_as_one_entry_named_default(tmp_path):
    path = tmp_path / "relay.json"
    assert relay_config.read_list(path) is None
    for text, want in (
        ('{"folder": "/r"}', RelayEntry("default", "folder", "/r", "", True)),
        ('{"lan": "http://x:1"}', RelayEntry("default", "lan", "http://x:1", "", False)),
        ('{"folder": "/r", "lan": "http://x:1"}', RelayEntry("default", "lan", "http://x:1", "", False)),
        ('{"folder": "/r", "lan": ""}', RelayEntry("default", "folder", "/r", "", True)),
        ('{"folder": 5}', None),
        ('{"folder": ""}', None),
        ("{}", None),
        ("[1]", None),
        ("not json", None),
    ):
        path.write_text(text)
        assert relay_config.read_list(path) == ([want] if want else None), text
        legacy = relay_config.read(path)
        assert legacy == ((want.kind, want.value) if want else None), text


def test_the_list_form_wins_and_every_malformed_case_reads_as_nothing(tmp_path):
    path = tmp_path / "relay.json"
    path.write_text('{"relays": [{"id": "0a1b2c3d", "kind": "folder", "path": "/a", "label": "cloud", "serve": true},'
                    ' {"id": "default", "kind": "lan", "url": "http://h:2"}], "folder": "/ignored"}')
    assert relay_config.read_list(path) == [RelayEntry("0a1b2c3d", "folder", "/a", "cloud", True),
                                            RelayEntry("default", "lan", "http://h:2", "", False)]
    long_id = "0123456789abcdef0123456789abcdef0"
    path.write_text('{"relays": []}')
    assert relay_config.read_list(path) == [], "an empty array is an empty list"
    assert relay_config.read(path) is None
    for bad in (
        '{"relays": [5]}',
        '{"relays": [{"kind": "folder", "path": "/a"}]}',
        '{"relays": [{"id": "ABCD", "kind": "folder", "path": "/a"}]}',
        '{"relays": [{"id": "", "kind": "folder", "path": "/a"}]}',
        '{"relays": [{"id": "%s", "kind": "folder", "path": "/a"}]}' % long_id,
        '{"relays": [{"id": "ab", "kind": "ftp", "path": "/a"}]}',
        '{"relays": [{"id": "ab", "kind": "folder"}]}',
        '{"relays": [{"id": "ab", "kind": "folder", "path": ""}]}',
        '{"relays": [{"id": "ab", "kind": "lan", "path": "/a"}]}',
        '{"relays": [{"id": "ab", "kind": "folder", "path": "/a", "label": 5}]}',
        '{"relays": [{"id": "ab", "kind": "folder", "path": "/a", "serve": "yes"}]}',
        '{"relays": [{"id": "ab", "kind": "lan", "url": "http://h:2", "serve": true}]}',
        '{"relays": [{"id": "ab", "kind": "folder", "path": "/a"}, {"id": "ab", "kind": "folder", "path": "/b"}]}',
        '{"relays": [{"id": "ab", "kind": "folder", "path": "/a", "serve": true},'
        ' {"id": "cd", "kind": "folder", "path": "/b", "serve": true}]}',
    ):
        path.write_text(bad)
        assert relay_config.read_list(path) is None, bad


def test_write_list_round_trips_in_sorted_keys_and_refuses_two_servers(tmp_path):
    path = tmp_path / "deeper" / "relay.json"
    entries = [RelayEntry("0a1b2c3d", "folder", "~/Cloud", "cloud", True), RelayEntry("deadbeef", "lan", "http://h:2")]
    relay_config.write_list(path, entries)
    assert path.read_text() == (
        '{"relays": [{"id": "0a1b2c3d", "kind": "folder", "label": "cloud", "path": "~/Cloud", "serve": true}, '
        '{"id": "deadbeef", "kind": "lan", "url": "http://h:2"}]}\n')
    assert relay_config.read_list(path) == entries
    two = [RelayEntry("aa", "folder", "/a", "", True), RelayEntry("bb", "folder", "/b", "", True)]
    with pytest.raises(ValueError, match="only one relay can serve"):
        relay_config.write_list(path, two)
    assert relay_config.read_list(path) == entries, "a refused write leaves the file"


RUST = pathlib.Path(__file__).resolve().parents[2] / "disconect-core" / "target" / "debug" / "disconect-core"


@pytest.mark.skipif(not RUST.exists(), reason="build projects/disconect-core first (cargo build)")
def test_write_list_bytes_equal_a_file_the_rust_cli_wrote(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DISCONECT_KEYCHAIN": "fail",
           "DISCONECT_DB": str(tmp_path / "rust" / "x.db")}
    (tmp_path / "rust").mkdir()
    for argv in (["~/Cloud", "--label", "cloud", "--serve"], ["http://h:2"], ["/b", "--label", "second"]):
        done = subprocess.run([str(RUST), "sync", "relay", "add", *argv], env=env, capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
    written = (tmp_path / "rust" / "relay.json").read_bytes()
    entries = relay_config.read_list(tmp_path / "rust" / "relay.json")
    assert [(e.kind, e.value, e.label, e.serve) for e in entries] == [
        ("folder", "~/Cloud", "cloud", True), ("lan", "http://h:2", "", False), ("folder", "/b", "second", False)]
    assert all(re.fullmatch(r"[0-9a-f]{8}", e.id) for e in entries)
    again = tmp_path / "again" / "relay.json"
    relay_config.write_list(again, entries)
    assert again.read_bytes() == written


def test_new_ids_are_eight_hex_and_the_serve_and_lan_helpers_pick_the_right_entry():
    ident = relay_config.new_id()
    assert re.fullmatch(r"[0-9a-f]{8}", ident), ident
    assert relay_config.valid_id(ident) and relay_config.valid_id("default")
    assert not relay_config.valid_id("Default") and not relay_config.valid_id("xyz") and not relay_config.valid_id("")
    assert not relay_config.valid_id("ab\n")
    entries = [RelayEntry("aa", "lan", "http://h:2"), RelayEntry("bb", "folder", "/b", "", True),
               RelayEntry("cc", "lan", "http://k:3")]
    assert relay_config.serve_entry(entries).id == "bb"
    assert relay_config.first_lan_url(entries) == "http://h:2"
    assert relay_config.serve_entry(entries[:1]) is None
    assert relay_config.first_lan_url(entries[1:2]) is None
    with pytest.raises(relay_config.UnsupportedTransport):
        relay_config.open_entry(entries[0])
    assert isinstance(relay_config.open_entry(entries[1]), FolderRelay)


# ---------------------------------------------------------------- helpers
def _device(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    db = tmp_path / f"{name}.db"
    with storage.open_for_write(db, "test"):
        pass
    return db


def _import_day(db: pathlib.Path, tmp_path: pathlib.Path, day: str, steps: int, end: str) -> None:
    record = _uds(day, steps, 50)
    record["wellnessEndTimeGmt"] = end
    root = tmp_path / f"x-{day}-{steps}"
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir(parents=True)
    (agg / f"UDSFile_{day}_{day}.json").write_text(json.dumps([record]))
    (root / "DI_CONNECT" / "DI-Connect-Uploaded-Files").mkdir()
    _import(db, root)


def _specs(*roots: pathlib.Path) -> list[SiteSpec]:
    return [SiteSpec(f"{i + 1:08x}", "folder", str(root)) for i, root in enumerate(roots)]


def _three_roots(tmp_path: pathlib.Path) -> list[pathlib.Path]:
    roots = [tmp_path / f"site{i}" for i in (1, 2, 3)]
    for root in roots:
        root.mkdir()
    return roots


def _open(*roots: pathlib.Path):
    sites, reports = open_sites(_specs(*roots), MASTER)
    assert all(report.error is None for report in reports), reports
    return sites, reports


def _fresh(count: int) -> list[SiteReport]:
    return [SiteReport(f"{i + 1:08x}", "folder") for i in range(count)]


def _listing(root: pathlib.Path) -> list[str]:
    return FolderRelay(root, create_root=True).list(ACCOUNT)


def _empty_site(root: pathlib.Path) -> None:
    shutil.rmtree(root / ACCOUNT)


def _push_all(db, sites, reports):
    with storage.open_for_write(db, "sync") as conn:
        return push_all(conn, MASTER, sites, reports)


def _pull_all(db, sites, reports, hold_rejected=False):
    with storage.open_for_write(db, "sync") as conn:
        return pull_all(conn, MASTER, sites, reports, hold_rejected)


def _count(db, sql: str) -> int:
    conn = storage.open_read_only(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _steps_of(db) -> float:
    return _count(db, "SELECT value FROM daily_metrics WHERE metric='steps' AND source_scope='vendor_cloud'")


# ---------------------------------------------------------------- push, heal, pull
def test_a_push_lands_on_three_sites_under_one_name_and_an_emptied_site_is_healed(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(*roots)
    first = _push_all(a, sites, reports)
    assert (len(first.bundles), first.partial) == (1, False)
    name = first.bundles[0]
    obj = name.split("/", 1)[1]
    blobs = [(root / ACCOUNT / obj).read_bytes() for root in roots]
    assert blobs[0] == blobs[1] == blobs[2], "identical bytes on every site"
    assert all((r.pushed, r.healed, r.behind, r.error) == (1, 0, 0, None) for r in reports)
    for root in roots:
        assert _listing(root) == [name]

    # a second bundle, then site 2 is emptied by hand: the next run heals exactly its two names
    _import_day(a, tmp_path, "2025-06-16", 5000, "2025-06-16T12:00:00.0")
    _push_all(a, sites, _fresh(3))
    _empty_site(roots[1])
    assert _listing(roots[1]) == []
    sites, reports = _open(*roots)
    healed = _push_all(a, sites, reports)
    assert healed.bundles == []
    assert [(r.pushed, r.healed, r.behind) for r in reports] == [(0, 0, 0), (0, 2, 0), (0, 0, 0)]
    assert _listing(roots[1]) == _listing(roots[0]), "the listing shows every pushed name again"
    # a nothing-to-do run touches nothing
    sites, reports = _open(*roots)
    _push_all(a, sites, reports)
    assert all((r.pushed, r.healed, r.behind) == (0, 0, 0) for r in reports)


def test_the_heal_budget_is_sixteen_bundles_a_run_and_the_rest_is_behind(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    sites, _ = _open(roots[0])
    for day in range(1, 21):
        _import_day(a, tmp_path, f"2025-07-{day:02d}", 1000 + day, f"2025-07-{day:02d}T12:00:00.0")
        _push_all(a, sites, _fresh(1))
    assert len(_listing(roots[0])) == 20
    # a second site is added later: it holds nothing yet
    both, reports = _open(roots[0], roots[1])
    _push_all(a, both, reports)
    assert (reports[1].healed, reports[1].behind) == (sync.HEAL_BUNDLES_PER_SITE, 4)
    assert (reports[0].healed, reports[0].behind) == (0, 0)
    assert len(_listing(roots[1])) == 16
    reports = _fresh(2)
    _push_all(a, both, reports)
    assert (reports[1].healed, reports[1].behind) == (4, 0)
    assert _listing(roots[1]) == _listing(roots[0]) and len(_listing(roots[1])) == 20


def test_a_repack_after_a_lost_conflict_leaves_the_retired_record_out_and_a_fresh_store_applies_it(tmp_path):
    roots = _three_roots(tmp_path)
    a, b = _device(tmp_path, "a"), _device(tmp_path, "b")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    _import_day(b, tmp_path, "2025-06-15", 8000, "2025-06-15T21:00:00.0")
    # A publishes to sites 1 and 2; B (the later observation) publishes to site 1 only
    a_sites, a_reports = _open(roots[0], roots[1])
    pushed = _push_all(a, a_sites, a_reports)
    a_name = pushed.bundles[0]
    original = FolderRelay(roots[1], create_root=True).get(a_name)
    assert len(unpack(MASTER, a_name, original)[1]) == 1
    b_sites, b_reports = _open(roots[0])
    _push_all(b, b_sites, b_reports)
    # A pulls the union: its record loses and is retired
    a_sites, a_reports = _open(roots[0], roots[1])
    pulled = _pull_all(a, a_sites, a_reports)
    assert (pulled.conflicts, pulled.status) == (1, "ok")
    assert _steps_of(a) == 8000.0
    # site 2 is emptied; the heal re-packs A's bundle without the retired record
    _empty_site(roots[1])
    reports = _fresh(2)
    _push_all(a, a_sites, reports)
    assert (reports[1].healed, reports[1].behind) == (1, 0)
    repacked = FolderRelay(roots[1], create_root=True).get(a_name)
    assert repacked != original, "a fresh nonce and fewer rows"
    assert unpack(MASTER, a_name, repacked)[1] == [], "the retired loser is left out"
    # a fresh store pulls the healed site, then the site that holds the winner's own bundle
    c = _device(tmp_path, "c")
    c_reports = [SiteReport("00000002", "folder")]
    only_two, _ = open_sites(_specs(roots[1]), MASTER)
    first = _pull_all(c, only_two, c_reports)
    assert (len(first.applied), len(first.rejected), first.status) == (1, 0, "ok")
    assert _count(c, "SELECT count(*) FROM raw_records") == 0
    only_one, _ = open_sites(_specs(roots[0]), MASTER)
    second = _pull_all(c, only_one, c_reports)
    assert second.status == "ok"
    assert _steps_of(c) == 8000.0, "the winner arrives through its own bundle"


def test_a_pull_falls_through_a_bad_copy_to_the_next_site_and_applies_each_name_once(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    both, reports = _open(roots[0], roots[1])
    shared = _push_all(a, both, reports).bundles[0]
    # a second bundle goes to site 2 only
    _import_day(a, tmp_path, "2025-06-16", 5000, "2025-06-16T12:00:00.0")
    second_only, r2 = _open(roots[1])
    lonely = _push_all(a, second_only, r2).bundles[0]
    # site 1's copy of the shared name is damaged
    path = roots[0] / ACCOUNT / shared.split("/", 1)[1]
    damaged = bytearray(path.read_bytes())
    damaged[-1] ^= 0xFF
    path.write_bytes(bytes(damaged))

    b = _device(tmp_path, "b")
    sites, reports = _open(roots[0], roots[1])
    pulled = _pull_all(b, sites, reports)
    assert sorted(pulled.applied) == sorted([shared, lonely]), pulled
    assert pulled.rejected == {}, "a bad copy on one site is not a rejection"
    assert pulled.status == "ok"
    # BL-4b: the counter marks a site only when the name ends rejected; a copy another site satisfied marks nothing
    assert (reports[0].rejected, reports[0].pulled) == (0, 0)
    assert (reports[1].rejected, reports[1].pulled) == (0, 2)
    assert _count(b, "SELECT count(*) FROM relay_bundles WHERE status='rejected'") == 0
    assert _count(b, "SELECT count(*) FROM relay_bundles WHERE direction='pulled' AND status='applied'") == 2


def test_a_name_every_site_rejects_is_rejected_once(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    both, reports = _open(roots[0], roots[1])
    obj = _push_all(a, both, reports).bundles[0].split("/", 1)[1]
    for root in roots[:2]:
        (root / ACCOUNT / obj).write_bytes(b"not a bundle")
    b = _device(tmp_path, "b")
    sites, reports = _open(roots[0], roots[1])
    pulled = _pull_all(b, sites, reports)
    assert (len(pulled.applied), len(pulled.rejected), pulled.status) == (0, 1, "partial")
    assert (reports[0].rejected, reports[1].rejected) == (1, 1)
    assert _count(b, "SELECT count(*) FROM relay_bundles WHERE status='rejected'") == 1


def _booked(db, name: str) -> str:
    conn = storage.open_read_only(db)
    try:
        return conn.execute("SELECT noted_at FROM relay_bundles WHERE name=? AND status='rejected'", (name,)).fetchone()[0]
    finally:
        conn.close()


def test_a_rejected_name_is_not_fetched_again_for_a_day_and_the_run_is_ok(tmp_path, monkeypatch):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-01T10:00:00Z")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0])
    first = _push_all(a, sites, reports).bundles[0]
    (roots[0] / ACCOUNT / first.split("/", 1)[1]).write_bytes(b"not a bundle")
    fetched: list[str] = []
    real_get = FolderRelay.get

    def counting_get(self, name):
        fetched.append(name)
        return real_get(self, name)

    monkeypatch.setattr(FolderRelay, "get", counting_get)
    b = _device(tmp_path, "b")
    sites, reports = _open(roots[0])
    run1 = _pull_all(b, sites, reports, True)
    assert (list(run1.rejected), run1.status, reports[0].rejected) == ([first], "partial", 1)
    assert fetched == [first]
    assert _booked(b, first) == "2026-03-01T10:00:00Z"
    # a run that does not hold (a click, the CLI, the phone) fetches it again at once, rejects it again, and the
    # booking moves to the new time
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-01T10:30:00Z")
    fetched.clear()
    sites, reports = _open(roots[0])
    click = _pull_all(b, sites, reports, False)
    assert (list(click.rejected), click.status, reports[0].rejected) == ([first], "partial", 1)
    assert fetched == [first]
    assert _booked(b, first) == "2026-03-01T10:30:00Z"
    # a second bundle arrives half an hour later: under an automatic run the garbage is held back, the run is clean
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-01T11:00:00Z")
    _import_day(a, tmp_path, "2025-06-16", 5000, "2025-06-16T12:00:00.0")
    second = _push_all(a, sites, _fresh(1)).bundles[0]
    fetched.clear()
    sites, reports = _open(roots[0])
    run2 = _pull_all(b, sites, reports, True)
    assert run2.applied == [second] and run2.rejected == {} and run2.status == "ok"
    assert fetched == [second] and (reports[0].rejected, reports[0].pulled) == (0, 1)
    assert _booked(b, first) == "2026-03-01T10:30:00Z"
    # a day after the booking it is retried once, rejected again, and the booking carries the new time
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-02T10:30:01Z")
    fetched.clear()
    sites, reports = _open(roots[0])
    run3 = _pull_all(b, sites, reports, True)
    assert (list(run3.rejected), run3.status, reports[0].rejected) == ([first], "partial", 1)
    assert fetched == [first]
    assert _booked(b, first) == "2026-03-02T10:30:01Z"


def test_an_io_failure_is_never_held_and_a_booking_in_the_future_is_retried(tmp_path, monkeypatch):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-01T10:00:00Z")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0])
    name = _push_all(a, sites, reports).bundles[0]
    object_path = roots[0] / ACCOUNT / name.split("/", 1)[1]
    good = object_path.read_bytes()
    # run 1: a permission-denied read is booked rejected (a folder error is never transient)
    object_path.chmod(0o000)
    try:
        b = _device(tmp_path, "b")
        sites, reports = _open(roots[0])
        run1 = _pull_all(b, sites, reports, True)
        assert run1.rejected == {name: "PermissionError"} and run1.applied == []
    finally:
        object_path.chmod(0o644)
    # run 2: the read is good again; the I/O reason is not held, so the bundle applies under hold true
    sites, reports = _open(roots[0])
    run2 = _pull_all(b, sites, reports, True)
    assert run2.applied == [name] and run2.rejected == {} and run2.status == "ok"
    # a booking in the future (a clock set back) counts as expired: fetched under hold true, booked at the new time
    object_path.write_bytes(b"not a bundle")
    c = _device(tmp_path, "c")
    sites, reports = _open(roots[0])
    assert list(_pull_all(c, sites, reports, True).rejected) == [name]
    with storage.open_for_write(c, "test") as conn:
        conn.execute("UPDATE relay_bundles SET noted_at='2026-03-05T10:00:00Z' WHERE name=?", (name,))
    sites, reports = _open(roots[0])
    run4 = _pull_all(c, sites, reports, True)
    assert (list(run4.rejected), run4.status, reports[0].rejected) == ([name], "partial", 1)
    assert _booked(c, name) == "2026-03-01T10:00:00Z"
    object_path.write_bytes(good)


def test_a_copy_damaged_on_site_one_and_good_on_site_two_marks_site_one_nothing(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    both, reports = _open(roots[0], roots[1])
    name = _push_all(a, both, reports).bundles[0]
    (roots[0] / ACCOUNT / name.split("/", 1)[1]).write_bytes(b"not a bundle")
    b = _device(tmp_path, "b")
    sites, reports = _open(roots[0], roots[1])
    pulled = _pull_all(b, sites, reports)
    assert pulled.applied == [name] and pulled.rejected == {} and pulled.status == "ok"
    assert (reports[0].rejected, reports[0].pulled) == (0, 0)
    assert (reports[1].rejected, reports[1].pulled) == (0, 1)


def test_a_chains_last_at_is_the_newest_applied_bundles_created_utc(tmp_path, monkeypatch):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    sites, _ = _open(roots[0])
    for day, steps, stamp in (("2025-06-15", 4000, "2026-03-01T10:00:00Z"), ("2025-06-16", 5000, "2026-03-03T08:30:00Z")):
        monkeypatch.setenv("DISCONECT_NOW", stamp)
        _import_day(a, tmp_path, day, steps, f"{day}T12:00:00.0")
        _push_all(a, sites, _fresh(1))
    b = _device(tmp_path, "b")
    monkeypatch.setenv("DISCONECT_NOW", "2026-03-09T00:00:00Z")
    sites, reports = _open(roots[0])
    assert len(_pull_all(b, sites, reports).applied) == 2
    with storage.open_for_write(b, "sync") as conn:
        (chain,) = sync.status(conn)["chains"]
    assert (chain["bundles"], chain["last_at"]) == (2, "2026-03-03T08:30:00Z")


class _Flaky:
    """A relay whose reads fail with a fault in the relay itself (an I/O error), listing included or not."""

    def __init__(self, inner: FolderRelay, fail_get: bool = True):
        self.inner, self.fail_get = inner, fail_get

    def put(self, name, data):
        self.inner.put(name, data)

    def get(self, name):
        if self.fail_get:
            # the stand-in for the Rust core's unreachable network relay (reason word ``OSError``): a folder's own
            # I/O error is never transient on either core, so the fault marks itself
            error = OSError(errno.EIO, "disk fault at /secret/path")
            error.transient = True
            raise error
        return self.inner.get(name)

    def list(self, account):
        return self.inner.list(account)

    def delete(self, name):
        self.inner.delete(name)


def test_a_transient_failure_takes_the_site_out_and_the_name_stays_pending_unless_another_site_has_it(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    both, reports = _open(roots[0], roots[1])
    name = _push_all(a, both, reports).bundles[0]
    flaky = sync.Site("00000001", "folder", _Flaky(FolderRelay(roots[0], create_root=True)), 0)
    good = sync.Site("00000002", "folder", FolderRelay(roots[1], create_root=True), 1)
    # the only site that lists it is out of reach: nothing is booked, the run is partial, the error is a word
    b = _device(tmp_path, "b")
    reports = _fresh(2)
    pulled = _pull_all(b, [flaky], [reports[0]])
    assert (pulled.applied, pulled.rejected, pulled.status) == ([], {}, "partial")
    assert reports[0].error == "unreachable" and "/secret" not in json.dumps(reports[0].as_dict())
    assert _count(b, "SELECT count(*) FROM relay_bundles") == 0
    # another site holds a good copy: the name is applied from it, the failing site is still reported
    reports = _fresh(2)
    pulled = _pull_all(b, [flaky, good], reports)
    assert pulled.applied == [name] and pulled.rejected == {}
    assert (reports[0].error, reports[0].pulled, reports[0].rejected) == ("unreachable", 0, 0)
    assert (reports[1].error, reports[1].pulled) == (None, 1)


def test_open_sites_reports_what_it_cannot_open_and_never_fails(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    afile = tmp_path / "file"
    afile.write_text("x")
    specs = [
        SiteSpec("00000001", "folder", str(real)),
        SiteSpec("00000002", "folder", str(tmp_path / "not mounted")),
        SiteSpec("00000003", "folder", str(real), unavailable=True),
        SiteSpec("00000004", "lan", "https://127.0.0.1:9"),
        SiteSpec("00000005", "lan", "http://127.0.0.1:9"),
        SiteSpec("00000006", "lan", "http://127.0.0.1:9/"),
        SiteSpec("00000007", "lan", "nonsense"),
        SiteSpec("00000008", "folder", str(link)),
        SiteSpec("00000009", "folder", str(afile)),
        SiteSpec("0000000a", "folder", "", unavailable=True),
    ]
    sites, reports = open_sites(specs, MASTER)
    assert [(r.id, r.kind, r.error) for r in reports] == [
        ("00000001", "folder", None),
        ("00000002", "folder", "unavailable"),
        ("00000003", "folder", "unavailable"),
        ("00000004", "lan", "bad_url"),
        ("00000005", "lan", "unsupported_transport"),   # the Rust core opens this one; this core speaks folders only
        ("00000006", "lan", "same_relay"),
        ("00000007", "lan", "bad_url"),
        ("00000008", "folder", "same_relay"),
        ("00000009", "folder", "unavailable"),
        ("0000000a", "folder", "unavailable"),
    ]
    assert [(s.id, s.report, s.kind) for s in sites] == [("00000001", 0, "folder")]
    assert not (tmp_path / "not mounted").exists(), "a missing root is never created"


def test_nothing_is_booked_when_no_site_can_take_a_bundle(tmp_path):
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = open_sites([SiteSpec("00000001", "folder", str(tmp_path / "gone"))], MASTER)
    result = _push_all(a, sites, reports)
    assert result.partial and result.bundles == []
    assert reports[0].error == "unavailable"
    assert _count(a, "SELECT count(*) FROM relay_bundles") == 0
    assert _count(a, "SELECT count(*) FROM relay_seen") == 0
    # an empty site list is the same: nothing packed
    assert _push_all(a, [], []).partial
    # the legacy single folder (create_root) is created by its first put, as ever
    legacy = SiteSpec("default", "folder", str(tmp_path / "made"), create_root=True)
    sites, reports = open_sites([legacy], MASTER)
    result = _push_all(a, sites, reports)
    assert not result.partial and len(result.bundles) == 1
    assert (tmp_path / "made").is_dir()


# ---------------------------------------------------------------- the CLI
def _cli_env(tmp_path, monkeypatch) -> pathlib.Path:
    db = tmp_path / "home" / "x.db"
    db.parent.mkdir()
    monkeypatch.setenv("DISCONECT_DB", str(db))
    return db.parent / "relay.json"


def test_the_relay_cli_adds_lists_and_removes_with_the_rust_words(tmp_path, monkeypatch, capsys):
    path = _cli_env(tmp_path, monkeypatch)
    folder_a, folder_b = tmp_path / "a", tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()

    def run(*argv):
        capsys.readouterr()
        code = cli.main(["--json", "sync", "relay", *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    assert run("list")[0:2] == (0, '{\n  "relays": []\n}\n')
    code, out, _ = run("add", str(folder_a), "--label", "cloud", "--serve")
    assert code == 0
    first = json.loads(out)["added"]
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert run("add", str(folder_b), "--serve")[0::2] == (2, "usage: only one relay can serve\n")
    assert run("add", "http://h:2", "--serve")[0::2] == (2, "usage: only a folder can serve\n")
    assert run("add", str(folder_a))[0::2] == (2, "usage: that relay is already in the list\n")
    link = tmp_path / "alias"
    link.symlink_to(folder_a)
    assert run("add", str(link))[0::2] == (2, "usage: that relay is already in the list\n"), "same folder by inode"
    assert run("add", "https://h:2")[0] == 2
    assert run("add", "http://h:2/")[0] == 0
    assert run("add", "http://h:2")[0::2] == (2, "usage: that relay is already in the list\n")
    assert run("add", str(folder_b), "--label", "second")[0] == 0
    entries = relay_config.read_list(path)
    assert [(e.kind, e.value, e.label, e.serve) for e in entries] == [
        ("folder", str(folder_a), "cloud", True), ("lan", "http://h:2/", "", False), ("folder", str(folder_b), "second", False)]
    code, out, _ = run("list")
    assert json.loads(out)["relays"][0] == {"id": first, "kind": "folder", "label": "cloud", "serve": True, "path": str(folder_a)}
    capsys.readouterr()
    assert cli.main(["sync", "relay", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{first}  folder  {folder_a}  cloud  serves"
    assert lines[1].endswith("  lan  http://h:2/  -") and lines[2].endswith(f"  folder  {folder_b}  second")
    assert run("remove", "nosuch")[0::2] == (2, "usage: no relay with that id\n")
    assert run("remove", entries[1].id)[0] == 0
    assert [e.id for e in relay_config.read_list(path)] == [entries[0].id, entries[2].id]
    for entry in relay_config.read_list(path):
        assert run("remove", entry.id)[0] == 0
    assert not path.exists(), "removing the last entry deletes relay.json"
    assert run("list")[0:2] == (0, '{\n  "relays": []\n}\n')
    capsys.readouterr()
    assert cli.main(["sync", "relay", "list"]) == 0
    assert capsys.readouterr().out == "", "no relay: nothing is printed, not a blank line"
    path.write_text("junk")
    assert run("list")[0::2] == (2, "usage: relay.json is not a relay list this build reads; fix or remove it\n")


def test_sync_push_and_pull_run_over_the_whole_list_and_print_a_line_per_site(tmp_path, monkeypatch, capsys):
    path = _cli_env(tmp_path, monkeypatch)
    db = path.parent / "x.db"
    monkeypatch.setenv("DISCONECT_PASSPHRASE", "a-strong-scratch-passphrase")
    monkeypatch.setattr(cli, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli, "_show_words_once", lambda master: False)
    assert cli.main(["--db", str(db), "key", "init"]) == 0
    export = tmp_path / "export"
    export.mkdir()
    from test_import import _build_export
    _build_export(export)
    assert cli.main(["--db", str(db), "import", str(export)]) == 0
    roots = _three_roots(tmp_path)
    for root in roots[:2]:
        assert cli.main(["sync", "relay", "add", str(root)]) == 0
    entries = relay_config.read_list(path)
    # a single entry prints exactly as before (no site lines); here two entries do
    capsys.readouterr()
    assert cli.main(["--db", str(db), "--json", "sync", "push"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(s["id"], s["pushed"], s["error"]) for s in payload["sites"]] == [(e.id, 1, None) for e in entries]
    assert len(list(roots[0].glob("*/*"))) == len(list(roots[1].glob("*/*"))) == 1
    # a site that is gone: the run still reaches the other and names the failure; a push exits 1 for it, a pull
    # (whose status is only about the bundles) exits 0, as the Rust CLI does
    shutil.rmtree(roots[1])
    capsys.readouterr()
    assert cli.main(["--db", str(db), "sync", "pull"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"  site {entries[0].id} folder: pushed 0, healed 0, behind 0, pulled 0, rejected 0\n" in out + "\n"
    assert f"  site {entries[1].id} folder: pushed 0, healed 0, behind 0, pulled 0, rejected 0, error unavailable" in out
    assert cli.main(["--db", str(db), "sync", "push"]) == cli.EXIT_FAILED
    # one entry only: the legacy single-relay path, no site lines
    assert cli.main(["sync", "relay", "remove", entries[1].id]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "sync", "push"]) == 0
    assert "site " not in capsys.readouterr().out
    # --remember makes --relay the whole list
    assert cli.main(["--db", str(db), "sync", "push", "--relay", str(roots[2]), "--remember"]) == 0
    remembered = relay_config.read_list(path)
    assert [(e.kind, e.value, e.serve) for e in remembered] == [("folder", str(roots[2]), True)]
    assert os.environ["DISCONECT_DB"] == str(db)


# ---------------------------------------------------------------- 19d review fixes (twin of the Rust core's)
def test_heal_ignores_a_stale_account_row(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0])
    first = _push_all(a, sites, reports)
    # a key rotation whose forget failed left the old account's bundle booked and linked
    foreign = f"{'f' * 64}/{'e' * 32}"
    with storage.open_for_write(a, "test") as conn:
        conn.execute("INSERT INTO relay_bundles(name, direction, status, device_id, device_seq, prev, created_utc, "
                     "noted_at, bytes, records) VALUES(?, 'pushed', 'applied', 'deadbeefdeadbeef', 99, NULL, "
                     "'2025-06-15T12:00:00Z', '2025-06-15T12:00:00Z', 10, 1)", (foreign,))
        linked = conn.execute("UPDATE relay_seen SET bundle = ? WHERE bundle = ?", (foreign, first.bundles[0])).rowcount
    assert linked > 0
    sites, again = _open(roots[0])
    _push_all(a, sites, again)     # the run does not raise
    assert (again[0].error, again[0].healed, again[0].behind) == (None, 0, 0)
    assert foreign not in _listing(roots[0]), "the foreign name is never healed"


def test_put_refuses_a_root_that_vanished(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0], roots[1])
    shutil.rmtree(roots[0])    # an unmounted volume
    pushed = _push_all(a, sites, reports)
    assert len(pushed.bundles) == 1, "the other site took it"
    assert (reports[0].error, reports[0].pushed) == ("missing", 0)
    assert (reports[1].error, reports[1].pushed) == (None, 1)
    assert not roots[0].exists(), "nothing recreated the root"
    # the only site: the old FileNotFoundError is raised, nothing created or booked
    b = _device(tmp_path, "b")
    _import_day(b, tmp_path, "2025-06-16", 5000, "2025-06-16T12:00:00.0")
    sites, reports = _open(roots[2])
    shutil.rmtree(roots[2])
    with storage.open_for_write(b, "sync") as conn, pytest.raises(FileNotFoundError):
        sync.push_strict(conn, MASTER, sites, reports)
    assert reports[0].error == "missing"
    assert not roots[2].exists()
    assert _count(b, "SELECT count(*) FROM relay_bundles") == 0
    # the serve / legacy entry may create its root; a plain root creates only the account level
    made = tmp_path / "made"
    FolderRelay(made, create_root=True).put(f"{ACCOUNT}/{'a' * 32}", b"x")
    assert (made / ACCOUNT / ("a" * 32)).is_file()
    plain = tmp_path / "plain"
    plain.mkdir()
    FolderRelay(plain).put(f"{ACCOUNT}/{'a' * 32}", b"x")
    assert (plain / ACCOUNT / ("a" * 32)).is_file()


def test_the_site_words_are_stable():
    from disconect.relay.folder import TooLarge

    transient = OSError(errno.EIO, "x")
    transient.transient = True
    table = [
        (TooLarge("x"), "too_large"),
        (ValueError("bad object name"), "bad_name"),
        (transient, "unreachable"),
        (FileNotFoundError(), "missing"),
        (NotADirectoryError(), "missing"),
        (PermissionError(), "no_permission"),
        (OSError(errno.ECONNREFUSED, "x"), "io_error"),
        (OSError(errno.EIO, "x"), "io_error"),
    ]
    for error, word in table:
        assert sync.site_word(error) == word, error
    # the stored `rejected` reason keeps the class name
    assert sync.reason_of(FileNotFoundError()) == "FileNotFoundError"
    assert sync.reason_of(TooLarge("x")) == "too_large"


def test_a_public_lan_url_is_bad_url():
    def spec(ident, url, check):
        return SiteSpec(ident, "lan", url, check_address=check)

    specs = [
        spec("00000001", "http://8.8.8.8:24816", True),
        spec("00000002", "http://mac.local:24816", True),
        spec("00000003", "http://192.168.1.20:24816", True),
        spec("00000004", "http://127.0.0.1:24816", True),
        spec("00000005", "http://169.254.1.1:24816", True),
        spec("00000006", "http://8.8.4.4:24816", False),
        spec("00000007", "http://192.168.1.21", True),
        spec("00000008", "http://[fd00::1]:24816", True),
        spec("00000009", "http://[2001:db8::1]:24816", True),
    ]
    sites, reports = open_sites(specs, MASTER)
    assert [r.error for r in reports] == ["bad_url", "bad_url", "unsupported_transport", "unsupported_transport",
                                          "bad_url", "unsupported_transport", "bad_url", "unsupported_transport",
                                          "bad_url"]
    assert sites == []


def test_an_evicted_copy_is_not_healed_again(tmp_path):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0])
    first = _push_all(a, sites, reports)
    obj = first.bundles[0].split("/", 1)[1]
    folder = roots[0] / ACCOUNT
    (folder / obj).rename(folder / f".{obj}.icloud")     # iCloud evicts the file: a placeholder stays
    assert _listing(roots[0]) == [], "a get would fail: the listing does not show it"
    sites, again = _open(roots[0])
    _push_all(a, sites, again)
    assert (again[0].healed, again[0].behind, again[0].error) == (0, 0, None)
    assert not (folder / obj).exists(), "no second copy was written beside the placeholder"
    (folder / f".{obj}.icloud").unlink()
    sites, third = _open(roots[0])
    _push_all(a, sites, third)
    assert third[0].healed == 1
    # a pull keeps `list`: an evicted copy is not offered to a reader
    (folder / obj).rename(folder / f".{obj}.icloud")
    assert FolderRelay(roots[0]).list(ACCOUNT) == []
    assert FolderRelay(roots[0]).list_present(ACCOUNT) == [first.bundles[0]]


def test_a_pack_failure_is_the_sites_io_error_and_never_the_runs(tmp_path, monkeypatch):
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0], roots[1])
    _push_all(a, sites, reports)
    _empty_site(roots[1])

    def boom(*args, **kwargs):
        raise ValueError("cannot pack")

    monkeypatch.setattr(sync, "_seal", boom)
    sites, reports = _open(roots[0], roots[1])
    _push_all(a, sites, reports)
    assert (reports[0].error, reports[1].error, reports[1].healed) == (None, "io_error", 0)


def test_an_empty_list_is_no_relay(tmp_path, monkeypatch, capsys):
    path = _cli_env(tmp_path, monkeypatch)
    folder = tmp_path / "s1"
    folder.mkdir()
    path.write_text('{"relays": []}')

    def run(*argv):
        capsys.readouterr()
        code = cli.main([*argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    assert run("sync", "relay", "list")[0:2] == (0, ""), "prints nothing"
    assert json.loads(run("--json", "sync", "relay", "list")[1]) == {"relays": []}
    # add appends to it
    assert run("sync", "relay", "add", str(folder))[0] == 0
    one = json.loads(run("--json", "sync", "relay", "list")[1])
    assert len(one["relays"]) == 1
    # removing the last entry deletes the file, and the next add works
    assert run("sync", "relay", "remove", one["relays"][0]["id"])[0] == 0
    assert not path.exists()
    assert run("sync", "relay", "add", str(folder))[0] == 0 and path.exists()


def test_remember_replaces_a_list_of_one_and_refuses_a_longer_one(tmp_path, monkeypatch, capsys):
    path = _cli_env(tmp_path, monkeypatch)
    db = path.parent / "x.db"
    monkeypatch.setenv("DISCONECT_PASSPHRASE", "a-strong-scratch-passphrase")
    monkeypatch.setattr(cli, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli, "_show_words_once", lambda master: False)
    assert cli.main(["--db", str(db), "key", "init"]) == 0
    s1, s2, s3 = (tmp_path / f"s{i}" for i in (1, 2, 3))
    for folder in (s1, s2, s3):
        folder.mkdir()

    def run(*argv):
        capsys.readouterr()
        code = cli.main(["--db", str(db), *argv])
        out = capsys.readouterr()
        return code, out.err

    # zero entries and one entry: --remember keeps replacing with the one-entry list
    assert run("sync", "push", "--relay", str(s1), "--remember")[0] == 0
    assert run("sync", "push", "--relay", str(s2), "--remember")[0] == 0
    entries = relay_config.read_list(path)
    assert [(e.kind, e.value) for e in entries] == [("folder", str(s2))]
    # two entries: refused, the file untouched
    assert run("sync", "relay", "add", str(s1))[0] == 0
    before = path.read_bytes()
    code, err = run("sync", "push", "--relay", str(s3), "--remember")
    assert (code, err) == (2, "usage: the relay list has more than one entry; use sync relay add\n")
    assert path.read_bytes() == before
    # without --remember a one-off relay still runs over a longer list
    assert run("sync", "push", "--relay", str(s3))[0] == 0


def test_the_site_line_and_rows_carry_the_label(tmp_path):
    line = cli._site_line(SiteReport("0000000a", "folder", label="cloud"))
    assert line == "  site 0000000a cloud folder: pushed 0, healed 0, behind 0, pulled 0, rejected 0"
    assert cli._site_line(SiteReport("0000000a", "folder", error="missing")) == (
        "  site 0000000a folder: pushed 0, healed 0, behind 0, pulled 0, rejected 0, error missing")
    roots = _three_roots(tmp_path)
    spec = SiteSpec("0000000a", "folder", str(roots[0]), label="cloud")
    assert SiteSpec.from_entry(RelayEntry("0000000b", "folder", str(roots[1]), "disk")).label == "disk"
    _, reports = open_sites([spec, SiteSpec("0000000c", "folder", str(tmp_path / "gone"))], MASTER)
    assert [(r.label, r.as_dict()["label"]) for r in reports] == [("cloud", "cloud"), ("", "")]


def test_relay_json_is_written_atomically_with_a_private_parent(tmp_path):
    path = tmp_path / "deeper" / "relay.json"
    relay_config.write_list(path, [RelayEntry("aa", "folder", "/b", "", True)])
    assert path.read_text() == '{"relays": [{"id": "aa", "kind": "folder", "path": "/b", "serve": true}]}\n'
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["relay.json"], "no temp file is left behind"


def test_a_half_applied_name_rejected_on_refetch_is_held_like_a_rejected_one(tmp_path):
    """BL-5 review: the marker kept by BL-5b carries the reason, so the automatic run does not fetch a damaged
    half-applied copy on every tick; a click still does, and a good copy's booking clears the reason."""
    roots = _three_roots(tmp_path)
    a = _device(tmp_path, "a")
    _import_day(a, tmp_path, "2025-06-15", 4000, "2025-06-15T12:00:00.0")
    sites, reports = _open(roots[0])
    name = _push_all(a, sites, reports).bundles[0]
    b = _device(tmp_path, "b")
    sites, reports = _open(roots[0])
    assert _pull_all(b, sites, reports).applied == [name]
    with storage.open_for_write(b, "test") as conn:
        conn.execute("UPDATE relay_bundles SET status='applying' WHERE name=?", (name,))   # a crash before the derive step
    path = roots[0] / name
    good = path.read_bytes()
    bad = bytearray(good)
    bad[len(bad) // 2] ^= 0xFF
    path.write_bytes(bytes(bad))
    sites, reports = _open(roots[0])
    run1 = _pull_all(b, sites, reports, hold_rejected=True)            # a crash-only marker is never held: fetched, rejected
    assert (list(run1.rejected), run1.status, reports[0].rejected) == ([name], "partial", 1)
    assert _count(b, "SELECT count(*) FROM relay_bundles WHERE status='applying' AND reason IS NOT NULL") == 1
    sites, reports = _open(roots[0])
    run2 = _pull_all(b, sites, reports, hold_rejected=True)            # the automatic run holds it: nothing fetched, run clean
    assert (run2.applied, dict(run2.rejected), run2.status, reports[0].rejected, reports[0].pulled) == ([], {}, "ok", 0, 0)
    path.write_bytes(good)
    sites, reports = _open(roots[0])
    run3 = _pull_all(b, sites, reports, hold_rejected=False)           # a click fetches the good copy
    assert (run3.applied, run3.status) == ([name], "ok")
    assert _count(b, "SELECT count(*) FROM relay_bundles WHERE status='applied' AND reason IS NULL") == 1
