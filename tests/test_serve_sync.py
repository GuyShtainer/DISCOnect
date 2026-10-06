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


def test_a_plaintext_store_reports_empty_counts_and_cannot_run(plain, db_path, tmp_path):
    assert plain.result("sync.status")["bundles"] == {}
    status = plain.result("sync.status")
    assert set(status) == {"bundles", "records_unsent", "records_seen", "conflicts", "superseded", "gaps",
                           "last_pushed_at", "last_pulled_at"}
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
                      "last_pushed_at": None, "last_pulled_at": None}


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
    assert set(result) == {"push", "pull"}
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
    again = other.result("sync.run")
    assert again["pull"]["applied"] == 0 and again["push"]["bundles"] == 0
    status = other.result("sync.status")
    assert status["bundles"] == {"pulled_applied": 1}
    assert status["last_pulled_at"] >= "2020" and status["last_pushed_at"] is None
    # a run that moved nothing books no bundle, so the times do not advance
    assert other.result("sync.status")["last_pulled_at"] == status["last_pulled_at"]


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


def test_sync_status_is_read_only_and_ignores_params(plain, db_path):
    before = db_path.read_bytes()
    assert plain.result("sync.status", limit=1, x=[1]) == plain.result("sync.status")
    assert db_path.read_bytes() == before


def test_sync_methods_are_registered():
    assert {"sync.status", "sync.run"} <= set(serve.METHODS)
    assert sync is not None
