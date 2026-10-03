"""`disconect migrate-home`: every refusal, the happy path, the half-done rerun, the merge, idempotence.

Synthetic stores in scratch HOMEs only (conftest points HOME at tmp_path); the real home is never touched.
"""

import fcntl
import os
import pathlib
import subprocess
import sys

import pytest

from disconect import cli, storage
from disconect.storage import keys, migrate_home

LEGACY = ".hearthbeat"
NEW = ".disconect"


def _store(folder: pathlib.Path, name: str) -> pathlib.Path:
    """A real (plaintext, WAL) store with one marker row, closed cleanly."""
    db = folder / name
    with storage.open_for_write(db, "test") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES ('survived')")
    return db


def _names(folder: pathlib.Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _marker(db: pathlib.Path) -> list[str]:
    conn = storage.open_read_only(db)
    try:
        return [row[0] for row in conn.execute("SELECT v FROM marker")]
    finally:
        conn.close()


def _run(capsys, *argv):
    code = cli.main(["migrate-home", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def _old_style(tmp_path) -> pathlib.Path:
    legacy = tmp_path / LEGACY
    db = _store(legacy, "hearthbeat.db")
    (legacy / "hearthbeat.db.write-lock").touch()
    return db


def test_the_happy_path_renames_every_sibling_then_the_folder(tmp_path, capsys):
    db = _old_style(tmp_path)
    legacy = db.parent
    (legacy / "hearthbeat.db.keys.json").write_text("{}")
    (legacy / "hearthbeat.db.pre-restore-20260101T000000Z").write_bytes(b"x")
    (legacy / "hearthbeat.db.plaintext-rollback").write_bytes(b"x")
    (legacy / "backups").mkdir()
    (legacy / "backups" / "hearthbeat-20260101T000000Z.db").write_bytes(b"x")
    (legacy / "relay.json").write_text("{}")
    code, out, err = _run(capsys)
    assert code == 0, err
    assert not legacy.exists()
    new = tmp_path / NEW
    assert _names(new) == ["backups", "disconect.db", "disconect.db.keys.json",
                           "disconect.db.plaintext-rollback", "disconect.db.pre-restore-20260101T000000Z",
                           "disconect.db.write-lock", "relay.json"]
    assert _names(new / "backups") == ["hearthbeat-20260101T000000Z.db"], "backups names are left alone"
    (new / "disconect.db.keys.json").unlink()            # a stub: the marker read below is plaintext
    assert _marker(new / "disconect.db") == ["survived"]
    assert "before ~/.hearthbeat:" in out and "after ~/.disconect:" in out and "  hearthbeat.db" in out
    assert storage.default_db_path() == new / "disconect.db"


def test_a_second_full_run_is_a_noop(tmp_path, capsys):
    _old_style(tmp_path)
    assert _run(capsys)[0] == 0
    after = _names(tmp_path / NEW)
    code, out, _ = _run(capsys)
    assert code == 0 and out.strip() == "nothing to migrate"
    assert _names(tmp_path / NEW) == after


def test_the_json_report(tmp_path, capsys):
    _old_style(tmp_path)
    code = cli.main(["--json", "migrate-home"])
    import json
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["moved"] is True and "disconect.db" in report["after"]


def test_a_nonempty_wal_is_folded_in_before_the_rename(tmp_path, capsys):
    """A writer that died after committing leaves its pages in -wal; a bare rename would drop them."""
    legacy = tmp_path / LEGACY
    legacy.mkdir()
    db = legacy / "hearthbeat.db"
    code = ("import os, sys\nfrom disconect.storage import sqlite as s\n"
            "c = s.connect(sys.argv[1], isolation_level=None)\n"
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')\n"
            "c.execute('CREATE TABLE marker (v TEXT)'); c.execute(\"INSERT INTO marker VALUES ('in the wal')\")\n"
            "os._exit(0)\n")
    subprocess.run([sys.executable, "-c", code, str(db)], check=True)
    assert (legacy / "hearthbeat.db-wal").stat().st_size > 0
    assert _run(capsys)[0] == 0
    new = tmp_path / NEW
    assert [n for n in _names(new) if n.endswith("-wal")] in ([], ["disconect.db-wal"])
    if (new / "disconect.db-wal").exists():
        assert (new / "disconect.db-wal").stat().st_size == 0
    assert _marker(new / "disconect.db") == ["in the wal"]


def test_an_encrypted_store_migrates_and_its_key_file_follows(tmp_path, capsys, monkeypatch):
    legacy = tmp_path / LEGACY
    db = _store(legacy, "hearthbeat.db")
    monkeypatch.setenv(keys.PASSPHRASE_ENV, "a perfectly fine passphrase")
    assert cli.main(["--db", str(db), "key", "init"]) == 0
    capsys.readouterr()
    assert (legacy / "hearthbeat.db.keys.json").exists()
    storage._unlocked.clear(); keys.forget_session()
    code, _, err = _run(capsys)
    assert code == 0, err
    new = tmp_path / NEW
    assert (new / "disconect.db.keys.json").exists() and not (new / "hearthbeat.db.keys.json").exists()
    storage._unlocked.clear(); keys.forget_session()
    monkeypatch.setenv(keys.PASSPHRASE_ENV, "a perfectly fine passphrase")   # unlock consumes it
    assert _marker(new / "disconect.db") == ["survived"]


# ---- refusals: nothing is moved ----

def _assert_untouched(tmp_path, listing):
    assert _names(tmp_path / LEGACY) == listing and not (tmp_path / NEW).exists()


@pytest.mark.parametrize("name", ["HEARTHBEAT_DB", "HEARTHBEAT_KEYS", "HEARTHBEAT_ANYTHING", "DISCONECT_DB"])
def test_refuses_when_an_env_name_pins_the_path(tmp_path, capsys, monkeypatch, name):
    db = _old_style(tmp_path)
    listing = _names(db.parent)
    monkeypatch.setenv(name, "/somewhere")
    code, out, err = _run(capsys)
    assert code == cli.EXIT_USAGE and f"migrate-home: {name} is set; unset it and rerun" in err
    _assert_untouched(tmp_path, listing)


def test_refuses_when_there_is_no_legacy_folder(tmp_path, capsys):
    code, out, err = _run(capsys)
    assert code == cli.EXIT_USAGE
    assert f"migrate-home: no legacy data folder ~/{LEGACY} to migrate" in err


def test_refuses_when_a_database_exists_in_both_folders(tmp_path, capsys):
    db = _old_style(tmp_path)
    _store(tmp_path / NEW, "disconect.db")
    listing = _names(db.parent)
    code, _, err = _run(capsys)
    assert code == cli.EXIT_USAGE
    assert "a database exists in both ~/.hearthbeat and ~/.disconect; nothing was moved" in err
    assert _names(db.parent) == listing and _marker(tmp_path / NEW / "disconect.db") == ["survived"]


def test_refuses_when_the_write_lock_is_held(tmp_path, capsys):
    db = _old_style(tmp_path)
    listing = _names(db.parent)
    with open(db.parent / "hearthbeat.db.write-lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, err = _run(capsys)
    assert code == cli.EXIT_USAGE
    assert "migrate-home: the legacy store's write lock is held; quit the app and Claude Desktop, then retry" in err
    assert _names(db.parent) == listing and not (tmp_path / NEW).exists()


def test_refuses_when_another_process_has_the_database_open(tmp_path, capsys):
    db = _old_style(tmp_path)
    reader = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\nfrom disconect.storage import sqlite as s\n"
         "c = s.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)\n"
         "c.execute('SELECT count(*) FROM sqlite_master').fetchone()\n"
         "print('open', flush=True); sys.stdin.readline()\n", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert reader.stdout.readline().strip() == "open"
        listing = _names(db.parent)
        code, _, err = _run(capsys)
        assert code == cli.EXIT_USAGE
        assert f"migrate-home: another process (pid {reader.pid}) has the legacy database open; " \
               "quit the app and Claude Desktop, then retry" in err
        assert _names(db.parent) == listing and not (tmp_path / NEW).exists()
    finally:
        reader.communicate("\n")
    assert _run(capsys)[0] == 0, "once the reader is gone the move goes through"


# ---- crash recovery and merging ----

def test_a_half_done_state_is_finished_by_a_rerun(tmp_path, capsys):
    """Siblings renamed, folder not yet: the state a crash between steps 3 and 4 leaves."""
    db = _old_style(tmp_path)
    legacy = db.parent
    (legacy / "hearthbeat.db.keys.json").write_text("{}")
    for entry in list(legacy.iterdir()):
        if entry.name.startswith("hearthbeat.db"):
            entry.rename(legacy / ("disconect.db" + entry.name[len("hearthbeat.db"):]))
    code, _, err = _run(capsys)
    assert code == 0, err
    new = tmp_path / NEW
    assert not legacy.exists() and "disconect.db" in _names(new)
    (new / "disconect.db.keys.json").unlink()            # a stub: the marker read below is plaintext
    assert _marker(new / "disconect.db") == ["survived"]
    assert not [n for n in _names(new) if n.startswith("hearthbeat")]


def test_a_half_done_state_with_the_lock_file_not_renamed_yet(tmp_path, capsys):
    db = _old_style(tmp_path)
    legacy = db.parent
    for entry in list(legacy.iterdir()):
        if entry.name.startswith("hearthbeat.db") and not entry.name.endswith(".write-lock"):
            entry.rename(legacy / ("disconect.db" + entry.name[len("hearthbeat.db"):]))
    assert _run(capsys)[0] == 0
    new = tmp_path / NEW
    assert _names(new) == ["disconect.db", "disconect.db.write-lock"] or "disconect.db.write-lock" in _names(new)
    assert "hearthbeat.db.write-lock" not in _names(new)


def test_a_db_less_new_folder_with_a_relay_config_is_merged(tmp_path, capsys):
    db = _old_style(tmp_path)
    (tmp_path / NEW).mkdir()
    (tmp_path / NEW / "relay.json").write_text('{"folder": "/r"}')
    code, _, err = _run(capsys)
    assert code == 0, err
    assert not db.parent.exists()
    new = tmp_path / NEW
    assert _names(new) == ["disconect.db", "disconect.db.write-lock", "relay.json"] or \
        {"disconect.db", "relay.json"} <= set(_names(new))
    assert (new / "relay.json").read_text() == '{"folder": "/r"}'
    assert _marker(new / "disconect.db") == ["survived"]


def test_a_clashing_file_in_a_db_less_new_folder_refuses(tmp_path, capsys):
    db = _old_style(tmp_path)
    (db.parent / "relay.json").write_text("{}")
    (tmp_path / NEW).mkdir()
    (tmp_path / NEW / "relay.json").write_text("{}")
    code, _, err = _run(capsys)
    assert code == cli.EXIT_USAGE and "relay.json would overwrite a file that already exists" in err
    assert (db.parent / "hearthbeat.db").exists()


def test_a_legacy_folder_without_a_database_is_moved_whole(tmp_path, capsys):
    legacy = tmp_path / LEGACY
    legacy.mkdir()
    (legacy / "relay.json").write_text("{}")
    assert _run(capsys)[0] == 0
    assert _names(tmp_path / NEW) == ["relay.json"] and not legacy.exists()


def test_the_migrated_store_opens_through_the_default_path(tmp_path, capsys):
    _old_style(tmp_path)
    assert _run(capsys)[0] == 0
    assert cli.main(["status"]) == 0
    assert "using legacy data folder" not in capsys.readouterr().err
