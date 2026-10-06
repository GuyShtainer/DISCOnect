"""The serve sidecar: protocol behaviour in-process, process-level guarantees in a real child.

In-process tests drive ``serve.serve_lines`` through a rig that feeds request lines and captures
the protocol stream. The process-level tests (EOF exit, fd isolation, never-printed, ignored env
passphrase) run the real module in a child, with an in-memory keyring installed by a bootstrap
script because the child cannot see the test process's keyring.
"""

import base64
import json
import os
import pathlib
import socket
import subprocess
import sys
import textwrap
import threading

import pytest

from disconect import cli, contract, identity, serve, storage
from disconect.ingest import sources
from disconect.storage import keys
from test_import import _build_export
from test_privacy import FORBIDDEN_KEYS, FORBIDDEN_TEXT, SERIAL, _seed

PASS = "a perfectly fine passphrase"
WRONG = "not the passphrase at all!"
STATUSES = {"present", "failed", "source_empty", "not_covered"}
#: The committed live-link store (two overlapping session files on 2025-06-15, one crossing midnight on 06-20/21).
LIVE_STORE = pathlib.Path(__file__).parent / "fixtures" / "serve" / "synthetic-live.hbdb"


class Rig:
    """Feeds request lines to one serve session and returns what it wrote to the protocol stream."""

    def __init__(self, db_path):
        import io
        self.out = io.StringIO()
        self.session = serve.Session(db_path, serve.Channel(self.out))
        self.next_id = 0
        self.lines: list[dict] = []

    def feed(self, lines) -> list[dict]:
        serve.serve_lines(self.session, lines)
        written = [json.loads(line) for line in self.out.getvalue().splitlines()]
        self.out.seek(0)
        self.out.truncate()
        self.lines.extend(written)
        return written

    def send(self, method, **params) -> dict:
        """One request; the response (events are kept in ``self.lines`` only)."""
        self.next_id += 1
        written = self.feed([json.dumps({"id": self.next_id, "method": method, "params": params})])
        return next(line for line in written if line.get("id") == self.next_id)

    def result(self, method, **params):
        response = self.send(method, **params)
        assert "error" not in response, response
        return response["result"]

    def error_code(self, method, **params) -> str:
        response = self.send(method, **params)
        assert "result" not in response, response
        return response["error"]["code"]


def _encrypt(db_path, monkeypatch, production_kdf=False):
    """Convert a seeded store to an encrypted one, leaving this process locked like a fresh start.

    A child process enforces the production KDF floor on the key file it reads, so tests that
    spawn one pass ``production_kdf=True``.
    """
    if production_kdf:
        keys.set_kdf_params(None)
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    assert cli.main(["--db", str(db_path), "key", "init"]) == 0
    monkeypatch.delenv(keys.PASSPHRASE_ENV, raising=False)
    storage._unlocked.clear()
    keys.forget_session()


@pytest.fixture
def plain(db_path):
    _seed(db_path)
    return Rig(db_path)


@pytest.fixture
def encrypted(db_path, monkeypatch, capsys):
    _seed(db_path)
    _encrypt(db_path, monkeypatch)
    capsys.readouterr()
    return Rig(db_path)


# ---- the protocol envelope ----

def test_app_info_names_the_product_and_carries_the_notice(plain, db_path):
    info = plain.result("app.info")
    assert info["product"] == identity.PRODUCT and info["notice"] == identity.NOTICE
    assert info["encrypted"] is False and info["db"] == str(db_path)
    assert info["contract"] == int(contract.CONTRACT_VERSION) and isinstance(info["schema"], int)


def test_malformed_and_unknown_requests_get_error_lines(plain):
    written = plain.feed(["not json", "[1]", json.dumps({"id": True, "method": "app.info"}),
                          json.dumps({"id": 7, "method": 5}), json.dumps({"id": 8, "method": "no.such"}),
                          "", json.dumps({"id": "x", "method": "app.info", "params": []})])
    codes = [(line["id"], line["error"]["code"]) for line in written]
    assert codes == [(None, "bad_params"), (None, "bad_params"), (None, "bad_params"), (7, "bad_params"),
                     (8, "unknown_method"), ("x", "bad_params")]


def test_string_ids_round_trip_and_blank_lines_are_skipped(plain):
    written = plain.feed(["", json.dumps({"id": "abc", "method": "app.info"})])
    assert len(written) == 1 and written[0]["id"] == "abc" and "result" in written[0]


def test_unexpected_failures_report_only_their_type(plain, monkeypatch):
    def boom(session, call):
        raise RuntimeError("secret detail /Users/someone/x")
    monkeypatch.setitem(serve.METHODS, "test.boom", boom)
    error = plain.send("test.boom")["error"]
    assert error == {"code": "internal", "message": "unexpected RuntimeError"}


def test_error_messages_are_redacted(plain):
    response = plain.send("import.run", path="/Users/someone/me@example.com/missing")
    assert response["error"]["code"] == "not_found"
    message = response["error"]["message"]
    assert "/Users/" not in message and "@example.com" not in message and "{path}" in message


def test_bad_params(plain):
    assert plain.error_code("data.metric", metric="sleep_score") == "bad_params"          # scope missing
    assert plain.error_code("data.metric", metric="nope", scope="device") == "bad_params"
    assert plain.error_code("data.metric", metric="sleep_score", scope="moon") == "bad_params"
    assert plain.error_code("data.metric", metric="sleep_score", scope="device", days="7") == "bad_params"
    assert plain.error_code("data.metric", metric="sleep_score", scope="device", last_day="2025-13-40") == "bad_params"
    assert plain.error_code("data.health", window_days=True) == "bad_params"
    assert plain.error_code("import.run", path="x", transport="carrier pigeon") == "bad_params"
    assert plain.error_code("key.cache") == "bad_params"


def test_missing_store_is_not_found(db_path):
    rig = Rig(db_path)
    assert rig.error_code("data.health") == "not_found"
    assert rig.result("key.status")["encrypted"] is False


# ---- locked / unlock / keychain ----

def test_locked_until_unlocked_then_data(encrypted):
    status = encrypted.result("key.status")
    assert status["key_file"] and status["encrypted"] and status["unlocked"] is False and status["keychain"] == "absent"
    assert status["kdf"]["p"] == 1
    for method, params in [("data.health", {}), ("data.metric", {"metric": "sleep_score", "scope": "device"}),
                           ("data.today", {}), ("data.facts", {}), ("import.run", {"path": "x"}),
                           ("import.last", {}), ("key.cache", {"enable": True})]:
        assert encrypted.error_code(method, **params) == "locked", method
    assert encrypted.error_code("key.unlock", passphrase=WRONG) == "wrong_passphrase"
    assert encrypted.error_code("data.health") == "locked"
    assert encrypted.result("key.unlock", passphrase=PASS) == {"unlocked": True}
    assert encrypted.result("key.status")["unlocked"] is True
    assert encrypted.result("data.health")["coverage"]["window"]["days"] == 90
    assert keys._session_passphrase is None, "serve must not cache the passphrase"


def test_unlock_rejects_a_non_string_passphrase(encrypted):
    assert encrypted.error_code("key.unlock", passphrase=12345) == "bad_params"


def test_plaintext_store_has_nothing_to_unlock_and_is_simply_open(plain):
    assert plain.error_code("key.unlock", passphrase=PASS) == "not_encrypted"
    assert plain.result("data.today")["metrics"], "a plaintext store with no key file is simply open"


def test_serve_never_primes_from_keychain_or_env(encrypted, db_path, monkeypatch):
    """A cached keychain item and an env passphrase exist; nothing unlocks until key.unlock asks."""
    master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)
    keys.keychain_set(keys.key_id_for(master), master)
    monkeypatch.setenv(keys.PASSPHRASE_ENV, PASS)
    assert encrypted.result("key.status") == {**encrypted.result("key.status"), "unlocked": False, "keychain": "cached"}
    assert encrypted.error_code("data.health") == "locked"
    assert encrypted.result("key.unlock") == {"unlocked": True}, "no passphrase param: the keychain path"
    assert encrypted.result("data.health")


def test_unlock_without_passphrase_and_empty_keychain_is_locked(encrypted):
    assert encrypted.error_code("key.unlock") == "locked"


def test_key_cache_round_trip(encrypted, db_path):
    encrypted.result("key.unlock", passphrase=PASS)
    assert encrypted.result("key.cache", enable=True) == {"keychain": "cached"}
    assert encrypted.result("key.status")["keychain"] == "cached"
    assert encrypted.result("key.cache", enable=False) == {"keychain": "absent"}
    assert encrypted.result("key.status")["keychain"] == "absent"


def test_key_cache_on_a_plaintext_store_has_nothing_to_cache(plain):
    assert plain.error_code("key.cache", enable=True) == "not_encrypted"


# ---- data ----

def test_metric_is_calendar_filled_with_statuses(plain, db_path):
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("DELETE FROM daily_metrics WHERE date='2025-06-21'")      # inside a recorded failure span
    result = plain.result("data.metric", metric="sleep_score", scope="device", days=14, last_day="2025-07-03")
    days = result["days"]
    assert [d["day"] for d in days][0] == "2025-06-20" and days[-1]["day"] == "2025-07-03" and len(days) == 14
    assert result["unit"] == contract.unit_for("sleep_score") and result["scope"] == "device"
    by_day = {d["day"]: d for d in days}
    assert by_day["2025-06-30"] == {"day": "2025-06-30", "value": 100, "status": "present", "completeness": None}
    assert by_day["2025-06-21"]["value"] is None and by_day["2025-06-21"]["status"] == "failed"
    for late in ("2025-07-01", "2025-07-02", "2025-07-03"):
        assert by_day[late]["value"] is None and by_day[late]["status"] in STATUSES - {"present"}
    assert all((d["value"] is None) == (d["status"] != "present") for d in days)
    assert all(d["value"] != 0 for d in days), "a missing day is never zero"


def test_sample_metric_calendar_carries_the_daily_mean(plain):
    days = plain.result("data.metric", metric="stress", scope="device", days=3, last_day="2025-07-01")["days"]
    assert [d["day"] for d in days] == ["2025-06-29", "2025-06-30", "2025-07-01"]
    assert [d["value"] for d in days] == [30, 30, None]


def test_metric_defaults_end_today_and_span_ninety_days(plain):
    days = plain.result("data.metric", metric="steps", scope="local")["days"]
    assert len(days) == 90 and all(d["value"] is None for d in days)


def test_today_lists_every_contract_pair_with_latest_value(plain):
    today = plain.result("data.today")
    rows = today["metrics"]
    assert len(rows) == len(serve._contract_pairs()) and today["day"]
    assert {(r["metric"], r["scope"]) for r in rows} <= set(contract.STREAMS_FOR)
    by_pair = {(r["metric"], r["scope"]): r for r in rows}
    assert by_pair[("sleep_score", "device")] == {"metric": "sleep_score", "scope": "device", "value": 100,
                                                  "unit": contract.unit_for("sleep_score"), "day": "2025-06-30",
                                                  "status": "present"}
    assert by_pair[("stress", "device")]["value"] == 30 and by_pair[("stress", "device")]["day"] == "2025-06-30"
    empty = by_pair[("steps", "local")]
    assert empty["value"] is None and empty["status"] in STATUSES - {"present"}
    assert all(r["status"] in STATUSES for r in rows)


def test_live_day_lists_sessions_and_folded_minutes(plain):
    # a day without a session: every folded metric listed, nothing in it
    empty = plain.result("data.live", day="2025-06-30")
    assert empty["day"] == "2025-06-30" and empty["sessions"] == []
    assert [m["metric"] for m in empty["metrics"]] == [m for (m, s) in contract.SESSION_STREAMS_FOR if s == "live"]
    assert all(m["minutes"] == 0 and m["median"] is None for m in empty["metrics"])
    assert empty["metrics"][0]["unit"] == contract.unit_for(empty["metrics"][0]["metric"])
    # the default day is local today; a malformed day is bad_params
    assert plain.result("data.live")["day"] == plain.result("data.today")["day"]
    assert plain.error_code("data.live", day="20250630") == "bad_params"
    assert plain.error_code("data.live", day=5) == "bad_params"


def _store_copy(tmp_path):
    db = tmp_path / "sleep.hbdb"
    db.write_bytes(LIVE_STORE.read_bytes())  # a copy: the committed store is never opened for writing
    return db


def test_sleep_night_latest_named_and_in_the_watchs_own_clock(tmp_path):
    rig = Rig(_store_copy(tmp_path))
    latest = rig.result("data.sleep")
    assert latest["date"] == "2025-06-30" and latest["time"] and latest["missing_values"]
    # the offset nearest the session's end (04:00Z on 06-30) is the +03:30 one stored on 06-20
    (device,) = latest["sessions"]
    assert device["utc_offset_s"] == 12600
    assert [(g["start_local"], g["end_local"]) for g in device["stages"]] == [("00:30", "01:30")]
    named = rig.result("data.sleep", date="2025-06-15")
    assert [s["source_scope"] for s in named["sessions"]] == ["device", "vendor_cloud"]
    assert named["sessions"][0]["utc_offset_s"] == 10800
    assert [(g["start_local"], g["end_local"]) for g in named["sessions"][0]["stages"]] == [
        ("23:00", "00:00"), ("00:00", "02:00"), ("02:00", "06:00")]
    # a session with scores only (no stages) still carries its offset, and no stage keys
    assert "stages" not in named["sessions"][1] and named["sessions"][1]["utc_offset_s"] == 10800
    # the same night through the MCP read keeps UTC and no local keys
    assert "utc_offset_s" not in json.dumps(rig.result("tools.call", name="get_sleep_detail", arguments={"date": "2025-06-15"}))


def test_sleep_night_with_no_record_or_no_sleep_says_why(tmp_path):
    rig = Rig(_store_copy(tmp_path))
    none = rig.result("data.sleep", date="2025-06-20")
    assert none["sessions"] == [] and none["reason"] == "no record of that night from any source"
    assert none["date"] == "2025-06-20" and "units" in none
    db = tmp_path / "nosleep.hbdb"
    db.write_bytes(LIVE_STORE.read_bytes())
    with storage.open_for_write(db, purpose="test") as conn:
        conn.execute("DELETE FROM sleep_sessions")
    empty = Rig(db).result("data.sleep")
    assert empty["date"] is None and empty["sessions"] == [] and empty["reason"] == "no sleep stored yet"


def test_sleep_night_bad_date_and_params(tmp_path):
    rig = Rig(_store_copy(tmp_path))
    for bad in ("20250615", "2025-02-30", "", 5):
        assert rig.error_code("data.sleep", date=bad) == "bad_params", bad


def test_sleep_night_without_a_stored_offset_has_no_local_keys_and_a_stage_can_cross_midnight(tmp_path):
    db = _store_copy(tmp_path)
    with storage.open_for_write(db, purpose="test") as conn:
        conn.execute("UPDATE sleep_stages SET start_utc='2025-06-14T20:30:00Z', end_utc='2025-06-14T21:30:00Z' "
                     "WHERE stage='light' AND sleep_id LIKE '2025-06-15|device%'")
    rig = Rig(db)
    crossing = rig.result("data.sleep", date="2025-06-15")["sessions"][0]["stages"][0]
    assert (crossing["start_local"], crossing["end_local"]) == ("23:30", "00:30")  # +03:00: over local midnight
    with storage.open_for_write(db, purpose="test") as conn:
        conn.execute("DELETE FROM clock_offsets")
    bare = rig.result("data.sleep", date="2025-06-15")
    for entry in bare["sessions"]:
        assert "utc_offset_s" not in entry
        assert all("start_local" not in g and "end_local" not in g for g in entry.get("stages", []))
    assert bare["sessions"][0]["stages"][0]["start_utc"] == "2025-06-14T20:30:00Z"


def _only_offsets(db, *pairs):
    with storage.open_for_write(db, purpose="test") as conn:
        conn.execute("DELETE FROM clock_offsets")
        conn.executemany("INSERT INTO clock_offsets(ts_utc, offset_s) VALUES(?, ?)", pairs)


def test_sleep_night_without_an_end_takes_the_offset_nearest_its_start(tmp_path):
    db = _store_copy(tmp_path)
    # the 06-30 device session runs 21:00Z..04:00Z; the 05:00Z offset is nearest its end, the 21:00Z one its start
    _only_offsets(db, ("2025-06-29T21:00:00Z", 3600), ("2025-06-30T05:00:00Z", 7200))
    rig = Rig(db)
    assert rig.result("data.sleep")["sessions"][0]["utc_offset_s"] == 7200
    with storage.open_for_write(db, purpose="test") as conn:
        conn.execute("UPDATE sleep_sessions SET end_utc=NULL WHERE date='2025-06-30'")
    (device,) = rig.result("data.sleep")["sessions"]
    assert device["utc_offset_s"] == 3600
    assert [(g["start_local"], g["end_local"]) for g in device["stages"]] == [("22:00", "23:00")]


def test_sleep_night_with_two_equally_near_offsets_takes_the_earlier(tmp_path):
    db = _store_copy(tmp_path)
    _only_offsets(db, ("2025-06-30T03:00:00Z", 3600), ("2025-06-30T05:00:00Z", 7200))  # the end, 04:00Z, is between
    (device,) = Rig(db).result("data.sleep")["sessions"]
    assert device["utc_offset_s"] == 3600


def test_sleep_needs_the_store_unlocked(encrypted):
    assert encrypted.error_code("data.sleep") == "locked"


def test_contract_lists_the_numeric_metrics_with_their_declared_scopes_and_answers_locked(encrypted, tmp_path):
    for rig in (encrypted, Rig(_store_copy(tmp_path))):   # reads nothing from the store: a locked one answers the same
        reply = rig.result("data.contract")
        assert reply["scopes"] == list(contract.SOURCE_SCOPES)
        rows = reply["metrics"]
        declared = {m for (m, _s) in contract.STREAMS_FOR if contract.cadence_for(m) is not None}
        assert [r["metric"] for r in rows] == [m.metric for m in contract.METRICS if m.metric in declared]
        for row in rows:
            assert set(row) == {"metric", "unit", "cadence", "scopes"}
            assert row["unit"] == contract.unit_for(row["metric"]) and row["cadence"] == contract.cadence_for(row["metric"])
            declared_pairs = set(contract.STREAMS_FOR) | set(contract.SESSION_STREAMS_FOR)
            assert row["scopes"] == [s for s in contract.SOURCE_SCOPES if (row["metric"], s) in declared_pairs]
            assert row["scopes"] and row["cadence"] in ("daily", "sample")
        by = {r["metric"]: r for r in rows}
        assert by["heart_rate"]["cadence"] == "sample" and by["stress"]["cadence"] == "sample"
        assert by["steps"]["cadence"] == "daily"
        assert rows[0]["metric"] == contract.METRICS[0].metric


def test_contract_lists_live_last_for_the_folded_metrics_and_for_no_other(tmp_path):
    rows = Rig(_store_copy(tmp_path)).result("data.contract")["metrics"]
    folded = {m for (m, scope) in contract.SESSION_STREAMS_FOR if scope == "live"}
    assert folded == {"heart_rate", "stress", "respiration_rate", "spo2", "energy_reserve"}
    for row in rows:
        assert ("live" in row["scopes"]) == (row["metric"] in folded)
        if row["metric"] in folded:
            assert row["scopes"][-1] == "live"
    assert {r["metric"] for r in rows} >= folded


def test_live_day_merges_overlapping_records_and_keeps_a_midnight_session_on_both_days(tmp_path):
    db = tmp_path / "live.hbdb"
    db.write_bytes(LIVE_STORE.read_bytes())  # a copy: the committed store is never opened for writing
    rig = Rig(db)
    # 2025-06-15: a partial file (10:00–10:09) stored before the full one (10:00–10:13) is one session
    day = rig.result("data.live", day="2025-06-15")
    # (the store's watch clock runs +3 h, so the local times differ from the UTC ones)
    assert day["sessions"] == [{"start_utc": "2025-06-15T10:00:00Z", "end_utc": "2025-06-15T10:13:00Z",
                                "start_local": "2025-06-15T13:00", "end_local": "2025-06-15T13:13", "minutes": 14}]
    by_metric = {m["metric"]: m for m in day["metrics"]}
    assert by_metric["heart_rate"]["minutes"] == 14 and by_metric["heart_rate"]["median"] == 96
    assert by_metric["spo2"] == {"metric": "spo2", "unit": "%", "minutes": 1, "median": 97}
    # 2025-06-20 23:55Z → 06-21 00:05Z crosses UTC midnight but not the watch's (+3:30 there): one local day
    assert rig.result("data.live", day="2025-06-20")["sessions"] == []
    after = rig.result("data.live", day="2025-06-21")
    assert after["sessions"] == [{"start_utc": "2025-06-20T23:55:00Z", "end_utc": "2025-06-21T00:05:00Z",
                                  "start_local": "2025-06-21T03:25", "end_local": "2025-06-21T03:35", "minutes": 11}]
    hr = next(m for m in after["metrics"] if m["metric"] == "heart_rate")
    assert hr["minutes"] == 11 and hr["median"] == 65
    assert next(m for m in after["metrics"] if m["metric"] == "spo2")["minutes"] == 0


def test_live_day_of_a_watch_behind_utc_is_the_local_day_not_the_utc_day(tmp_path):
    # `day` is the date on the watch's own clock (the stated offset nearest the session's start/end), never
    # the UTC date. The store's watch ran -05:00 from 1 March to 10 April: a session at 2025-03-06T01:00Z..01:05Z
    # is the evening of 5 March there, so it is reported on the 5th and the UTC day (the 6th) lists nothing.
    db = tmp_path / "live.hbdb"
    db.write_bytes(LIVE_STORE.read_bytes())
    rig = Rig(db)
    assert rig.result("data.live", day="2025-03-06")["sessions"] == []
    evening = rig.result("data.live", day="2025-03-05")
    assert evening["sessions"] == [{"start_utc": "2025-03-06T01:00:00Z", "end_utc": "2025-03-06T01:05:00Z",
                                    "start_local": "2025-03-05T20:00", "end_local": "2025-03-05T20:05", "minutes": 6}]
    hr = next(m for m in evening["metrics"] if m["metric"] == "heart_rate")
    assert hr["minutes"] == 6 and hr["median"] == 72
    assert next(m for m in rig.result("data.live", day="2025-03-06")["metrics"]
                if m["metric"] == "heart_rate")["minutes"] == 0


def test_health_and_facts_leave_out_the_core_convention_texts(plain):
    health = plain.result("data.health", window_days=60)
    assert "conventions" not in health and health["coverage"]["window"]["days"] == 60
    facts = plain.result("data.facts", days=7, baseline_days=14)
    assert "sources" not in facts and facts["window"]["days"] == 7 and facts["baseline"]["days"] == 14
    for payload in (health, facts, plain.result("data.today")):
        assert "garmin" not in json.dumps(payload).lower()


# ---- import ----

def test_import_streams_progress_and_answers_after_completion(plain, tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    response = plain.send("import.run", path=str(root), transport="export")
    result = response["result"]
    assert set(result) == {"run_id", "files", "ok", "partial", "duplicate", "failed"}
    assert result["ok"] > 0 and result["failed"] == 0 and result["partial"] is False
    events = [line for line in plain.lines if line.get("event") == "progress"]
    assert events and all(e["op"] == "import" and isinstance(e["done"], int) for e in events)
    assert any(e["total"] is None for e in events) and any(isinstance(e["total"], int) for e in events)
    assert events[-1]["done"] >= 1 and all("/" not in e["note"] or e["note"].startswith("json:") for e in events)
    last = plain.result("import.last")["runs"][0]
    assert last["id"] == result["run_id"] and last["files_failed"] == 0 and last["failures"] == 0
    again = plain.result("import.run", path=str(root))
    assert again["ok"] == 0 and again["duplicate"] > 0


def test_second_import_while_one_runs_is_busy(plain, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    real = sources.import_path

    def slow(path, conn, transport=None, progress=None):
        started.set()
        assert release.wait(10)
        return real(path, conn, transport, progress)
    monkeypatch.setattr(sources, "import_path", slow)
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    request = lambda i, method, **p: json.dumps({"id": i, "method": method, "params": p})   # noqa: E731

    def lines():
        yield request(1, "import.run", path=str(root))
        assert started.wait(10)
        yield request(2, "import.run", path=str(root))
        yield request(3, "import.last")          # reads keep working while the worker writes
        release.set()
    written = plain.feed(lines())
    by_id = {line["id"]: line for line in written if "id" in line}
    assert by_id[2]["error"]["code"] == "busy"
    assert "result" in by_id[3] and "result" in by_id[1] and by_id[1]["result"]["ok"] > 0
    assert plain.result("import.run", path=str(root))["duplicate"] > 0, "the slot is free again"


def test_eof_with_an_import_in_flight_waits_for_it_and_its_answer_is_the_last_line(plain, tmp_path, monkeypatch):
    """Twin of the Rust 'the held import answers last, at EOF': the loop outlives its input for the worker."""
    started, release, input_ended = threading.Event(), threading.Event(), threading.Event()
    real = sources.import_path

    def held(path, conn, transport=None, progress=None):
        started.set()
        assert release.wait(10)
        return real(path, conn, transport, progress)
    monkeypatch.setattr(sources, "import_path", held)
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    request = lambda i, method, **p: json.dumps({"id": i, "method": method, "params": p})   # noqa: E731

    def lines():
        yield request(1, "import.run", path=str(root))
        assert started.wait(10)
        yield request(2, "app.info")
        input_ended.set()   # the iterator is exhausted next: serve_lines sees EOF with the worker still held

    def releaser():
        input_ended.wait(10)
        release.set()
    thread = threading.Thread(target=releaser, name="releaser")
    thread.start()
    try:
        written = plain.feed(lines())
    finally:
        input_ended.set()   # an early failure never leaves the worker held for the full wait
        release.set()
        thread.join()
    ids = [line["id"] for line in written if "id" in line]
    assert ids == [2, 1], "the read answers first, the import's final answer is the last line"
    assert "id" in written[-1] and written[-1]["id"] == 1 and written[-1]["result"]["ok"] > 0
    new_run = next(run for run in plain.result("import.last")["runs"] if run["id"] == written[-1]["result"]["run_id"])
    assert new_run["finished_at"], "the run row was written and finished before the process exited"


def test_import_is_busy_while_another_process_holds_the_write_lock(plain, db_path, tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.write_lock(db_path, "cli import"):
        assert plain.error_code("import.run", path=str(root)) == "busy"
    assert plain.result("import.run", path=str(root))["ok"] > 0


def test_import_failure_is_an_error_line_not_a_crash(plain, tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk exploded at /Users/someone/x")
    monkeypatch.setattr(sources, "import_path", broken)
    response = plain.send("import.run", path=str(tmp_path))
    assert response["error"]["code"] == "internal" and "/Users/" not in json.dumps(response)
    assert plain.result("app.info"), "the process keeps serving"


# ---- privacy walk: every method, every line ----

def _walk_strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            assert key not in FORBIDDEN_KEYS, f"forbidden key {key!r} at {path}"
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _calls(export_root):
    """One or more requests per method, in an order that exercises locked, unlocked and error paths."""
    unlocked_reads = [("data.health", {"window_days": 3650}),
                      ("data.metric", {"metric": "sleep_score", "scope": "device", "days": 60, "last_day": "2025-06-30"}),
                      ("data.metric", {"metric": "stress", "scope": "device", "days": 30, "last_day": "2025-06-30"}),
                      ("data.today", {}), ("data.live", {"day": "2025-06-30"}), ("data.sleep", {}), ("data.contract", {}),
                      ("data.facts", {"days": 7, "baseline_days": 28}),
                      ("sync.status", {}), ("sync.run", {}),
                      ("relay.addresses", {}), ("relay.serve", {"on": False}),
                      ("pair.offer", {"listen": "192.168.1.20:24816"}), ("pair.confirm", {"digits": "123456"}),
                      ("pair.cancel", {}), ("pair.forget", {})]
    unlocked_reads += [("tools.call", {"name": name, "arguments": arguments}) for name, arguments in
                       (("get_data_health", {}), ("get_metric_series", {"metrics": ["steps", "heart_rate"]}),
                        ("get_sleep_detail", {}), ("list_activities", {"limit": 5}), ("get_period_facts", {}),
                        ("get_contract", {}), ("get_sleep_detail", {"date": "nonsense"}), ("no_such_tool", {}))]
    return ([("app.info", {}), ("key.status", {}), ("bogus.method", {})]
            + unlocked_reads                                    # locked errors
            + [("key.unlock", {"passphrase": WRONG}), ("key.unlock", {"passphrase": PASS}),
               ("key.cache", {"enable": True}), ("key.cache", {"enable": False})]
            + unlocked_reads
            + [("import.run", {"path": str(export_root), "transport": "export"}),
               ("import.run", {"path": "/Users/someone/missing.zip"}), ("import.last", {}), ("key.status", {})])


def test_every_method_passes_the_privacy_walk_with_no_network(encrypted, tmp_path, no_network):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    calls = _calls(root)
    assert {method for method, _ in calls} >= set(serve.METHODS), "add a privacy call for every new method"
    for method, params in calls:
        encrypted.send(method, **params)
    assert len(encrypted.lines) >= len(calls)
    for line in encrypted.lines:
        for path, text in _walk_strings({k: v for k, v in line.items()}):
            if path == ".result.db":
                continue                                     # app.info states the path it was started with
            for needle in FORBIDDEN_TEXT:
                assert needle not in text, f"{needle!r} leaked at {path}"
            assert "garmin" not in text.lower(), f"manufacturer name at {path}"
        assert SERIAL not in json.dumps(line) and PASS not in json.dumps(line)


def test_the_no_network_fixture_actually_blocks(no_network):
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(AssertionError):
        socket.getaddrinfo("example.com", 80)


# ---- real child process ----

BOOTSTRAP = textwrap.dedent('''
    import os, sys
    import keyring, keyring.backend

    class Memory(keyring.backend.KeyringBackend):
        priority = 1
        items = {}
        def get_password(self, service, username): return self.items.get((service, username))
        def set_password(self, service, username, password): self.items[(service, username)] = password
        def delete_password(self, service, username): self.items.pop((service, username), None)

    keyring.set_keyring(Memory())
    from disconect import serve
    EXTRA
    sys.exit(serve.main(["--db", os.environ["DISCONECT_DB"]]))
''')


def _child_env(db_path, tmp_path, **extra):
    return {"DISCONECT_DB": str(db_path), "HOME": str(tmp_path), "PATH": os.environ["PATH"], **extra}


def _spawn(db_path, tmp_path, extra_code="", **env):
    script = tmp_path / "bootstrap.py"
    script.write_text(BOOTSTRAP.replace("EXTRA", textwrap.dedent(extra_code)))
    return subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_child_env(db_path, tmp_path, **env), text=True)


def _requests(*calls) -> str:
    return "".join(json.dumps({"id": i, "method": m, "params": p}) + "\n" for i, (m, p) in enumerate(calls, 1))


def test_eof_ends_the_process_with_exit_zero_within_two_seconds(db_path, tmp_path):
    _seed(db_path)
    proc = _spawn(db_path, tmp_path)
    proc.stdin.write(_requests(("app.info", {})))
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["result"]["product"] == identity.PRODUCT
    proc.stdin.close()
    assert proc.wait(timeout=2) == 0
    proc.stdout.close()
    proc.stderr.close()


def test_stray_prints_and_raw_fd_writes_never_reach_the_protocol_stream(db_path, tmp_path):
    _seed(db_path)
    proc = _spawn(db_path, tmp_path, '''
        def noisy(session, call):
            print("STRAY PRINT")
            os.write(1, b"STRAY RAW WRITE\\n")
            sys.stdout.write("STRAY STDOUT\\n")
            return {"ok": True}
        serve.METHODS["test.noisy"] = noisy
    ''')
    out, err = proc.communicate(_requests(("test.noisy", {}), ("app.info", {})), timeout=30)
    assert proc.returncode == 0
    lines = [json.loads(line) for line in out.splitlines()]            # every stdout line parses as protocol
    assert [line["id"] for line in lines] == [1, 2] and lines[0]["result"] == {"ok": True}
    assert "STRAY" not in out and all(f"STRAY {kind}" in err for kind in ("PRINT", "RAW WRITE", "STDOUT"))


def test_passphrase_and_master_key_are_never_printed(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    _encrypt(db_path, monkeypatch, production_kdf=True)
    master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)
    proc = _spawn(db_path, tmp_path)
    out, err = proc.communicate(_requests(("key.unlock", {"passphrase": WRONG}), ("key.unlock", {"passphrase": PASS}),
                                          ("key.cache", {"enable": True}), ("data.health", {"window_days": 7}),
                                          ("key.cache", {"enable": False})), timeout=60)
    assert proc.returncode == 0
    replies = [json.loads(line) for line in out.splitlines()]
    assert [("error" in r) for r in replies] == [True, False, False, False, False]
    secrets_ = {"passphrase": PASS, "wrong passphrase": WRONG, "master": master, "db key": keys.db_key(master)}
    for name, secret in secrets_.items():
        raw = secret.encode() if isinstance(secret, str) else secret
        forms = {raw, raw.hex().encode(), base64.b64encode(raw), base64.urlsafe_b64encode(raw)}
        for stream_name, text in (("stdout", out), ("stderr", err)):
            for form in forms:
                assert form.decode("latin-1") not in text, f"{name} leaked on {stream_name}"


def test_env_passphrase_is_ignored_and_reported(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    _encrypt(db_path, monkeypatch, production_kdf=True)
    proc = _spawn(db_path, tmp_path, **{keys.PASSPHRASE_ENV: PASS})
    out, _err = proc.communicate(_requests(("key.status", {}), ("data.health", {})), timeout=60)
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["event"] == "log" and lines[0]["level"] == "warn" and PASS not in lines[0]["message"]
    assert lines[1]["result"]["unlocked"] is False and lines[2]["error"]["code"] == "locked"


# ---- the core helpers serve relies on ----

def test_local_today_is_the_later_of_utc_and_local(db_path):
    import datetime
    from disconect import queries
    from disconect.ingest.clock import ClockOffsets
    from disconect.ingest.model import ClockOffset
    utc = datetime.timezone.utc
    now = datetime.datetime.now(utc)
    with storage.open_for_write(db_path, "test") as conn:
        assert queries.local_today(conn) == now.date().isoformat()
        ClockOffsets.persist(conn, [ClockOffset(now, 14 * 3600)], None, None)
        ahead = queries.local_today(conn)
        assert ahead == (now + datetime.timedelta(hours=14)).date().isoformat()
        conn.execute("DELETE FROM clock_offsets")
        ClockOffsets.persist(conn, [ClockOffset(now, -12 * 3600)], None, None)
        assert queries.local_today(conn) == now.date().isoformat(), "behind UTC never moves today backwards"


def test_day_statuses_agree_with_the_ledger(db_path):
    from collections import Counter
    from disconect import coverage
    _seed(db_path)
    with storage.open_for_write(db_path, "test") as conn:
        conn.execute("DELETE FROM daily_metrics WHERE date='2025-06-21'")
        statuses = coverage.day_statuses(conn, "sleep_score", "device", "2025-06-01", "2025-07-10")
        row = next(r for r in coverage.ledger(conn, "2025-07-10", 40)["ledger"]
                   if (r["metric"], r["source_scope"]) == ("sleep_score", "device"))
        assert list(statuses) == sorted(statuses) and len(statuses) == 40
        assert Counter(statuses.values()) == Counter({s: row[s] for s in STATUSES if row[s]})
        assert coverage.day_statuses(conn, "steps", "moon", "2025-06-01", "2025-06-02") == {
            "2025-06-01": "not_covered", "2025-06-02": "not_covered"}
        with pytest.raises(ValueError):
            coverage.day_statuses(conn, "sleep_score", "device", "2025-06-02", "2025-06-01")


def test_import_path_reports_progress_per_file(db_path, tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    calls = []
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn, progress=lambda done, total, note: calls.append((done, total, note)))
    fits = [c for c in calls if c[1] is not None]
    assert [c[0] for c in fits] == list(range(1, len(fits) + 1)) and fits[-1][1] == len(fits) == 4
    assert all(c[1] is None and c[2].startswith("json:") for c in calls[len(fits):]) and len(calls) > len(fits)


def test_key_status_dict_is_what_the_cli_prints(encrypted, db_path, capsys):
    status = keys.status_for(db_path)
    assert cli.main(["--db", str(db_path), "--json", "key", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == status
    assert status["key_file"] is True and status["database_encrypted"] is True and status["keychain"] is False


def test_cli_serve_subcommand_runs_the_same_main(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(serve, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["--db", str(db_path), "serve"]) == 0
    assert seen == [["--db", str(db_path)]]


def test_a_moved_legacy_folder_is_never_recreated_by_a_later_import(tmp_path):
    """F1b of the 02a review: serve resolves the legacy folder, migrate-home moves it under the idle process,
    the next import.run used to re-create ~/.hearthbeat as a new store (split data). It is refused instead."""
    legacy = tmp_path / ".hearthbeat"
    legacy.mkdir()
    _seed(legacy / "hearthbeat.db")
    source = tmp_path / "export"
    source.mkdir()
    script = tmp_path / "bootstrap_home.py"
    script.write_text(BOOTSTRAP.replace("EXTRA", "").replace('["--db", os.environ["DISCONECT_DB"]]', "[]"))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True)
    try:
        proc.stdin.write(_requests(("app.info", {})))
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["result"]["product"] == identity.PRODUCT
        # what migrate-home does underneath an idle process
        for entry in legacy.iterdir():
            if entry.name.startswith("hearthbeat.db"):
                entry.rename(legacy / ("disconect.db" + entry.name[len("hearthbeat.db"):]))
        legacy.rename(tmp_path / ".disconect")
        proc.stdin.write(json.dumps({"id": 2, "method": "import.run", "params": {"path": str(source)}}) + "\n")
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        assert reply["error"]["code"] == "not_found", reply
        assert reply["error"]["message"] == "data folder ~/.hearthbeat has moved; restart DISCOnect"
        assert not legacy.exists(), "the old folder must not come back"
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
        proc.stdout.close()
        proc.stderr.close()
