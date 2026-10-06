"""Bet 9b slice 2 gate: live-link files import identically on the Python and the Rust core.

``core_diff`` must report 0 between a Python-imported and a Rust-imported scratch store in four orders:
(a) live only, (b) live then FIT, (c) FIT then live, (d) import-last vs pull-last through the folder relay.
Synthetic data only; the FIT side comes from ``fit_builder`` through ``test_import._monitoring_day``.
"""

import datetime
import json
import os
import pathlib
import subprocess
import sys

import pytest

from disconect import storage
from disconect.ingest import sources
from disconect.relay import sync
from disconect.relay.folder import FolderRelay
from disconect.storage import keys
from test_converge_10b_mixed_core import Fleet
from test_core_parity import BINARY, PASS, _rust_env
from test_import import UTC, _monitoring_day

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "tools"))
import core_diff  # noqa: E402  (tools/ is a script directory, not a package)

pytestmark = pytest.mark.skipif(not BINARY.exists(), reason="build projects/disconect-core first (cargo build)")

MIDNIGHT = datetime.datetime(2025, 6, 15, 0, 0, tzinfo=UTC).timestamp()


def _lines(path, lines):
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _live_source(base: pathlib.Path) -> pathlib.Path:
    """A folder of synthetic live files: one spanning midnight (float and int ``t``), one that overlaps a
    monitoring day, a status-only file (skipped, counted) and a frame log (not live, not FIT)."""
    folder = base / "ble"
    folder.mkdir()
    _lines(folder / "live-a.jsonl", [
        {"status": "scanning"},
        {"t": MIDNIGHT - 2.0, "metric": "heart_rate", "value": 66},
        {"t": MIDNIGHT - 1, "metric": "steps", "value": 10},
        {"t": MIDNIGHT + 1.5, "metric": "heart_rate", "value": 67},
        {"t": MIDNIGHT + 1.5, "metric": "heart_rate", "value": 65},
        {"t": MIDNIGHT + 2.25, "metric": "spo2", "value": 97},
        {"status": "stopped", "stop": "LinkClosed"},
    ])
    _lines(folder / "live-b.jsonl", [
        {"t": MIDNIGHT + 43200.0, "metric": "stress", "value": 31},
        {"t": MIDNIGHT + 43260.0, "metric": "respiration_rate", "value": 14},
        {"t": MIDNIGHT + 43320.0, "metric": "energy_reserve", "value": 80},
    ])
    _lines(folder / "live-empty.jsonl", [{"status": "scanning"}, {"stop": "x"}])
    _lines(folder / "frames.jsonl", [{"frame": "0a0b", "dir": "rx"}])
    return folder


def _fit_source(base: pathlib.Path) -> pathlib.Path:
    folder = base / "fit"
    folder.mkdir()
    (folder / "A1.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, tzinfo=UTC), 8000))
    return folder


def _py_import(db, source):
    with storage.open_for_write(db, "test") as conn:
        sources.import_path(source, conn)


def _rs_import(db, source):
    env = {k: v for k, v in os.environ.items() if k not in (keys.PASSPHRASE_ENV, keys.KEYS_ENV)}
    done = subprocess.run([str(BINARY), "--db", str(db), "import", str(source)], env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr   # every order exits 0 (a frame log is skipped, not failed)


def _assert_identical(p_db, r_db):
    report = core_diff.compare(p_db, r_db)
    detail = {t: v for t, v in report["tables"].items() if v["only_in_p"] or v["only_in_r"] or v["differing_rows"]}
    assert report["differing"] == 0, (report["schema"], detail)
    return report


def _live_count(db) -> int:
    with storage.open_read_only(db) as conn:
        return conn.execute("SELECT count(*) FROM raw_records WHERE stream='json:live'").fetchone()[0]


@pytest.mark.parametrize("order", ["live_only", "live_then_fit", "fit_then_live"])
def test_core_diff_is_zero_in_each_import_order(tmp_path, order):
    live, fit = _live_source(tmp_path), _fit_source(tmp_path)
    steps = {"live_only": [live], "live_then_fit": [live, fit], "fit_then_live": [fit, live]}[order]
    p_db, r_db = tmp_path / "p.db", tmp_path / "r.db"
    for source in steps:
        _py_import(p_db, source)
        _rs_import(r_db, source)
    assert _live_count(p_db) == _live_count(r_db) == 2
    report = _assert_identical(p_db, r_db)
    assert report["tables"]["raw_records"]["rows_p"] == report["tables"]["raw_records"]["rows_r"]


def test_core_diff_is_zero_import_last_vs_pull_last_through_the_relay(tmp_path, monkeypatch):
    """One source device holds live + FIT and pushes. Store A imports the live files and pulls the FIT
    record (pull last); store B pulls the live record, then imports the FIT file (import last). The same
    two orders run on the Python core and on the Rust core; the stores must not differ."""
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    fleet = Fleet(tmp_path)
    try:
        relay = FolderRelay(tmp_path / "relay")
        live, fit = _live_source(tmp_path), _fit_source(tmp_path)
        source = fleet.device("source", "py")
        fleet.do_import(source, live)
        fleet.do_import(source, fit)
        fleet.push(source, relay)
        stores = {}
        for core in ("py", "rs"):
            pull_last = fleet.device(f"{core}-pull-last", core)
            fleet.do_import(pull_last, live)
            fleet.pull(pull_last, relay)
            import_last = fleet.device(f"{core}-import-last", core)
            fleet.pull(import_last, relay)
            fleet.do_import(import_last, fit)
            stores[core] = (pull_last, import_last)
        for index in (0, 1):
            _assert_identical(stores["py"][index].db, stores["rs"][index].db)
        assert _live_count(stores["py"][0].db) == _live_count(stores["py"][1].db) == 2
    finally:
        fleet.close()
