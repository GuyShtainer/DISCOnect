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

import pytest

from disconect import storage
from disconect.ingest import connect_export, sources
from disconect.relay import sync
from disconect.relay.folder import FolderRelay
from disconect.storage import keys
from test_converge_10b import (_assert_reparse_quiet, _bio, _daily, _day, _export, _fitness, _hash,
                               _interrupted_export, _load, _metrics_export, _readiness_rows, _split_readiness_export, _wellness_export)
from test_core_parity import BINARY, PASS, _rust_env
from test_relay_atomic import EARLY_STEPS, LATE_STEPS, _count, _hash_of, _steps, _store_with
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
        with storage.open_for_write(puller.db, "sync") as conn, pytest.raises(sync.ConflictWriteFailed):
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
