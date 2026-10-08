"""Rust core (disconect-core, the Rust twin, developed beside this package; not in this repository) parity with the Python core: shared DDL and a shared encrypted store.

(a) always: the Rust crate's ``sql/v{1,2,3}.sql`` are byte-identical to ``migrations._V1/_V2/_V3``.
(b) when the debug binary exists: Python makes an encrypted store and Rust reads it; Rust ``init``
creates an encrypted store under a Python-written key file and Python reads it. Key files here use the
production Argon2id cost (the simpler choice: the Rust floor stays on, no test hook crosses the process
boundary), so each unlock costs ~0.4 s.
"""

import datetime
import json
import os
import pathlib
import subprocess
import zipfile

import pytest

from disconect import storage
from disconect.ingest import sources
from disconect.storage import keys, migrations
import test_connect_metrics as metrics_tests
import monorepo
from test_import import _build_export, _readiness, _sleep_json, _uds

core_diff = monorepo.harness("core_diff")

UTC = datetime.timezone.utc

CRATE = monorepo.CRATE
BINARY = monorepo.BINARY
PASS = "parity test passphrase 2026"


@pytest.mark.parametrize("version,ddl", migrations.MIGRATIONS)
def test_rust_sql_is_byte_identical_to_python_migrations(version, ddl):
    assert (CRATE / "sql" / f"v{version}.sql").read_bytes() == ddl.encode("utf-8")


def _rust_env():
    env = {k: v for k, v in os.environ.items() if k not in (keys.PASSPHRASE_ENV, keys.KEYS_ENV)}
    env[keys.PASSPHRASE_ENV] = PASS
    return env


def _rust(*argv):
    return subprocess.run([str(BINARY), *argv], env=_rust_env(), capture_output=True, text=True, timeout=120)


def _python_counts(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
    return {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}


needs_binary = monorepo.needs_binary


@needs_binary
def test_rust_reads_a_python_made_encrypted_store(tmp_path, monkeypatch):
    keys.set_kdf_params(None)   # production cost: the Rust side enforces the floor
    db = tmp_path / "py.db"
    master = keys.create(keys.key_path_for(db), PASS)
    storage.remember(db, master)
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.open_for_write(db, "test") as conn:
        sources.import_path(root, conn)
    assert storage.is_encrypted_file(db) is True
    storage.forget(db)
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    py = storage.open_read_only(db, allow_prompt=False)
    expected, version = _python_counts(py), migrations.current_version(py)
    py.close()
    assert expected["daily_metrics"] > 0

    done = _rust("--db", str(db), "status", "--json")
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["schema_version"] == version == 3
    assert report["encrypted"] is True and report["unlocked"] is True
    assert report["tables"] == expected
    assert PASS not in done.stdout + done.stderr


@needs_binary
def test_python_reads_a_rust_made_encrypted_store(tmp_path, monkeypatch):
    keys.set_kdf_params(None)
    db = tmp_path / "rs.db"
    keys.create(keys.key_path_for(db), PASS)
    done = _rust("--db", str(db), "init")
    assert done.returncode == 0, done.stderr
    assert storage.is_encrypted_file(db) is True
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    conn = storage.open_read_only(db, allow_prompt=False)
    assert migrations.current_version(conn) == 3
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3]
    conn.close()
    storage.forget(db)
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    with storage.open_for_write(db, "test") as writer:   # Python migrates a Rust-made store as a no-op
        assert migrations.current_version(writer) == 3


def test_synthetic_fit_fixtures_match_the_oracle(tmp_path):
    """The Rust crate's decoder fixtures are the Python decoder's own answers; regenerate and compare."""
    import gen_core_fixtures
    gen_core_fixtures.write_synthetic(tmp_path)
    committed = gen_core_fixtures.SYNTHETIC_DIR
    fresh = sorted(p.name for p in tmp_path.iterdir())
    assert fresh == sorted(p.name for p in committed.iterdir()), "fixture set changed: rerun gen_core_fixtures.py"
    crate_copy = monorepo.CRATE / "tests" / "fixtures" / "synthetic"
    assert fresh == sorted(p.name for p in crate_copy.iterdir()), "the crate's copy drifted: rerun gen_core_fixtures.py --synthetic-dir"
    crate_copy = monorepo.CRATE / "tests" / "fixtures" / "synthetic"
    assert fresh == sorted(p.name for p in crate_copy.iterdir()), "the crate's copy drifted: rerun gen_core_fixtures.py --synthetic-dir"
    for name in fresh:
        assert (tmp_path / name).read_bytes() == (committed / name).read_bytes(), f"{name} drifted: rerun gen_core_fixtures.py"
        assert (tmp_path / name).read_bytes() == (crate_copy / name).read_bytes(), f"{name} drifted in the crate's copy"
        assert (tmp_path / name).read_bytes() == (crate_copy / name).read_bytes(), f"{name} drifted in the crate's copy"


def test_canon_fixtures_match_the_oracle(tmp_path):
    """The Rust canonical-JSON fixtures are the oracle's own bytes; regenerate and compare."""
    import gen_canon_fixtures
    gen_canon_fixtures.write_canon(tmp_path)
    committed = gen_canon_fixtures.CANON_DIR
    fresh = sorted(p.name for p in tmp_path.iterdir())
    assert fresh == sorted(p.name for p in committed.iterdir()), "fixture set changed: rerun gen_canon_fixtures.py"
    for name in fresh:
        assert (tmp_path / name).read_bytes() == (committed / name).read_bytes(), f"{name} drifted: rerun gen_canon_fixtures.py"


def _build_full_export(root: pathlib.Path) -> None:
    """Every JSON family, a nested FIT zip, and the corners the Rust port must reproduce.

    On top of ``test_import._build_export`` (FIT zip, uds, sleep, readiness, an ignored folder):
    a second uds/sleep/readiness window sharing a boundary day with the first (first key wins), a
    dateless uds record, a float ``deepSleepSeconds`` (half-even), a nap list, two readiness
    contexts on one day, a duplicate readiness timestamp, a day whose chosen readiness record
    differs per file (last write wins), float and null epoch-ms, a non-ASCII label, a window that
    runs into the future (clamped to the written day), a file that is not JSON, ignored files.
    """
    _build_export(root)
    connect = root / "DI_CONNECT"
    agg, wellness, metrics = (connect / "DI-Connect-Aggregator", connect / "DI-Connect-Wellness",
                              connect / "DI-Connect-Metrics")

    def day(number: int) -> datetime.datetime:
        return datetime.datetime(2025, 6, number, tzinfo=UTC)

    (agg / "UDSFile_2025-06-16_2025-06-17.json").write_text(json.dumps([
        _uds("2025-06-16", 4321, 47), _uds("2025-06-18", 7000, 51), {"totalSteps": 1},
        {"calendarDate": "2025-06-17", "includesWellnessData": False}]))
    (agg / "UDSFile_2025-06-20_2999-12-31.json").write_text(json.dumps([_uds("2025-06-20", 100, 60)]))
    (agg / "UDSFile_2025-06-21_2025-06-22.json").write_text("{not json")
    (agg / "UDSFile_readme.txt").write_text("not a family member")
    sleep = _sleep_json("2025-06-17", day(17) + datetime.timedelta(hours=6), 66)
    sleep.update({"deepSleepSeconds": 7200.5, "lightSleepSeconds": 3601.5, "retro": 1, "napList": [{}, {}]})
    (wellness / "2025-06-16_2025-06-17_111_sleepData.json").write_text(json.dumps([
        _sleep_json("2025-06-16", day(16) + datetime.timedelta(hours=6), 12), sleep]))
    (wellness / "unrelated_data.json").write_text("[]")
    (wellness / "111_fitnessAgeData.json").write_text(json.dumps([
        metrics_tests._fitness_age("2025-06-15", 33.5), {"currentBioAge": 30}]))
    (wellness / "111_userBioMetrics.json").write_text(json.dumps([
        metrics_tests._bio_metrics("2025-06-15", 72500.0, 1), metrics_tests._bio_metrics("2025-06-16", 71.8, 2),
        metrics_tests._bio_metrics("2025-06-17", None, 3)]))
    morning = _readiness("2025-06-17", "2025-06-17T04:00:00.0", "AFTER_WAKEUP_RESET", 70)
    morning.update({"level": "H\u00f6ch", "hrvWeeklyAverage": 0.05})
    (metrics / "TrainingReadinessDTO_20250615_20250617_222.json").write_text(json.dumps([
        _readiness("2025-06-15", "2025-06-15T16:00:00.0", "UPDATE_REALTIME_VARIABLES", 61),
        _readiness("2025-06-17", "2025-06-17T18:00:00.0", "POST_EXERCISE_RESET", 66), morning,
        _readiness("2025-06-17", "2025-06-17T18:00:00.0", "UPDATE_REALTIME_VARIABLES", 1),
        _readiness("2025-06-18", "2025-06-18T20:00:00.0", "UPDATE_REALTIME_VARIABLES", 45),   # no morning: latest wins
        _readiness("2025-06-18", "2025-06-18T10:00:00.0", "POST_EXERCISE_RESET", 40),
        _readiness("2025-06-19", "2025-06-19T05:00:00.0", "AFTER_WAKEUP_RESET", 80),          # two mornings: later wins
        _readiness("2025-06-19", "2025-06-19T03:00:00.0", "AFTER_WAKEUP_RESET", 30),
        {"calendarDate": "2025-06-18", "timestamp": 5}, {"timestamp": "2025-06-18T01:00:00.0"}]))
    (metrics / "MetricsMaxMetData_20250615_20250617_111.json").write_text(json.dumps([
        {"calendarDate": "2025-06-15", "updateTimestamp": "2025-06-15T04:30:00.0", "vo2MaxValue": 52.0,
         "sport": "RUNNING"},
        {"calendarDate": "2025-06-16", "updateTimestamp": "2025-06-16T04:30:00.0", "vo2MaxValue": 0.05,
         "sport": "\u039f\u0394\u039f\u03a3"}]))
    ts = metrics_tests._epoch_ms(day(15))
    float_load = metrics_tests._training_load(day(15), 190, 141, 1.3, "HIGH")
    float_load.update({"calendarDate": float(ts) + 0.5, "timestamp": float(ts) + 0.5})
    string_date = metrics_tests._training_load(day(17), 170, 139, 1.2, "LOW")
    string_date.update({"calendarDate": "2025-06-17T00:00:00.0", "timestamp": None})
    (metrics / "MetricsAcuteTrainingLoad_20250615_20250617_111.json").write_text(json.dumps([
        metrics_tests._training_load(day(15), 180, 140, 1.29, "OPTIMAL"), float_load, string_date,
        {"timestamp": ts}]))
    (metrics / "EnduranceScore_20250615_20250617_111.json").write_text(json.dumps([
        metrics_tests._endurance(day(15), 62), metrics_tests._endurance(day(16), 63)]))
    (metrics / "HillScore_20250615_20250617_111.json").write_text(json.dumps([
        metrics_tests._hill(day(15)), metrics_tests._hill(day(16))]))


def _zip_of(root: pathlib.Path, target: pathlib.Path) -> None:
    """The same tree as one outer zip, members written in reverse order (the importer must sort)."""
    files = sorted((p for p in root.rglob("*") if p.is_file()), reverse=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())


@needs_binary
@pytest.mark.parametrize("form", ["folder", "zip"])
def test_synthetic_export_imports_identically(tmp_path, form):
    """One synthetic export, folder or zip, imported by the Python oracle and by the Rust binary."""
    root = tmp_path / "export"
    root.mkdir()
    _build_full_export(root)
    source = root
    if form == "zip":
        source = tmp_path / "export.zip"
        _zip_of(root, source)
    assert sources.connect_export.looks_like_export(source)

    p_db, r_db = tmp_path / "p.db", tmp_path / "r.db"
    with storage.open_for_write(p_db, "test") as conn:
        stats = sources.import_path(source, conn)
    env = {k: v for k, v in os.environ.items() if k not in (keys.PASSPHRASE_ENV, keys.KEYS_ENV)}
    done = subprocess.run([str(BINARY), "--db", str(r_db), "import", str(source)], env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 1, done.stdout + done.stderr   # partial: the not-JSON file is a failure
    assert done.stdout.splitlines()[0].startswith(f"partial: {stats.files_imported} imported, "), done.stdout

    # the fixture really exercises what it claims to
    assert stats.status() == "partial" and stats.files_failed == 1
    assert stats.failures[0]["kind"] == "bad_json" and stats.transport == "connect_export"
    for reason in ("uds_without_date", "sleep_stub_without_date", "naps_not_imported",
                   "hill_score_fields_ambiguous", "hrv_weekly_average_below_minimum", "vo2max_below_minimum"):
        assert stats.dropped.get(reason, 0) >= 1, reason
    assert stats.ignored >= 4 and stats.files_duplicate >= 3
    with storage.open_read_only(p_db) as conn:
        streams = {r[0] for r in conn.execute("SELECT DISTINCT stream FROM raw_records WHERE stream LIKE 'json:%'")}
        assert len(streams) == 9
        assert conn.execute("SELECT count(*) FROM raw_records WHERE stream LIKE 'fit:%'").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM export_ranges").fetchone()[0] >= 8
        today = datetime.date.today().isoformat()
        assert conn.execute("SELECT count(*) FROM export_ranges WHERE from_day='2025-06-20' AND to_day <= ?",
                            (today,)).fetchone()[0] == 1, "a window into the future is clamped to the written day"
        assert conn.execute("SELECT count(*) FROM import_failures WHERE kind='bad_json' AND start_utc IS NOT NULL"
                            ).fetchone()[0] == 1

    report = core_diff.compare(p_db, r_db)
    detail = {t: v for t, v in report["tables"].items()
              if v["only_in_p"] or v["only_in_r"] or v["differing_rows"]}
    assert report["differing"] == 0, (report["schema"], detail)
    assert report["tables"]["import_runs"]["rows_p"] == report["tables"]["import_runs"]["rows_r"] == 1
    assert report["tables"]["raw_records"]["rows_r"] == report["tables"]["raw_records"]["rows_p"] > 30


def test_core_diff_never_accepts_a_rust_null_as_an_enum_name():
    """Review finding: ``dict.get`` of an unknown pair is None, so P-text vs R-NULL used to
    vanish into the newer-profile allowance; the allowance is for a verified name only."""
    core_diff = monorepo.harness("core_diff")

    assert core_diff.accepted_enum_name("sport", "63", "video_gaming")
    assert not core_diff.accepted_enum_name("sport", "63", None)
    assert not core_diff.accepted_enum_name("start_utc", "2025-06-15T00:00:00Z", None)
    assert not core_diff.accepted_enum_name("sport", "63", "esport")

