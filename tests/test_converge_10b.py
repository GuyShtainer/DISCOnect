"""Bet 10b: daily rows are a pure function of the converged raw set.

Devices that imported different account exports converge on raw records through the relay; these
tests hold that the *daily facts* converge too, in every arrival order, and that
``reparse_all`` after an import or a pull never changes a daily row. Synthetic data only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib

import pytest

from disconect import storage
from disconect.ingest import connect_export, sources
from disconect.ingest.clock import ClockOffsets
from disconect.ingest.writer import Writer
from disconect.relay.folder import FolderRelay
from test_connect_metrics import _bio_metrics, _endurance, _fitness_age, _training_load
from test_import import _build_export, _readiness, _sleep_json, _uds
from test_relay import _import, _pull, _push

UTC = datetime.timezone.utc
DAYS = [f"2025-07-0{i}" for i in range(1, 7)]


# ---------------------------------------------------------------- synthetic exports
def _day(number: int) -> datetime.datetime:
    return datetime.datetime(2025, 7, number, tzinfo=UTC)


def _ms(moment: datetime.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _load(day: datetime.datetime, offset_ms: int, acute: int) -> dict:
    record = _training_load(day, acute, 140, 1.2, "OPTIMAL")
    record["timestamp"] = _ms(day) + offset_ms
    return record


def _endure(day: datetime.datetime, offset_ms: int, score: int) -> dict:
    record = _endurance(day, score)
    record["timestamp"] = _ms(day) + offset_ms
    return record


def _fitness(date: str, create: str, age: float) -> dict:
    record = _fitness_age(date, age)
    record["createTimestamp"] = f"{date}T{create}.0"
    return record


def _bio(date: str, weight: float, version: int, clock: str = "00:00:00") -> dict:
    record = _bio_metrics(date, weight, version)
    record["weight"]["timestampGMT"] = f"{date}T{clock}.0"
    return record


def _vo2(date: str, clock: str, value: float) -> dict:
    return {"calendarDate": date, "updateTimestamp": f"{date}T{clock}.0", "vo2MaxValue": value, "sport": "RUNNING"}


def _sections(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    connect = root / "DI_CONNECT"
    folders = (connect / "DI-Connect-Aggregator", connect / "DI-Connect-Wellness", connect / "DI-Connect-Metrics")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    return folders


def _export(root: pathlib.Path, variant: str) -> pathlib.Path:
    """Three exports of 'the same account' that disagree on the records a day has."""
    aggregator, wellness, metrics = _sections(root)
    uds = [_uds(day, 8000 + i, 50) for i, day in enumerate(DAYS)]
    sleep = [_sleep_json(day, _day(i + 1) + datetime.timedelta(hours=5), 70 + i) for i, day in enumerate(DAYS)]
    readiness = {day: [_readiness(day, f"{day}T04:00:00.0", "AFTER_WAKEUP_RESET", 60 + i)] for i, day in enumerate(DAYS)}
    vo2 = [_vo2(day, "04:30:00", 50.0) for day in DAYS]
    load = [_load(_day(i + 1), 0, 180 + i) for i in range(6)]
    endurance = [_endure(_day(i + 1), 0, 60 + i) for i in range(6)]
    fitness = [_fitness("2025-07-02", "00:05:00", 33.5)]
    bio = [_bio("2025-07-02", 72500.0, 1)]
    if variant == "Y":
        uds[5]["totalSteps"] += 900
        readiness["2025-07-05"].append(_readiness("2025-07-05", "2025-07-05T15:00:00.0", "UPDATE_REALTIME_VARIABLES", 40))
        vo2.append(_vo2("2025-07-03", "18:00:00", 51.0))
        load.append(_load(_day(4), 1000, 181))
        endurance.append(_endure(_day(4), 3600_000, 70))
        fitness.append(_fitness("2025-07-02", "12:00:00", 33.0))
        bio.append(_bio("2025-07-02", 72000.0, 2))
    if variant == "Z":
        vo2.append(_vo2("2025-07-03", "12:00:00", 49.0))
        load.append(_load(_day(4), 5 * 3600_000, 175))
        readiness["2025-07-04"].append(_readiness("2025-07-04", "2025-07-04T18:00:00.0", "POST_EXERCISE_RESET", 30))
        bio.append(_bio("2025-07-02", 71500.0, 3, "07:00:00"))
        sleep[5]["sleepScores"]["overallScore"] = 99
    (aggregator / "UDSFile_2025-07-01_2025-07-06.json").write_text(json.dumps(uds))
    (wellness / "2025-07-01_2025-07-06_111_sleepData.json").write_text(json.dumps(sleep))
    (metrics / "TrainingReadinessDTO_20250701_20250706_1.json").write_text(
        json.dumps([record for day in DAYS for record in readiness[day]]))
    (metrics / "MetricsMaxMetData_20250701_20250706_1.json").write_text(json.dumps(vo2))
    (metrics / "MetricsAcuteTrainingLoad_20250701_20250706_1.json").write_text(json.dumps(load))
    (metrics / "EnduranceScore_20250701_20250706_1.json").write_text(json.dumps(endurance))
    (wellness / "111_fitnessAgeData.json").write_text(json.dumps(fitness))
    (wellness / "111_userBioMetrics.json").write_text(json.dumps(bio))
    return root


def _metrics_export(root: pathlib.Path, load: list[dict]) -> pathlib.Path:
    _aggregator, _wellness, metrics = _sections(root)
    (metrics / "MetricsAcuteTrainingLoad_20250701_20250706_1.json").write_text(json.dumps(load))
    return root


def _wellness_export(root: pathlib.Path, fitness: list[dict], bio: list[dict]) -> pathlib.Path:
    _aggregator, wellness, _metrics = _sections(root)
    (wellness / "111_fitnessAgeData.json").write_text(json.dumps(fitness))
    (wellness / "111_userBioMetrics.json").write_text(json.dumps(bio))
    return root


def _readiness_export(root: pathlib.Path, windows: list[list[dict]]) -> pathlib.Path:
    """One readiness file per window: a day's records may be spread over several files."""
    _aggregator, _wellness, metrics = _sections(root)
    for index, records in enumerate(windows, start=1):
        (metrics / f"TrainingReadinessDTO_2025070{index}_2025070{index + 5}_{index}.json").write_text(json.dumps(records))
    return root


# ---------------------------------------------------------------- reading the stores
def _daily(db_path: pathlib.Path, with_observed: bool = False) -> set[tuple]:
    """Every daily row as (table, date, metric, scope, device, value/label[, observed]); no ids.

    Sleep sessions join as ``("sleep_sessions", date, "sleep_session", scope, device, summary)``
    (keyed by the natural ``sleep_id``, never the integer id), so the same filters on ``row[2]``
    and ``row[5]`` keep working.
    """
    conn = storage.open_read_only(db_path)
    try:
        rows: set[tuple] = set()
        for table, column in (("daily_metrics", "value"), ("daily_labels", "label")):
            extra = ", observed_utc" if with_observed else ""
            for row in conn.execute(f"SELECT date, metric, source_scope, COALESCE(device_id, ''), {column}{extra} FROM {table}"):
                rows.add((table, *row))
        for date, scope, device, sleep_id, start, end, score, deep in conn.execute(
                "SELECT date, source_scope, COALESCE(device_id, ''), sleep_id, start_utc, end_utc, overall_score, deep_s "
                "FROM sleep_sessions"):
            rows.add(("sleep_sessions", date, "sleep_session", scope, device, f"{sleep_id}|{start}|{end}|{score}|{deep}"))
        return rows
    finally:
        conn.close()


def _reparse(db_path: pathlib.Path) -> None:
    with storage.open_for_write(db_path, "test") as conn:
        sources.reparse_all(conn)


def _assert_reparse_quiet(db_path: pathlib.Path) -> None:
    """The invariant: replaying the raw set changes no daily row (values, labels or observed time)."""
    before = _daily(db_path, with_observed=True)
    _reparse(db_path)
    assert _daily(db_path, with_observed=True) == before, "reparse_all changed a daily row"


def _assert_converged(dbs: list[pathlib.Path]) -> set[tuple]:
    reference = _daily(dbs[0])
    for other in dbs[1:]:
        assert _daily(other) == reference, f"daily rows differ between {dbs[0].name} and {other.name}"
    return reference


def _rounds(dbs: list[pathlib.Path], relay: FolderRelay, rounds: int = 3) -> None:
    for _ in range(rounds):
        for db in dbs:
            _push(db, relay)
        for db in dbs:
            _pull(db, relay)


def _ring(dbs: list[pathlib.Path], relay: FolderRelay) -> None:
    """The attack's ring schedule: A->B->C->A one hop at a time, then full rounds until quiet."""
    first, second, third = dbs
    _push(first, relay), _pull(second, relay), _push(second, relay)
    _pull(third, relay), _push(third, relay), _pull(first, relay)
    _rounds(dbs, relay)


# ---------------------------------------------------------------- a, b: the 3-device ring
@pytest.fixture
def exports(tmp_path):
    return {name: _export(tmp_path / f"export_{name}", name) for name in ("X", "Y", "Z")}


def test_ring_of_three_devices_with_different_exports_converges_in_both_orders(tmp_path, exports):
    outcomes = []
    for order, names in enumerate((("X", "Y", "Z"), ("Z", "Y", "X"))):
        base = tmp_path / f"ring{order}"
        base.mkdir()
        dbs = [base / f"{letter}.db" for letter in "abc"]
        for db, name in zip(dbs, names):
            _import(db, exports[name])
            _assert_reparse_quiet(db)   # (b) after the import itself
        _ring(dbs, FolderRelay(base / "relay"))
        outcomes.append(_assert_converged(dbs))
        for db in dbs:
            _assert_reparse_quiet(db)   # (b) after the pulls
    assert outcomes[0] == outcomes[1], "the same raw set gives the same days whichever device held which export"


def test_one_device_importing_two_exports_is_order_independent(tmp_path, exports):
    # uds and sleep stay first-wins at the raw level on a local import (a separate backlog item),
    # so the second export carries only the streams whose raw set is order independent
    connect = exports["Y"] / "DI_CONNECT"
    next((connect / "DI-Connect-Aggregator").glob("UDSFile_*")).unlink()
    next((connect / "DI-Connect-Wellness").glob("*sleepData.json")).unlink()
    one, two = tmp_path / "xy.db", tmp_path / "yx.db"
    _import(one, exports["X"]), _import(one, exports["Y"])
    _import(two, exports["Y"]), _import(two, exports["X"])
    _assert_converged([one, two])
    _assert_reparse_quiet(one), _assert_reparse_quiet(two)


# ---------------------------------------------------------------- c: equal-instant ties
def test_equal_instant_ties_pick_the_same_record_on_both_devices(tmp_path):
    x = _wellness_export(tmp_path / "x", [_fitness("2025-07-02", "00:05:00", 33.5)], [_bio("2025-07-02", 72500.0, 1)])
    y = _wellness_export(tmp_path / "y", [_fitness("2025-07-02", "12:00:00", 33.0)], [_bio("2025-07-02", 72000.0, 2)])
    for order, (first, second) in enumerate(((x, y), (y, x))):
        base = tmp_path / f"o{order}"
        base.mkdir()
        a, b = base / "a.db", base / "b.db"
        _import(a, first), _import(b, second)
        relay = FolderRelay(base / "relay")
        _push(a, relay), _push(b, relay), _pull(a, relay), _pull(b, relay)
        rows = _assert_converged([a, b])
        assert {row[2] for row in rows} == {"fitness_age", "weight_kg"}
        _assert_reparse_quiet(a), _assert_reparse_quiet(b)


# ---------------------------------------------------------------- d: relay supersession restores the runner-up
def _hash(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("challenger_wins", [True, False])
def test_supersession_restores_the_runner_up_the_same_on_both_devices(tmp_path, challenger_wins):
    early = _load(_day(3), 0, 180)
    contested = _load(_day(3), 2 * 3600_000, 190)
    challenger = dict(contested)   # same key, different bytes, and it no longer states the acute load
    challenger.pop("dailyTrainingLoadAcute")
    challenger["dailyTrainingLoadChronic"] = 150
    for device_id in range(1000, 2000):
        challenger["deviceId"] = device_id
        if (_hash(challenger) > _hash(contested)) == challenger_wins:
            break
    x = _metrics_export(tmp_path / "x", [early])
    y = _metrics_export(tmp_path / "y", [early, contested])
    z = _metrics_export(tmp_path / "z", [early, challenger])
    dbs = [tmp_path / f"{letter}.db" for letter in "abc"]
    for db, source in zip(dbs, (x, y, z)):
        _import(db, source)
    _ring(dbs, FolderRelay(tmp_path / "relay"))
    rows = _assert_converged(dbs)
    acute = {row[5] for row in rows if row[2] == "training_load_acute"}
    chronic = {row[5] for row in rows if row[2] == "training_load_chronic"}
    if challenger_wins:
        assert acute == {180.0} and chronic == {150.0}, "the runner-up's acute load is back once the winner lacks it"
    else:
        assert acute == {190.0}
    for db in dbs:
        _assert_reparse_quiet(db)


# ---------------------------------------------------------------- e: a null instant beside a stamped record
def test_a_record_with_a_null_instant_wins_the_same_on_both_devices(tmp_path):
    undated = _load(_day(3), 0, 170)
    undated["timestamp"] = None
    x = _metrics_export(tmp_path / "x", [undated])
    y = _metrics_export(tmp_path / "y", [_load(_day(3), 2 * 3600_000, 190)])
    for order, (first, second) in enumerate(((x, y), (y, x))):
        base = tmp_path / f"o{order}"
        base.mkdir()
        a, b = base / "a.db", base / "b.db"
        _import(a, first), _import(b, second)
        relay = FolderRelay(base / "relay")
        _push(a, relay), _push(b, relay), _pull(a, relay), _pull(b, relay)
        rows = _assert_converged([a, b])
        assert len({row[5] for row in rows if row[2] == "training_load_acute"}) == 1
        _assert_reparse_quiet(a), _assert_reparse_quiet(b)


# ---------------------------------------------------------------- f, g: readiness windows
def _split_readiness_export(root: pathlib.Path) -> pathlib.Path:
    """One day whose readiness is spread over two window files: the morning reset in one, a later update in the other."""
    morning = _readiness("2025-07-06", "2025-07-06T04:00:00.0", "AFTER_WAKEUP_RESET", 65)
    update = _readiness("2025-07-06", "2025-07-06T16:00:00.0", "UPDATE_REALTIME_VARIABLES", 33)
    return _readiness_export(root, [[morning], [update]])


def _readiness_rows(db_path: pathlib.Path) -> set[tuple]:
    return {row for row in _daily(db_path) if row[2].startswith("training_readiness") or row[2].startswith("readiness_")
            or row[2] in ("hrv_weekly_average", "recovery_time")}


def test_an_importer_and_a_fresh_puller_agree_on_split_readiness_windows(tmp_path):
    desktop, phone = tmp_path / "desktop.db", tmp_path / "phone.db"
    relay = FolderRelay(tmp_path / "relay")
    _import(desktop, _split_readiness_export(tmp_path / "export"))
    _push(desktop, relay)
    _pull(phone, relay)
    on_desktop, on_phone = _readiness_rows(desktop), _readiness_rows(phone)
    assert on_desktop and on_desktop == on_phone
    score = {row[5] for row in on_desktop if row[2] == "training_readiness"}
    assert score == {65.0}, "the morning reset is the day's readiness, not the later update"
    _assert_converged([desktop, phone])
    _assert_reparse_quiet(desktop), _assert_reparse_quiet(phone)


def test_a_pull_that_carries_readiness_only_equals_a_reparse(tmp_path):
    feeder, store, whole = tmp_path / "feeder.db", tmp_path / "store.db", tmp_path / "whole.db"
    relay = FolderRelay(tmp_path / "relay")
    readiness = _split_readiness_export(tmp_path / "readiness_export")
    full = _export(tmp_path / "full_export", "Y")
    _import(feeder, readiness)
    _import(store, full)
    _push(feeder, relay)
    _pull(store, relay)   # the bundle holds readiness records and nothing else
    _import(whole, full), _import(whole, readiness)
    _assert_converged([store, whole])
    _assert_reparse_quiet(store), _assert_reparse_quiet(whole)


# ---------------------------------------------------------------- the content order itself
def test_raw_record_order_does_not_depend_on_insertion_order(tmp_path):
    def sequence(label: str, order: tuple[int, int]) -> list[tuple]:
        db = tmp_path / f"{label}.db"
        # same weigh-in instant (equal start_utc), different version -> different key and bytes
        weigh_ins = [_bio("2025-07-02", 72500.0, 1), _bio("2025-07-02", 72000.0, 2)]
        for index in order:
            _import(db, _wellness_export(tmp_path / f"{label}_{index}", [], [weigh_ins[index]]))
        with storage.open_for_write(db, "test") as conn:
            ids = sources._raw_ids_by_stream(conn, None)
            return [conn.execute("SELECT stream, source_key, payload_hash FROM raw_records WHERE id=?", (raw_id,)).fetchone()
                    for raw_id, _stream in ids]

    forward, backward = sequence("forward", (0, 1)), sequence("backward", (1, 0))
    assert forward == backward and len(forward) == 2
    assert [row[2] for row in forward] == sorted(row[2] for row in forward), "equal start_utc falls to the payload hash"


# ---------------------------------------------------------------- what the re-derive must leave alone
def test_rederive_keeps_fit_derived_rows_and_the_rows_of_a_record_that_no_longer_decodes(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    db = tmp_path / "a.db"
    _import(db, root)
    with storage.open_for_write(db, "test") as conn:
        local_before = conn.execute("SELECT count(*) FROM daily_metrics WHERE source_scope='local'").fetchone()[0]
        samples_before = conn.execute("SELECT count(*) FROM metric_samples").fetchone()[0]
        broken_id, = conn.execute("SELECT id FROM raw_records WHERE stream='json:uds' ORDER BY id").fetchone()
        rows_of_broken = conn.execute("SELECT count(*) FROM daily_metrics WHERE raw_record_id=?", (broken_id,)).fetchone()[0]
        conn.execute("UPDATE raw_records SET payload=? WHERE id=?", (b"not zlib", broken_id))
        writer = Writer(conn, ClockOffsets.load(conn), "test")
        writer.begin_run()
        sources.rederive_json(conn, writer, ["json:uds", "json:sleep", "json:readiness", "fit:monitoring_b"])
        assert writer.stats.files_failed == 0 and writer.stats.files_imported == 0, "no stats of its own"
        assert conn.execute("SELECT count(*) FROM daily_metrics WHERE source_scope='local'").fetchone()[0] == local_before > 0
        assert conn.execute("SELECT count(*) FROM metric_samples").fetchone()[0] == samples_before
        assert conn.execute("SELECT count(*) FROM daily_metrics WHERE raw_record_id=?", (broken_id,)).fetchone()[0] == rows_of_broken > 0


# ---------------------------------------------------------------- an interrupted import, retried
def _interrupted_export(root: pathlib.Path) -> pathlib.Path:
    """Split readiness plus two out-of-order loads of one day: needs a re-derive to settle."""
    export = _split_readiness_export(root)
    _aggregator, _wellness, metrics = _sections(export)
    (metrics / "MetricsAcuteTrainingLoad_20250701_20250706_1.json").write_text(
        json.dumps([_load(_day(4), 5 * 3600_000, 175), _load(_day(4), 0, 190)]))
    return export


def test_a_kill_in_the_middle_of_an_export_import_is_healed_by_the_retry(tmp_path, monkeypatch):
    export = _interrupted_export(tmp_path / "export")
    killed, clean = tmp_path / "killed.db", tmp_path / "clean.db"
    real, calls = connect_export._import_entry, []

    def kill_on_third_file(entry, stream, writer):
        calls.append(stream)
        if len(calls) == 3:
            raise KeyboardInterrupt("simulated kill between files")
        return real(entry, stream, writer)

    monkeypatch.setattr(connect_export, "_import_entry", kill_on_third_file)
    with pytest.raises(KeyboardInterrupt):
        _import(killed, export)
    monkeypatch.setattr(connect_export, "_import_entry", real)
    _import(killed, export)    # the retry: the finished streams are all duplicates
    _import(clean, export)
    _assert_converged([clean, killed])
    _assert_reparse_quiet(killed)


def test_a_kill_inside_the_import_rederive_is_healed_by_the_retry(tmp_path, monkeypatch):
    export = _interrupted_export(tmp_path / "export")
    killed, clean = tmp_path / "killed.db", tmp_path / "clean.db"
    real = sources.rederive_json

    def kill(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated kill inside the re-derive")

    monkeypatch.setattr(sources, "rederive_json", kill)
    with pytest.raises(KeyboardInterrupt):
        _import(killed, export)
    monkeypatch.setattr(sources, "rederive_json", real)
    _import(killed, export)    # every record is a duplicate now
    _import(clean, export)
    _assert_converged([clean, killed])
    _assert_reparse_quiet(killed)


# ---------------------------------------------------------------- a known class, pinned
def test_a_damaged_local_record_is_repaired_from_the_relays_copy_before_the_pull_decides(tmp_path):
    """Was the pinned known class "relay repairs a damaged local copy" (BACKLOG, 10b review); DONE 2026-10-05.

    A raw record whose stored bytes no longer inflate to their hash is kept (decode-before-delete), a peer
    never pushes a pulled record back, and ``reparse_all`` cannot heal it from the damaged bytes alone. The
    pull now verifies every record the relay carried and refetches a damaged one from the bundle named in
    ``relay_seen`` -- before the conflict rule runs, so the key is decided on intact bytes on both devices.
    """
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    relay = FolderRelay(tmp_path / "relay")
    late, early = _load(_day(4), 5 * 3600_000, 175), _load(_day(4), 0, 190)
    _import(a, _metrics_export(tmp_path / "x", [late]))
    _push(a, relay), _pull(b, relay)
    with storage.open_for_write(a, "test") as conn:
        conn.execute("UPDATE raw_records SET payload=? WHERE stream='json:training_load'", (b"not zlib",))
    _import(b, _metrics_export(tmp_path / "y", [early]))
    _push(b, relay)
    repaired = _pull(a, relay)
    assert repaired.records_repaired == 1 and repaired.records_new == 1, "repaired first, then the peer's record applied"
    _push(a, relay), _pull(b, relay)
    assert _daily(a) == _daily(b), "both devices hold the later observation"
    with storage.open_read_only(a) as conn:
        assert conn.execute("SELECT count(*) FROM raw_records WHERE payload=?", (b"not zlib",)).fetchone()[0] == 0
    _reparse(a)
    assert _daily(a) == _daily(b)
