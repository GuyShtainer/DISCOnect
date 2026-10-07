# SPDX-License-Identifier: AGPL-3.0-or-later
"""``sync.status`` and ``sync.run`` over the serve protocol (Bet 12, slice B), on the Python oracle:
the checks in order, the progress events, counts-only results, the slot shared with ``import.run``,
and the refusal of a LAN relay (``unsupported_transport``). The Rust core is compared with these
answers by ``tools/serve_diff.py`` (its sync leg)."""

from __future__ import annotations

import json
import shutil
import threading

import pytest

from disconect import serve, storage
from disconect.ingest import sources
from disconect.relay import config as relay_config
from disconect.relay import sync
from disconect.storage import keys
from test_import import _build_export
from test_privacy import _seed
from test_serve import PASS, Rig, _encrypt

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


EVENTS = [("push", "start"), ("push", "done"), ("pull", "start"), ("pull", "done")]


def _relay_json(db_path, body) -> None:
    (db_path.parent / "relay.json").write_text(json.dumps(body))


def _sync_events(rig: Rig) -> list[dict]:
    return [line for line in rig.lines if line.get("event") == "progress" and line.get("op") == "sync"]


def _second_device(db_path, tmp_path) -> Rig:
    """An empty encrypted store holding the same master as ``db_path``, unlocked in this process."""
    other = tmp_path / "b.db"
    shutil.copy(keys.key_path_for(db_path), keys.key_path_for(other))
    master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(other)), PASS)
    storage.remember(other, master)
    with storage.open_for_write(other, "test"):
        pass
    storage.forget(other)
    rig = Rig(other)
    assert rig.result("key.unlock", passphrase=PASS) == {"unlocked": True}
    return rig


def test_sync_status_names_the_lan_relay_address_and_nothing_else(encrypted, db_path):
    encrypted.result("key.unlock", passphrase=PASS)
    url = lambda: encrypted.result("sync.status")["relay_url"]  # noqa: E731
    assert url() is None  # no relay.json
    _relay_json(db_path, {"folder": "/some/folder"})
    assert url() is None  # a folder relay has no address
    _relay_json(db_path, {"folder": 5})
    assert url() is None
    _relay_json(db_path, {"lan": "http://192.168.1.20:8321"})
    assert url() == "http://192.168.1.20:8321"
    _relay_json(db_path, {"lan": " http://192.168.1.20:8321/ ", "folder": "/f"})
    assert url() == "http://192.168.1.20:8321"  # trimmed, no trailing slash, lan wins
    for bad in ("https://192.168.1.20:8321", "http://user@host:1", "http://host:1/path", "192.168.1.20:8321"):
        _relay_json(db_path, {"lan": bad})
        assert url() is None, bad


def test_a_plaintext_store_reports_empty_counts_and_cannot_run(plain, db_path, tmp_path):
    assert plain.result("sync.status")["bundles"] == {}
    status = plain.result("sync.status")
    assert set(status) == {"bundles", "records_unsent", "records_seen", "conflicts", "superseded", "gaps",
                           "last_pushed_at", "last_pulled_at", "serving", "relay_url", "relay_kind", "relays"}
    assert status["serving"] is None and status["relay_url"] is None and status["relays"] is None
    assert status["records_unsent"] > 0 and status["gaps"] == []
    assert status["last_pushed_at"] is None and status["last_pulled_at"] is None
    response = plain.send("sync.run")
    assert response["error"] == {"code": "not_found", "message": "no relay is configured (relay.json in the data folder)"}
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    response = plain.send("sync.run")
    assert response["error"]["code"] == "not_encrypted"
    assert response["error"]["message"].startswith("the relay needs an encrypted store")


def test_a_locked_store_answers_locked_before_anything_else(encrypted, db_path, tmp_path):
    for method in ("sync.status", "sync.run"):
        assert encrypted.error_code(method) == "locked"
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    assert encrypted.error_code("sync.run") == "locked"


def test_a_store_older_than_the_relay_tables_answers_like_a_fresh_one(db_path):
    from disconect.storage import migrations, sqlite

    version, ddl = migrations.MIGRATIONS[0]
    conn = sqlite.connect(str(db_path))
    conn.executescript(ddl)
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, '2025-07-02T09:30:00Z')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    status = Rig(db_path).result("sync.status")
    assert status == {"bundles": {}, "records_unsent": 0, "records_seen": 0, "conflicts": 0, "superseded": 0, "gaps": [],
                      "last_pushed_at": None, "last_pulled_at": None, "serving": None, "relay_url": None, "relay_kind": None, "relays": None}


def test_a_folder_relay_pushes_then_pulls_with_events_and_counts_only(encrypted, db_path, tmp_path):
    relay = tmp_path / "relay"
    _relay_json(db_path, {"folder": str(relay)})
    assert encrypted.result("key.unlock", passphrase=PASS) == {"unlocked": True}
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    encrypted.result("import.run", path=str(root), transport="export")
    encrypted.lines.clear()
    result = encrypted.result("sync.run")
    events = _sync_events(encrypted)
    assert [(e["phase"], e["state"]) for e in events] == EVENTS
    assert set(result) == {"push", "pull", "sites", "status"}
    assert result["push"]["bundles"] == 1 and result["push"]["records"] > 0
    assert result["pull"] == {"applied": 0, "rejected": 0, "records_new": 0, "records_duplicate": 0,
                              "records_invalid": 0, "conflicts": 0, "ranges_new": 0, "records_repaired": 0, "records_kept": 0, "gaps": 0, "status": "ok"}
    done = {e["phase"]: {k: v for k, v in e.items() if k not in ("event", "op", "phase", "state")}
            for e in events if e["state"] == "done"}
    assert done == {"push": result["push"], "pull": result["pull"]}
    status = encrypted.result("sync.status")
    assert status["bundles"] == {"pushed_applied": 1}
    # the time of the newest applied bundle per direction: pushed now, never pulled
    assert status["last_pushed_at"] >= "2020" and status["last_pushed_at"].endswith("Z") and status["last_pulled_at"] is None

    other = _second_device(db_path, tmp_path)
    pulled = other.result("sync.run")
    assert pulled["push"] == {"bundles": 0, "records": 0, "ranges": 0}
    assert pulled["pull"]["applied"] == 1 and pulled["pull"]["records_new"] > 0
    assert pulled["pull"]["records_new"] + pulled["pull"]["records_invalid"] == result["push"]["records"]
    status = other.result("sync.status")
    assert status["bundles"] == {"pulled_applied": 1}
    assert status["last_pulled_at"] >= "2020" and status["last_pushed_at"] is None
    again = other.result("sync.run")
    assert again["pull"]["applied"] == 0 and again["push"]["bundles"] == 0
    # a run that moved nothing books no bundle, so the times do not advance
    after = other.result("sync.status")
    assert after["last_pulled_at"] == status["last_pulled_at"] and after["last_pushed_at"] == status["last_pushed_at"]
    assert after["bundles"] == status["bundles"]


def test_a_lan_relay_is_refused_with_its_own_code_after_the_other_checks(encrypted, db_path, tmp_path):
    _relay_json(db_path, {"lan": "http://127.0.0.1:9"})
    assert encrypted.error_code("sync.run") == "locked"
    encrypted.result("key.unlock", passphrase=PASS)
    response = encrypted.send("sync.run")
    assert response["error"] == {"code": "unsupported_transport", "message": relay_config.LAN_TEXT}
    # lan wins over folder, as in the Rust core, so the two cores agree on what the file names
    _relay_json(db_path, {"folder": str(tmp_path / "relay"), "lan": "http://127.0.0.1:9"})
    assert encrypted.error_code("sync.run") == "unsupported_transport"
    # an empty lan falls through to the folder; junk names nothing
    _relay_json(db_path, {"folder": str(tmp_path / "relay"), "lan": ""})
    assert encrypted.result("sync.run")["push"]["bundles"] == 1
    for junk in ("not json", "[1]", json.dumps({"folder": 5}), json.dumps({"folder": ""})):
        (db_path.parent / "relay.json").write_text(junk)
        assert encrypted.error_code("sync.run") == "not_found", junk


def test_sync_run_and_import_run_share_one_slot(encrypted, db_path, tmp_path, monkeypatch):
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    encrypted.result("key.unlock", passphrase=PASS)
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    started, release = threading.Event(), threading.Event()
    real = sources.import_path

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(sources, "import_path", slow)
    encrypted.next_id += 1
    first = encrypted.next_id
    serve.handle_line(encrypted.session, json.dumps({"id": first, "method": "import.run", "params": {"path": str(root)}}))
    assert started.wait(5)
    busy = encrypted.send("sync.run")
    assert busy["error"] == {"code": "busy", "message": "an import or a sync is already running"}
    release.set()
    encrypted.session.import_thread.join()
    monkeypatch.undo()
    assert encrypted.result("sync.run")["push"]["bundles"] == 1, "the slot is free again"


def test_import_cancel_during_a_sync_is_not_found(encrypted, db_path, tmp_path, monkeypatch):
    """The slot is shared, the cancel is not: a sync holding it is no import (Python only; the Rust sync has no hold hook)."""
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    encrypted.result("key.unlock", passphrase=PASS)
    started, release = threading.Event(), threading.Event()
    real = sync.push_strict

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(sync, "push_strict", slow)
    encrypted.next_id += 1
    serve.handle_line(encrypted.session, json.dumps({"id": encrypted.next_id, "method": "sync.run", "params": {}}))
    assert started.wait(5)
    assert encrypted.send("import.cancel")["error"] == {"code": "not_found", "message": "no import is running"}
    release.set()
    encrypted.session.import_thread.join()


def test_sync_status_is_read_only_and_ignores_params(plain, db_path):
    before = db_path.read_bytes()
    assert plain.result("sync.status", limit=1, x=[1]) == plain.result("sync.status")
    assert db_path.read_bytes() == before


def test_sync_methods_are_registered():
    assert {"sync.status", "sync.run"} <= set(serve.METHODS)
    assert sync is not None


# ---------------------------------------------------------------- Bet 19d: the relay list
def _sites_ids(result) -> list[tuple]:
    return [(site["id"], site["kind"], site["error"]) for site in result["sites"]]


def test_sync_run_takes_a_relays_param_and_sync_status_relays_follows_the_last_run(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    assert encrypted.result("sync.status")["relays"] is None
    roots = [tmp_path / f"site{i}" for i in (1, 2, 3)]
    for root in roots:
        root.mkdir()
    relays = [{"id": f"{i + 1:08x}", "kind": "folder", "path": str(root), "label": "x"} for i, root in enumerate(roots)]
    # no relay.json at all: the call's list is enough
    result = encrypted.result("sync.run", relays=relays)
    assert set(result) == {"push", "pull", "sites", "status"} and result["status"] == "ok"
    assert result["push"]["bundles"] == 1
    assert result["sites"] == [{"id": f"{i + 1:08x}", "kind": "folder", "label": "x", "pushed": 1, "healed": 0,
                                "behind": 0, "pulled": 0, "rejected": 0, "error": None} for i in range(3)]
    assert encrypted.result("sync.status")["relays"] == result["sites"]
    # a missing and an unavailable site: partial, the others still sync, the words are reasons never paths
    relays[1]["path"] = str(tmp_path / "gone")
    relays[2]["unavailable"] = True
    del relays[2]["path"]
    again = encrypted.result("sync.run", relays=relays)
    assert again["status"] == "partial"
    assert _sites_ids(again) == [("00000001", "folder", None), ("00000002", "folder", "unavailable"),
                                 ("00000003", "folder", "unavailable")]
    assert encrypted.result("sync.status")["relays"] == again["sites"]
    assert str(tmp_path) not in json.dumps(again)


def test_a_relays_param_is_checked_after_the_store_and_before_the_slot(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    good = {"id": "ab", "kind": "folder", "path": str(tmp_path / "r")}
    bad_text = "relays: each entry needs an id, a kind and a path or url"
    for value in ("x", {}, [5], [{"kind": "folder", "path": "/a"}], [{**good, "id": "ABC"}], [{**good, "id": ""}],
                  [{**good, "kind": "ftp"}], [{"id": "ab", "kind": "folder"}], [{"id": "ab", "kind": "lan"}],
                  [{**good, "label": 5}], [{**good, "unavailable": "yes"}], [good, {**good, "path": "/b"}], None):
        response = encrypted.send("sync.run", relays=value)
        assert response["error"] == {"code": "bad_params", "message": bad_text}, value
    # an empty list is no relay at all, before the store is looked at
    assert encrypted.send("sync.run", relays=[])["error"]["code"] == "not_found"
    # a list of one with a malformed address is bad_params before anything opens; a good one is this core's own refusal
    lan = {"id": "ab", "kind": "lan", "url": "nonsense"}
    assert encrypted.send("sync.run", relays=[lan])["error"] == {
        "code": "bad_params", "message": "relays: a LAN relay URL looks like http://host:port"}
    _relay_json(db_path, {"lan": "https://h:1"})
    assert encrypted.send("sync.run")["error"] == {
        "code": "bad_params", "message": "relay.json: a LAN relay is plain http:// (the bodies are encrypted; https is not supported)"}
    assert encrypted.send("sync.run", relays=[{**lan, "url": "http://127.0.0.1:9"}])["error"] == {
        "code": "unsupported_transport", "message": relay_config.LAN_TEXT}
    assert encrypted.result("sync.status")["relays"] == [{
        "id": "ab", "kind": "lan", "label": "", "pushed": 0, "healed": 0, "behind": 0, "pulled": 0, "rejected": 0,
        "error": "unsupported_transport"}]


def test_on_a_plaintext_store_not_encrypted_comes_before_the_shape_of_relays(plain, tmp_path):
    good = {"id": "ab", "kind": "folder", "path": str(tmp_path / "r")}
    assert plain.send("sync.run", relays=[])["error"]["code"] == "not_found"
    assert plain.send("sync.run", relays=[good])["error"]["code"] == "not_encrypted"
    assert plain.send("sync.run", relays="x")["error"]["code"] == "not_encrypted", "shape errors come after not_encrypted"


def test_a_lan_entry_among_folders_is_a_site_reported_unsupported_transport_and_the_folders_still_sync(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    one, two = tmp_path / "one", tmp_path / "two"
    for root in (one, two):
        root.mkdir()
    _relay_json(db_path, {"relays": [
        {"id": "00000001", "kind": "folder", "path": str(one), "serve": True},
        {"id": "00000002", "kind": "lan", "url": "http://127.0.0.1:9"},
        {"id": "00000003", "kind": "folder", "path": str(two)}]})
    result = encrypted.result("sync.run")
    assert result["status"] == "partial"
    assert _sites_ids(result) == [("00000001", "folder", None), ("00000002", "lan", "unsupported_transport"),
                                  ("00000003", "folder", None)]
    assert result["push"]["bundles"] == 1
    assert [s["pushed"] for s in result["sites"]] == [1, 0, 1]
    assert len(sync.FolderRelay(one, create_root=True).list(sync.account_for(keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)))) == 1
    status = encrypted.result("sync.status")
    assert status["relay_url"] == "http://127.0.0.1:9" and status["relays"] == result["sites"]


def test_relay_prefix_uses_the_serve_entry_and_a_list_without_one_serves_nothing(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    folder = str(tmp_path / "r")
    joiner = "this device is a joiner; it serves nothing"
    for body in ({"lan": "http://127.0.0.1:9"},
                 {"relays": [{"id": "ab", "kind": "folder", "path": folder}]},
                 {"relays": [{"id": "ab", "kind": "lan", "url": "http://h:1"}, {"id": "cd", "kind": "folder", "path": folder}]}):
        _relay_json(db_path, body)
        response = encrypted.send("relay.addresses")
        assert response["error"] == {"code": "bad_params", "message": joiner}, body
    # a serve entry passes the prefix (this core then has no server to run)
    for body in ({"folder": folder},
                 {"relays": [{"id": "ab", "kind": "lan", "url": "http://h:1"}, {"id": "cd", "kind": "folder", "path": folder, "serve": True}]}):
        _relay_json(db_path, body)
        assert encrypted.error_code("relay.addresses") == "unsupported_transport", body
    # a lan serve entry is a malformed file: it reads as nothing
    _relay_json(db_path, {"relays": [{"id": "ab", "kind": "lan", "url": "http://h:1", "serve": True}]})
    assert encrypted.error_code("relay.addresses") == "not_found"


def test_an_empty_relay_list_answers_not_found_and_a_public_lan_address_is_refused(encrypted, db_path):
    _relay_json(db_path, {"relays": []})
    encrypted.result("key.unlock", passphrase=PASS)
    for method in ("sync.run", "relay.addresses"):
        assert encrypted.send(method)["error"] == {
            "code": "not_found", "message": "no relay is configured (relay.json in the data folder)"}, method
    # a per-call list of one `lan` entry must be an IP literal on a private or local network
    for url in ("http://8.8.8.8:24816", "http://mac.local:24816"):
        response = encrypted.send("sync.run", relays=[{"id": "0000000a", "kind": "lan", "url": url}])
        assert response["error"] == {
            "code": "bad_params",
            "message": "relays: a LAN relay address must be an IP address on a private or local network, not a name"}, url
    # inside a list of two or more it is the site word bad_url
    folder = db_path.parent / "one"
    folder.mkdir()
    result = encrypted.result("sync.run", relays=[
        {"id": "0000000a", "kind": "folder", "path": str(folder), "label": "disk"},
        {"id": "0000000b", "kind": "lan", "url": "http://8.8.8.8:24816", "label": "mac"}])
    assert [(s["id"], s["label"], s["error"]) for s in result["sites"]] == [
        ("0000000a", "disk", None), ("0000000b", "mac", "bad_url")]
    assert result["status"] == "partial"


def test_a_strict_single_lan_failure_never_replaces_the_sites_of_a_run_in_flight(encrypted, db_path, tmp_path, monkeypatch):
    encrypted.result("key.unlock", passphrase=PASS)
    folder = tmp_path / "one"
    folder.mkdir()
    ran = encrypted.result("sync.run", relays=[{"id": "0000000a", "kind": "folder", "path": str(folder)}])
    assert ran["sites"][0]["id"] == "0000000a"
    lan = [{"id": "0000000b", "kind": "lan", "url": "http://127.0.0.1:9", "label": "mac"}]
    # the slot is held by another worker: the lan call is refused as before and records nothing
    assert encrypted.session.import_slot.acquire(blocking=False)
    refused = encrypted.send("sync.run", relays=lan)
    assert refused["error"]["code"] == "unsupported_transport"
    assert encrypted.result("sync.status")["relays"] == ran["sites"], "the last run's sites are untouched"
    encrypted.session.import_slot.release()
    # the slot is free: the row is recorded (with the label) and the slot is free again
    assert encrypted.send("sync.run", relays=lan)["error"]["code"] == "unsupported_transport"
    assert encrypted.result("sync.status")["relays"] == [{
        "id": "0000000b", "kind": "lan", "label": "mac", "pushed": 0, "healed": 0, "behind": 0, "pulled": 0,
        "rejected": 0, "error": "unsupported_transport"}]
    assert encrypted.session.import_slot.acquire(blocking=False), "the slot was released"
    encrypted.session.import_slot.release()


def test_auto_runs_the_folder_sites_and_reports_lan_not_auto(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    one, two = tmp_path / "one", tmp_path / "two"
    for root in (one, two):
        root.mkdir()
    # on this core a lan entry would otherwise be unsupported_transport; under auto it is never looked at
    _relay_json(db_path, {"relays": [
        {"id": "0000000a", "kind": "folder", "path": str(one)},
        {"id": "0000000b", "kind": "folder", "path": str(two)},
        {"id": "0000000c", "kind": "lan", "url": "http://127.0.0.1:9", "label": "mac"}]})
    result = encrypted.result("sync.run", auto=True)
    assert result["status"] == "ok"
    assert _sites_ids(result) == [("0000000a", "folder", None), ("0000000b", "folder", None),
                                  ("0000000c", "lan", "not_auto")]
    assert [s["pushed"] for s in result["sites"]] == [1, 1, 0]
    lan = {"id": "0000000c", "kind": "lan", "label": "mac", "pushed": 0, "healed": 0, "behind": 0, "pulled": 0,
           "rejected": 0, "error": "not_auto"}
    assert result["sites"][2] == lan
    status = encrypted.result("sync.status")
    assert status["relays"][2] == lan and status["relay_kind"] == "mixed"


def test_auto_with_no_folder_site_is_not_folder(encrypted, db_path):
    encrypted.result("key.unlock", passphrase=PASS)
    _relay_json(db_path, {"lan": "http://127.0.0.1:9"})
    assert encrypted.send("sync.run", auto=True)["error"] == {
        "code": "not_folder", "message": "auto sync runs over folder relays only; the list has none"}
    response = encrypted.send("sync.run", auto=True, relays=[{"id": "0000000a", "kind": "lan", "url": "http://8.8.8.8:1"}])
    assert response["error"]["code"] == "not_folder"
    assert encrypted.send("sync.run", auto=True, relays=[])["error"]["code"] == "not_found"


def test_auto_must_be_true_or_absent(encrypted, db_path, tmp_path):
    encrypted.result("key.unlock", passphrase=PASS)
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    for bad in (False, None, 1, "true"):
        assert encrypted.send("sync.run", auto=bad)["error"] == {"code": "bad_params", "message": "auto: true or absent"}, bad


def test_relay_kind_in_sync_status(encrypted, db_path):
    encrypted.result("key.unlock", passphrase=PASS)
    assert encrypted.result("sync.status")["relay_kind"] is None
    for body, kind in (({"relays": []}, None), ({"folder": "/f"}, "folder"), ({"lan": "http://127.0.0.1:9"}, "lan"),
                       ({"relays": [{"id": "0000000a", "kind": "folder", "path": "/f"},
                                    {"id": "0000000b", "kind": "lan", "url": "http://127.0.0.1:9"}]}, "mixed")):
        _relay_json(db_path, body)
        assert encrypted.result("sync.status")["relay_kind"] == kind, body
