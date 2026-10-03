# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bet 10b across the two cores: a Rust device and a Python device must agree on every daily row.

The scenarios of ``test_converge_10b`` re-run with some devices on the Rust binary (``import``,
``sync push``, ``sync pull``) and some on the Python oracle, all holding one master key. After every
scenario the daily rows of all devices are equal, and the invariant holds on the Rust-made stores
too: the replay of the raw set (``disconect-core reparse`` on the Rust seats, Python's ``reparse_all`` on
the Python seats) changes no daily row. Bet 12a adds the failed-conflict-write scenario (i). Synthetic data only.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import pathlib
import shutil
import subprocess
import zlib

import pytest

from disconect import storage
from disconect.ingest import connect_export, sources
from disconect.relay import sync
from disconect.relay.folder import FolderRelay
from disconect.storage import keys
from test_converge_10b import (_assert_reparse_quiet, _bio, _daily, _day, _export, _fitness, _hash,
                               _interrupted_export, _load, _metrics_export, _readiness_rows, _split_readiness_export, _wellness_export)
from test_core_parity import BINARY, PASS, _rust_env
from test_relay_atomic import (EARLY_STEPS, LATE_STEPS, _count, _hash_of, _history, _rec, _seen, _steps, _store_many,
                               _store_with, _unsent)
from test_import import _uds

needs_binary = pytest.mark.skipif(not BINARY.exists(), reason="build projects/disconect-core first (cargo build)")
pytestmark = needs_binary


@dataclasses.dataclass
class Device:
    db: pathlib.Path
    core: str   # "py" or "rs"


class Fleet:
    """Devices sharing one master key, each on the core it was given."""

    def __init__(self, base: pathlib.Path):
        keys.set_kdf_params(None)   # production cost: the Rust side enforces the floor
        self.base = base
        self.master: bytes | None = None
        self.first_key: pathlib.Path | None = None
        self.devices: list[Device] = []

    def device(self, name: str, core: str) -> Device:
        db = self.base / f"{name}.db"
        key_file = keys.key_path_for(db)
        if self.first_key is None:
            self.master = keys.create(key_file, PASS)
            self.first_key = key_file
        else:
            shutil.copy(self.first_key, key_file)
        storage.remember(db, self.master)
        with storage.open_for_write(db, "test"):
            pass
        device = Device(db, core)
        self.devices.append(device)
        return device

    def close(self) -> None:
        for device in self.devices:
            storage.forget(device.db)

    @staticmethod
    def _rust(device: Device, *argv: str) -> None:
        done = subprocess.run([str(BINARY), "--db", str(device.db), *argv], env=_rust_env(),
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, f"rust {argv}: {done.stderr[-400:]}"

    def do_import(self, device: Device, source: pathlib.Path) -> None:
        if device.core == "rs":
            self._rust(device, "import", str(source))
            return
        with storage.open_for_write(device.db, "test") as conn:
            sources.import_path(source, conn)

    def push(self, device: Device, relay: FolderRelay) -> None:
        if device.core == "rs":
            self._rust(device, "sync", "push", "--relay", str(relay.root))
            return
        with storage.open_for_write(device.db, "sync") as conn:
            sync.push(conn, self.master, relay)

    def pull(self, device: Device, relay: FolderRelay) -> None:
        if device.core == "rs":
            self._rust(device, "sync", "pull", "--relay", str(relay.root))
            return
        with storage.open_for_write(device.db, "sync") as conn:
            sync.pull(conn, self.master, relay)

    def rounds(self, devices: list[Device], relay: FolderRelay, rounds: int = 3) -> None:
        for _ in range(rounds):
            for device in devices:
                self.push(device, relay)
            for device in devices:
                self.pull(device, relay)

    def ring(self, devices: list[Device], relay: FolderRelay) -> None:
        first, second, third = devices
        self.push(first, relay), self.pull(second, relay), self.push(second, relay)
        self.pull(third, relay), self.push(third, relay), self.pull(first, relay)
        self.rounds(devices, relay)


@pytest.fixture
def fleet(tmp_path):
    fleet = Fleet(tmp_path)
    yield fleet
    fleet.close()


def _converged(devices: list[Device]) -> set[tuple]:
    reference = _daily(devices[0].db)
    for other in devices[1:]:
        rows = _daily(other.db)
        assert rows == reference, (f"{devices[0].core} vs {other.core}: {len(reference ^ rows)} daily rows differ "
                                   f"({sorted(reference ^ rows)[:3]})")
    return reference


def _reparse_runs(db: pathlib.Path) -> int:
    conn = storage.open_read_only(db)
    try:
        return conn.execute("SELECT count(*) FROM import_runs WHERE transport='reparse'").fetchone()[0]
    finally:
        conn.close()


def _assert_rust_reparse_quiet(device: Device) -> None:
    """The 10b invariant on a Rust seat, replayed by the Rust binary itself: a run row, no daily row changed."""
    before, runs = _daily(device.db, with_observed=True), _reparse_runs(device.db)
    done = subprocess.run([str(BINARY), "--db", str(device.db), "--json", "reparse"], env=_rust_env(),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"rust reparse: {done.stderr[-400:]}"
    stats = json.loads(done.stdout)
    assert stats["status"] == "ok" and stats["transport"] == "reparse"
    assert _reparse_runs(device.db) == runs + 1, "the reparse booked its run row"
    assert _daily(device.db, with_observed=True) == before, "disconect-core reparse changed a daily row"


def _quiet(devices: list[Device]) -> None:
    for device in devices:
        if device.core == "rs":
            _assert_rust_reparse_quiet(device)
        else:
            _assert_reparse_quiet(device.db)


# a: the 3-device ring, one Rust device in each seat, both import orders
@pytest.mark.parametrize("rust_seat,names", list(itertools.product(range(3), (("X", "Y", "Z"), ("Z", "Y", "X")))))
def test_ring_with_one_rust_device(tmp_path, fleet, rust_seat, names):
    exports = {name: _export(tmp_path / f"export_{name}", name) for name in ("X", "Y", "Z")}
    devices = [fleet.device(letter, "rs" if seat == rust_seat else "py") for seat, letter in enumerate("abc")]
    for device, name in zip(devices, names):
        fleet.do_import(device, exports[name])
    _quiet(devices)   # after the imports themselves
    fleet.ring(devices, FolderRelay(tmp_path / "relay"))
    _converged(devices)
    _quiet(devices)   # after the pulls


# c: equal-instant ties
@pytest.mark.parametrize("cores", [("rs", "py"), ("py", "rs")])
@pytest.mark.parametrize("order", [0, 1])
def test_ties(tmp_path, fleet, cores, order):
    x = _wellness_export(tmp_path / "x", [_fitness("2025-07-02", "00:05:00", 33.5)], [_bio("2025-07-02", 72500.0, 1)])
    y = _wellness_export(tmp_path / "y", [_fitness("2025-07-02", "12:00:00", 33.0)], [_bio("2025-07-02", 72000.0, 2)])
    first, second = ((x, y), (y, x))[order]
    a, b = fleet.device("a", cores[0]), fleet.device("b", cores[1])
    fleet.do_import(a, first), fleet.do_import(b, second)
    relay = FolderRelay(tmp_path / "relay")
    fleet.push(a, relay), fleet.push(b, relay), fleet.pull(a, relay), fleet.pull(b, relay)
    rows = _converged([a, b])
    assert {row[2] for row in rows} == {"fitness_age", "weight_kg"}
    _quiet([a, b])


# d: relay supersession restores the runner-up, both hash orders
@pytest.mark.parametrize("challenger_wins", [True, False])
@pytest.mark.parametrize("rust_seat", [0, 1, 2])
def test_supersession(tmp_path, fleet, challenger_wins, rust_seat):
    early = _load(_day(3), 0, 180)
    contested = _load(_day(3), 2 * 3600_000, 190)
    challenger = dict(contested)
    challenger.pop("dailyTrainingLoadAcute")
    challenger["dailyTrainingLoadChronic"] = 150
    for device_id in range(1000, 2000):
        challenger["deviceId"] = device_id
        if (_hash(challenger) > _hash(contested)) == challenger_wins:
            break
    sources_ = (_metrics_export(tmp_path / "x", [early]), _metrics_export(tmp_path / "y", [early, contested]),
                _metrics_export(tmp_path / "z", [early, challenger]))
    devices = [fleet.device(letter, "rs" if seat == rust_seat else "py") for seat, letter in enumerate("abc")]
    for device, source in zip(devices, sources_):
        fleet.do_import(device, source)
    fleet.ring(devices, FolderRelay(tmp_path / "relay"))
    rows = _converged(devices)
    acute = {row[5] for row in rows if row[2] == "training_load_acute"}
    assert acute == ({180.0} if challenger_wins else {190.0})
    _quiet(devices)


# e: a null instant beside a stamped record
@pytest.mark.parametrize("cores", [("rs", "py"), ("py", "rs")])
@pytest.mark.parametrize("order", [0, 1])
def test_null_instant(tmp_path, fleet, cores, order):
    undated = _load(_day(3), 0, 170)
    undated["timestamp"] = None
    x = _metrics_export(tmp_path / "x", [undated])
    y = _metrics_export(tmp_path / "y", [_load(_day(3), 2 * 3600_000, 190)])
    first, second = ((x, y), (y, x))[order]
    a, b = fleet.device("a", cores[0]), fleet.device("b", cores[1])
    fleet.do_import(a, first), fleet.do_import(b, second)
    relay = FolderRelay(tmp_path / "relay")
    fleet.push(a, relay), fleet.push(b, relay), fleet.pull(a, relay), fleet.pull(b, relay)
    rows = _converged([a, b])
    assert len({row[5] for row in rows if row[2] == "training_load_acute"}) == 1
    _quiet([a, b])


# f: one importer and a fresh puller, readiness split over two window files
@pytest.mark.parametrize("importer,puller", [("py", "rs"), ("rs", "py")])
def test_split_readiness_importer_and_fresh_puller(tmp_path, fleet, importer, puller):
    desktop, phone = fleet.device("desktop", importer), fleet.device("phone", puller)
    relay = FolderRelay(tmp_path / "relay")
    fleet.do_import(desktop, _split_readiness_export(tmp_path / "export"))
    fleet.push(desktop, relay)
    fleet.pull(phone, relay)
    on_desktop, on_phone = _readiness_rows(desktop.db), _readiness_rows(phone.db)
    assert on_desktop and on_desktop == on_phone
    assert {row[5] for row in on_desktop if row[2] == "training_readiness"} == {65.0}
    _converged([desktop, phone])
    _quiet([desktop, phone])


# g: a pull that carries readiness only equals a reparse of the whole raw set
@pytest.mark.parametrize("feeder_core,store_core", [("py", "rs"), ("rs", "py")])
def test_readiness_only_pull(tmp_path, fleet, feeder_core, store_core):
    feeder, store = fleet.device("feeder", feeder_core), fleet.device("store", store_core)
    whole = fleet.device("whole", "py")
    relay = FolderRelay(tmp_path / "relay")
    readiness = _split_readiness_export(tmp_path / "readiness_export")
    full = _export(tmp_path / "full_export", "Y")
    fleet.do_import(feeder, readiness)
    fleet.do_import(store, full)
    fleet.push(feeder, relay)
    fleet.pull(store, relay)
    fleet.do_import(whole, full), fleet.do_import(whole, readiness)
    _converged([store, whole])
    _quiet([store, whole])


# h: an interrupted import, retried on either core, equals a clean import
@pytest.mark.parametrize("retry_core", ["py", "rs"])
def test_kill_mid_import_then_reimport(tmp_path, fleet, monkeypatch, retry_core):
    export = _interrupted_export(tmp_path / "export")
    killed, clean = fleet.device("killed", retry_core), fleet.device("clean", "py")
    real, calls = connect_export._import_entry, []

    def kill_on_third_file(entry, stream, writer):
        calls.append(stream)
        if len(calls) == 3:
            raise KeyboardInterrupt("simulated kill between files")
        return real(entry, stream, writer)

    monkeypatch.setattr(connect_export, "_import_entry", kill_on_third_file)
    with pytest.raises(KeyboardInterrupt):   # the kill is always the Python import; the retry is on `retry_core`
        with storage.open_for_write(killed.db, "test") as conn:
            sources.import_path(export, conn)
    monkeypatch.setattr(connect_export, "_import_entry", real)
    fleet.do_import(killed, export)
    fleet.do_import(clean, export)
    _converged([clean, killed])
    _quiet([killed])


@pytest.mark.parametrize("retry_core", ["py", "rs"])
def test_crash_in_import_rederive_then_reimport(tmp_path, fleet, monkeypatch, retry_core):
    export = _interrupted_export(tmp_path / "export")
    killed, clean = fleet.device("killed", retry_core), fleet.device("clean", "py")
    real = sources.rederive_json

    def kill(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated kill inside the re-derive")

    monkeypatch.setattr(sources, "rederive_json", kill)
    with pytest.raises(KeyboardInterrupt):
        with storage.open_for_write(killed.db, "test") as conn:
            sources.import_path(export, conn)
    monkeypatch.setattr(sources, "rederive_json", real)
    fleet.do_import(killed, export)
    fleet.do_import(clean, export)
    _converged([clean, killed])
    _quiet([killed])


# i: a storage error while a conflict winner is written is an error, the loser survives, the retry converges
@pytest.mark.parametrize("feeder_core,puller_core", [("py", "rs"), ("rs", "py")])
def test_failed_conflict_write_leaves_the_loser_and_the_retry_converges(tmp_path, fleet, feeder_core, puller_core):
    early = _uds("2025-06-15", EARLY_STEPS, 50)
    early["wellnessEndTimeGmt"] = "2025-06-15T12:00:00.0"
    late = _uds("2025-06-15", LATE_STEPS, 50)
    late["wellnessEndTimeGmt"] = "2025-06-15T21:00:00.0"
    feeder, puller = fleet.device("feeder", feeder_core), fleet.device("puller", puller_core)
    fleet.do_import(puller, _store_with(tmp_path / "x", early))
    fleet.do_import(feeder, _store_with(tmp_path / "y", late))
    relay = FolderRelay(tmp_path / "relay")
    fleet.push(feeder, relay), fleet.push(puller, relay)
    incoming, loser = _hash_of(feeder.db), _hash_of(puller.db)
    # a permanent trigger (a TEMP one would not survive into the Rust process) refuses the winner's raw row
    with storage.open_for_write(puller.db, "test") as conn:
        conn.execute("CREATE TRIGGER inject_12a BEFORE INSERT ON raw_records "
                     f"WHEN NEW.payload_hash='{incoming}' BEGIN SELECT RAISE(ABORT,'injected'); END")
        conn.commit()
    if puller_core == "rs":
        done = subprocess.run([str(BINARY), "--db", str(puller.db), "sync", "pull", "--relay", str(relay.root)],
                              env=_rust_env(), capture_output=True, text=True, timeout=300)
        assert done.returncode == 6, f"the pull must fail as a database error: {done.returncode} {done.stderr[-300:]}"
    else:
        with storage.open_for_write(puller.db, "sync") as conn, pytest.raises(sync.RecordWriteFailed):
            sync.pull(conn, fleet.master, relay)
    with storage.open_for_write(puller.db, "test") as conn:
        assert conn.execute("SELECT payload_hash FROM raw_records WHERE stream='json:uds'").fetchall() == [(loser,)]
        assert _steps(conn) == EARLY_STEPS
        for table in ("sync_conflicts", "raw_superseded"):
            assert _count(conn, f"SELECT count(*) FROM {table}") == 0, f"orphan {table} row"
        assert [r[0] for r in conn.execute("SELECT status FROM relay_bundles WHERE direction='pulled'")] == ["applying"]
        conn.execute("DROP TRIGGER inject_12a")
        conn.commit()
    fleet.rounds([feeder, puller], relay)
    rows = _converged([feeder, puller])
    assert {row[5] for row in rows if row[2] == "steps" and row[3] == "vendor_cloud"} == {float(LATE_STEPS)}
    _quiet([feeder, puller])


# ---- opus review of 12a (F1-F4, F6) on every core pairing ---------------------------------------

CORE_PAIRS = [("py", "rs"), ("rs", "py"), ("py", "py"), ("rs", "rs")]


def _trigger(device: Device, name: str, sql: str) -> None:
    """A permanent trigger: a TEMP one would not survive into the Rust process."""
    with storage.open_for_write(device.db, "test") as conn:
        conn.execute(f"CREATE TRIGGER {name} {sql}")
        conn.commit()


def _drop_trigger(device: Device, name: str) -> None:
    with storage.open_for_write(device.db, "test") as conn:
        conn.execute(f"DROP TRIGGER {name}")
        conn.commit()


def _pull_fails(fleet: Fleet, device: Device, relay: FolderRelay) -> None:
    """The pull fails as a database error (exit 6 on the Rust seat)."""
    if device.core == "rs":
        done = subprocess.run([str(BINARY), "--db", str(device.db), "sync", "pull", "--relay", str(relay.root)],
                              env=_rust_env(), capture_output=True, text=True, timeout=300)
        assert done.returncode == 6, f"the pull must fail as a database error: {done.returncode} {done.stderr[-300:]}"
        return
    with storage.open_for_write(device.db, "sync") as conn, pytest.raises((sync.RecordWriteFailed, storage.sqlite.Error)):
        sync.pull(conn, fleet.master, relay)


def _read(device: Device, sql: str) -> list:
    conn = storage.open_read_only(device.db)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _pair_of_devices(tmp_path, fleet, feeder_core, puller_core, puller_records):
    """The feeder holds the late 06-15 observation, the puller ``puller_records`` (the early 06-15 one among them)."""
    feeder, puller = fleet.device("feeder", feeder_core), fleet.device("puller", puller_core)
    fleet.do_import(puller, _store_many(tmp_path / "x", puller_records))
    fleet.do_import(feeder, _store_with(tmp_path / "y", _rec("2025-06-15", LATE_STEPS, "21")))
    relay = FolderRelay(tmp_path / "relay")
    fleet.push(feeder, relay), fleet.push(puller, relay)
    return feeder, puller, relay


# F1: the winner reuses the retired loser's rowid (the loser was the max id) and must still be marked seen
@pytest.mark.parametrize("feeder_core,puller_core", CORE_PAIRS)
def test_f1_conflict_winner_is_marked_seen_on_every_core(tmp_path, fleet, feeder_core, puller_core):
    feeder, puller, relay = _pair_of_devices(tmp_path, fleet, feeder_core, puller_core, [_rec("2025-06-15", EARLY_STEPS, "12")])
    fleet.pull(puller, relay)
    conn = storage.open_read_only(puller.db)
    try:
        assert _seen(conn, _hash_of(feeder.db)) == 1
        assert _unsent(conn) == 0
    finally:
        conn.close()
    fleet.rounds([feeder, puller], relay)
    _converged([feeder, puller])
    _quiet([feeder, puller])


# F2: the winner is stored but its relay_seen row was refused; the retry must still mark it
@pytest.mark.parametrize("feeder_core,puller_core", [("py", "rs"), ("rs", "py")])
def test_f2_retried_pull_marks_the_stored_winner_seen(tmp_path, fleet, feeder_core, puller_core):
    feeder, puller, relay = _pair_of_devices(
        tmp_path, fleet, feeder_core, puller_core, [_rec("2025-06-15", EARLY_STEPS, "12"), _rec("2025-06-16", 3000, "21")])
    incoming = _hash_of(feeder.db)
    _trigger(puller, "inject_f2", "BEFORE INSERT ON relay_seen WHEN (SELECT payload_hash FROM raw_records "
                                  f"WHERE id=NEW.raw_record_id)='{incoming}' BEGIN SELECT RAISE(ABORT,'crash after commit'); END")
    _pull_fails(fleet, puller, relay)
    assert _read(puller, "SELECT status FROM relay_bundles WHERE direction='pulled'") == [("applying",)]
    _drop_trigger(puller, "inject_f2")
    fleet.pull(puller, relay)
    conn = storage.open_read_only(puller.db)
    try:
        assert _seen(conn, incoming) == 1
        assert _history_of(conn) == (1, 1), "no duplicate conflict row"
        assert _unsent(conn) == 0
    finally:
        conn.close()
    fleet.rounds([feeder, puller], relay)
    _converged([feeder, puller])


def _history_of(conn) -> tuple[int, int]:
    return _count(conn, "SELECT count(*) FROM sync_conflicts"), _count(conn, "SELECT count(*) FROM raw_superseded")


# F3: a plain (non-conflict) record whose write is refused keeps its bundle applying; the retry converges
@pytest.mark.parametrize("abort", ["ABORT", "ROLLBACK"])
@pytest.mark.parametrize("feeder_core,puller_core", CORE_PAIRS)
def test_f3_plain_record_storage_failure_is_retried(tmp_path, fleet, feeder_core, puller_core, abort):
    feeder, puller = fleet.device("feeder", feeder_core), fleet.device("puller", puller_core)
    fleet.do_import(feeder, _store_with(tmp_path / "y", _rec("2025-06-15", LATE_STEPS, "21")))
    relay = FolderRelay(tmp_path / "relay")
    fleet.push(feeder, relay)
    incoming = _hash_of(feeder.db)
    _trigger(puller, "inject_f3", f"BEFORE INSERT ON raw_records WHEN NEW.payload_hash='{incoming}' BEGIN SELECT RAISE({abort},'injected'); END")
    _pull_fails(fleet, puller, relay)
    assert _read(puller, "SELECT status FROM relay_bundles WHERE direction='pulled'") == [("applying",)]
    assert _read(puller, "SELECT count(*) FROM raw_records") == [(0,)]
    _drop_trigger(puller, "inject_f3")
    fleet.rounds([feeder, puller], relay)
    _converged([feeder, puller])
    assert _read(puller, "SELECT status FROM relay_bundles WHERE direction='pulled'")[0] == ("applied",)
    _quiet([feeder, puller])


# F4: the failing bundle carries an incoming loser ahead of the failing winner; no duplicate history rows after the retry
@pytest.mark.parametrize("feeder_core,puller_core", [("py", "py"), ("py", "rs"), ("rs", "py")])
def test_f4_reapplied_bundle_keeps_one_history_row_per_conflict(tmp_path, fleet, feeder_core, puller_core):
    feeder, puller, third = fleet.device("feeder", feeder_core), fleet.device("puller", puller_core), fleet.device("third", "py")
    fleet.do_import(feeder, _store_many(tmp_path / "f", [_rec("2025-06-14", 1000, "12"), _rec("2025-06-15", LATE_STEPS, "21")]))
    fleet.do_import(puller, _store_many(tmp_path / "p", [_rec("2025-06-14", 2000, "21"), _rec("2025-06-15", EARLY_STEPS, "12")]))
    fleet.do_import(third, _store_many(tmp_path / "t", [_rec("2025-06-16", 3000, "21")]))
    relay = FolderRelay(tmp_path / "relay")
    for device in (feeder, puller, third):
        fleet.push(device, relay)
    winner = _read(feeder, "SELECT payload_hash FROM raw_records WHERE source_key LIKE '%2025-06-15%'")[0][0]
    _trigger(puller, "inject_f4", f"BEFORE INSERT ON raw_records WHEN NEW.payload_hash='{winner}' BEGIN SELECT RAISE(ABORT,'x'); END")
    _pull_fails(fleet, puller, relay)
    _drop_trigger(puller, "inject_f4")
    fleet.pull(puller, relay), fleet.pull(third, relay)
    fleet.rounds([feeder, puller, third], relay)
    histories = {device.db.stem: tuple(_read(device, "SELECT (SELECT count(*) FROM sync_conflicts), "
                                                     "(SELECT count(*) FROM raw_superseded)")[0])
                 for device in (feeder, puller, third)}
    assert len(set(histories.values())) == 1, histories
    assert histories["puller"][0] == histories["puller"][1]
    _converged([feeder, puller, third])
    _quiet([feeder, puller, third])


# F6: a corrupt retained record fails the reparse dry run with the same JSON failure entry on both cores
def test_f6_corrupt_record_reparse_failures_carry_the_same_keys(tmp_path, fleet):
    payloads = {}
    for core in ("py", "rs"):
        device = fleet.device(core, core)
        fleet.do_import(device, _store_with(tmp_path / core, _rec("2025-06-15", LATE_STEPS, "21")))
        with storage.open_for_write(device.db, "test") as conn:
            conn.execute("UPDATE raw_records SET payload=?", (zlib.compress(b"this is not json"),))
            conn.commit()
        if core == "rs":
            done = subprocess.run([str(BINARY), "--db", str(device.db), "--json", "reparse"], env=_rust_env(),
                                  capture_output=True, text=True, timeout=300)
            assert done.returncode == 1, done.stderr[-300:]
            payloads[core] = json.loads(done.stdout)
        else:
            with storage.open_for_write(device.db, "reparse") as conn:
                stats = sources.reparse_all(conn)
            payloads[core] = {"failures": stats.failures, "files_failed": stats.files_failed, "status": stats.status()}
    py, rs = payloads["py"]["failures"], payloads["rs"]["failures"]
    assert len(py) == len(rs) == 1
    assert sorted(py[0]) == sorted(rs[0]) == ["error", "file", "kind", "stream"]
    for key in ("file", "kind", "stream"):
        assert py[0][key] == rs[0][key], key   # the error wording differs (kb/22 #9)
    assert payloads["py"]["status"] == payloads["rs"]["status"] == "failed"
