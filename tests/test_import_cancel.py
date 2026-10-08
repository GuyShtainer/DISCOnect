"""Review: ``import_path(cancel=)`` per phase — what is read, what is skipped, what is still derived.

Twin of ``disconect-core/tests/import_cancel_test.rs``. Synthetic data only.
"""
import datetime

import pytest

from disconect import storage
from disconect.ingest import sources
from test_import import UTC, _build_export, _monitoring_day
from test_live_import import LIVE_LINES, _write_lines

DERIVED = ("daily_metrics", "metric_samples", "monitoring_intervals", "daily_labels")


def _counts(db_path):
    conn = storage.open_read_only(db_path)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DERIVED}
    out["fit_records"] = conn.execute("SELECT COUNT(*) FROM raw_records WHERE stream LIKE 'fit:%'").fetchone()[0]
    out["json_records"] = conn.execute("SELECT COUNT(*) FROM raw_records WHERE stream LIKE 'json:%'").fetchone()[0]
    out["run_status"] = conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    return out


def _import(path, db_path, cancel=None):
    with storage.open_for_write(db_path, "test") as conn:
        return sources.import_path(path, conn, cancel=cancel)


def _after(n: int):
    """A cancel check that says True from its n-th call on (0 = before anything is written)."""
    calls = [0]

    def check() -> bool:
        calls[0] += 1
        return calls[0] > n
    return check


def test_fit_folder_cancel_after_one_file_derives_that_file_like_a_lone_import(tmp_path):
    day1 = datetime.datetime(2025, 6, 14, 21, tzinfo=UTC)
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "a.fit").write_bytes(_monitoring_day(day1, 8000))
    (folder / "b.fit").write_bytes(_monitoring_day(day1 + datetime.timedelta(days=1), 5000))
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "a.fit").write_bytes(_monitoring_day(day1, 8000))

    stats = _import(folder, tmp_path / "cut.db", cancel=_after(1))
    assert stats.cancelled and stats.status() == "cancelled"
    assert (stats.files_seen, stats.files_imported, stats.files_failed) == (1, 1, 0)
    cut = _counts(tmp_path / "cut.db")
    assert cut["run_status"] == "cancelled" and cut["fit_records"] == 1 and cut["daily_metrics"] > 0
    whole = _import(lone, tmp_path / "lone.db")
    assert whole.status() == "ok"
    assert {k: v for k, v in cut.items() if k != "run_status"} == {k: v for k, v in _counts(tmp_path / "lone.db").items() if k != "run_status"}

    # a cancel before the first write reads nothing: booked cancelled with zero counters
    stats = _import(folder, tmp_path / "zero.db", cancel=_after(0))
    assert stats.cancelled and stats.files_seen == 0 and _counts(tmp_path / "zero.db")["run_status"] == "cancelled"


def test_live_phase_cancel_skips_the_fit_walk_and_still_derives_live_samples(tmp_path, monkeypatch):
    folder = tmp_path / "drop"
    folder.mkdir()
    _write_lines(folder / "live-1.jsonl", LIVE_LINES)
    _write_lines(folder / "live-2.jsonl", LIVE_LINES[:-1] + [{"t": 1750000009.0, "metric": "steps", "value": 41}])
    (folder / "m.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, tzinfo=UTC), 8000))

    def never(_path):
        raise AssertionError("the FIT walk ran after a cancel in the live phase")
    monkeypatch.setattr(sources, "iter_fit_files", never)

    stats = _import(folder, tmp_path / "live.db", cancel=_after(1))
    assert stats.cancelled and stats.files_seen == 1 and stats.files_imported == 1
    counts = _counts(tmp_path / "live.db")
    assert counts["run_status"] == "cancelled" and counts["fit_records"] == 0 and counts["daily_metrics"] == 0
    assert counts["metric_samples"] > 0, "derive_live_samples ran for the one live file written"


def test_export_json_phase_cancel_keeps_the_fit_rows_and_rederives_the_json_read(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    whole = _import(root, tmp_path / "whole.db")
    assert whole.status() == "ok"
    full = _counts(tmp_path / "whole.db")

    # four FIT members (one check before each write), then the JSON files (one check before each): the sixth
    # check lands before the second JSON file
    stats = _import(root, tmp_path / "cut.db", cancel=_after(6))
    assert stats.cancelled and stats.status() == "cancelled" and stats.files_imported >= 4
    cut = _counts(tmp_path / "cut.db")
    assert cut["run_status"] == "cancelled"
    assert cut["fit_records"] == full["fit_records"], "the FIT phase finished before the cancel"
    assert 0 < cut["json_records"] < full["json_records"], "one JSON file read, the rest skipped"
    assert cut["daily_metrics"] > 0, "the JSON re-derivation and the FIT tail derivations ran"


@pytest.mark.parametrize("phase_calls", [1, 3])
def test_a_cancel_after_the_last_check_changes_nothing(tmp_path, phase_calls):
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "a.fit").write_bytes(_monitoring_day(datetime.datetime(2025, 6, 14, 21, tzinfo=UTC), 8000))
    stats = _import(folder, tmp_path / "one.db", cancel=_after(phase_calls))
    assert not stats.cancelled and stats.status() == "ok" and stats.files_imported == 1
