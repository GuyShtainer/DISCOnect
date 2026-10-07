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

REAL_RUNNING_PROGRAMS = migrate_home.running_programs      # the autouse fixture stubs the module attribute
LEGACY = ".hearthbeat"
NEW = ".disconect"


@pytest.fixture(autouse=True)
def _no_product_running(monkeypatch):
    """The real process table may hold the real app; tests that care use ``test_process_check``'s own spawn."""
    monkeypatch.setattr(migrate_home, "running_programs", lambda own_pid=None: [])


def _store(folder: pathlib.Path, name: str) -> pathlib.Path:
    """A real (plaintext, WAL) store with one marker row, closed cleanly."""
    folder.mkdir(parents=True, exist_ok=True)       # a writer never creates a missing legacy folder
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


# ---- review fixes (F1a, F3, nit) ----

def _named_sleeper(tmp_path, name: str) -> subprocess.Popen:
    """A real idle process whose argv[0] is ``<tmp>/bin/<name>`` (``exec -a``), as the app or serve would be."""
    import time
    argv0 = str(tmp_path / "bin" / name)
    proc = subprocess.Popen(["bash", "-c", 'exec -a "$0" sleep 60', argv0])
    for _ in range(100):                      # until pgrep can see it under its new name
        if subprocess.run(["pgrep", "-f", "--", name], capture_output=True).returncode == 0:
            break
        time.sleep(0.05)
    return proc


def _stop(*procs):
    for proc in procs:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("name", list(migrate_home.RUNNING_NAMES))
def test_an_idle_running_program_refuses_the_move_and_touches_nothing(tmp_path, capsys, monkeypatch, name):
    """The reviewer's F1: an idle app or MCP server holds no file open, so lsof sees nothing."""
    monkeypatch.setattr(migrate_home, "running_programs", REAL_RUNNING_PROGRAMS)
    db = _old_style(tmp_path)
    before = _names(db.parent)
    proc = _named_sleeper(tmp_path, name)
    # the refusal names the lowest pid of ours that is running, and a sibling session's real core or app may come
    # before the sleeper: keep the real pgrep detection but let only the launched pid through (BL-1 review)
    monkeypatch.setattr(migrate_home, "running_programs",
                        lambda own_pid=None: [p for p in REAL_RUNNING_PROGRAMS(own_pid) if p[1] == proc.pid])
    try:
        code, _, err = _run(capsys)
    finally:
        _stop(proc)
    assert code == cli.EXIT_USAGE
    assert f"migrate-home: {name} (pid {proc.pid}) is running; quit the app and Claude Desktop, then retry" in err
    _assert_untouched(tmp_path, before)


def test_the_check_matches_a_full_bundle_path_but_not_a_longer_name(tmp_path):
    bundle = tmp_path / "disconect-app.app" / "Contents" / "MacOS"
    proc = _named_sleeper(bundle, "disconect-app")
    longer = _named_sleeper(tmp_path, "disconect-serve-helper")
    try:
        found = REAL_RUNNING_PROGRAMS()
    finally:
        _stop(proc, longer)
    assert ("disconect-app", proc.pid) in found
    assert all(pid != longer.pid for _, pid in found)


def test_this_process_and_its_parents_are_never_reported():
    assert os.getpid() in migrate_home._ancestors(os.getpid())
    assert all(pid != os.getpid() for _, pid in REAL_RUNNING_PROGRAMS())


def test_nothing_running_lets_the_move_through(tmp_path, capsys):
    _old_style(tmp_path)
    assert _run(capsys)[0] == 0 and (tmp_path / NEW / "disconect.db").is_file()


def test_a_half_done_state_resolves_and_finishes_through_the_default_path(tmp_path, capsys):
    """F2 end to end: siblings renamed, folder not yet; the CLI reads it, a rerun finishes it."""
    db = _old_style(tmp_path)
    for name in _names(db.parent):
        if name.startswith("hearthbeat.db"):
            (db.parent / name).rename(db.parent / ("disconect.db" + name[len("hearthbeat.db"):]))
    assert storage.default_db_path() == db.parent / "disconect.db"
    assert cli.main(["status"]) == 0
    assert "using legacy data folder" in capsys.readouterr().err
    assert _run(capsys)[0] == 0
    assert _marker(tmp_path / NEW / "disconect.db") == ["survived"] and not db.parent.exists()


def test_a_symlinked_legacy_folder_merges_into_an_existing_db_less_new_folder(tmp_path, capsys):
    """F3 (the reviewer's case D2): ~/.hearthbeat -> a real directory, ~/.disconect already exists."""
    target = tmp_path / "elsewhere" / "data"
    _store(target, "hearthbeat.db")
    (target / "relay.json").write_text('{"folder": "/r"}')
    (tmp_path / LEGACY).symlink_to(target, target_is_directory=True)
    (tmp_path / NEW).mkdir()
    code, _, err = _run(capsys)
    assert code == 0, err
    assert not (tmp_path / LEGACY).exists() and not (tmp_path / LEGACY).is_symlink()
    assert target.is_dir() and _names(target) == [], "the target directory itself is left, emptied of what moved"
    assert {"disconect.db", "relay.json"} <= set(_names(tmp_path / NEW))
    assert _marker(tmp_path / NEW / "disconect.db") == ["survived"]


def test_a_symlink_whose_target_keeps_entries_is_left_and_reported(tmp_path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()
    (target / "stays").write_text("x")
    link = tmp_path / LEGACY
    link.symlink_to(target, target_is_directory=True)
    assert migrate_home._remove_legacy(link) == ["stays"]
    assert link.is_symlink() and (target / "stays").read_text() == "x"


def test_a_keyed_store_that_cannot_be_unlocked_says_nothing_was_moved(tmp_path, capsys):
    legacy = tmp_path / LEGACY
    legacy.mkdir()
    db = legacy / "hearthbeat.db"
    code = ("import os, sys\nfrom disconect.storage import sqlite as s\n"
            "c = s.connect(sys.argv[1], isolation_level=None)\n"
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')\n"
            "c.execute('CREATE TABLE marker (v TEXT)')\nos._exit(0)\n")
    subprocess.run([sys.executable, "-c", code, str(db)], check=True)
    (legacy / "hearthbeat.db.keys.json").write_text("{}")    # keyed, but nothing can unlock it
    before = _names(legacy)
    code, _, err = _run(capsys)
    assert code == cli.EXIT_LOCKED and err.startswith("locked: ") and err.rstrip().endswith("nothing was moved")
    # taking the write lock leaves its own (empty) file; no database file was renamed, no folder appeared
    assert set(_names(legacy)) - {"hearthbeat.db.write-lock"} == set(before) and not (tmp_path / NEW).exists()
