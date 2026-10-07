"""Where the data folder is (Bet 02a): pure resolution, the legacy read-through, one stderr hint."""

import pathlib

import pytest

from disconect import cli, identity, storage
from disconect.relay import config as relay_config
from disconect.storage import home

LEGACY_HINT = "using legacy data folder ~/.hearthbeat; run 'disconect migrate-home' to move it"


def _touch_db(folder: pathlib.Path, name: str) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    return path


def _tree(root: pathlib.Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_the_new_file_wins_over_the_legacy_one(tmp_path):
    new = _touch_db(tmp_path / ".disconect", "disconect.db")
    _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    assert home.resolve_default_db() == (new, None)
    assert storage.default_db_path() == new


def test_the_legacy_file_is_read_through_when_only_it_exists(tmp_path):
    legacy = _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    assert home.resolve_default_db() == (legacy, ".hearthbeat")
    assert storage.default_db_path() == legacy


def test_a_db_less_new_folder_does_not_hide_the_legacy_store(tmp_path):
    """The decision is on the db *file*: `sync --remember` makes ~/.disconect with only relay.json."""
    (tmp_path / ".disconect").mkdir()
    (tmp_path / ".disconect" / "relay.json").write_text("{}")
    legacy = _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    assert home.resolve_default_db() == (legacy, ".hearthbeat")


def test_neither_exists_gives_the_new_path_and_creates_nothing(tmp_path):
    before = _tree(tmp_path)
    assert home.resolve_default_db() == (tmp_path / ".disconect" / "disconect.db", None)
    assert home.relay_config_path() == tmp_path / ".disconect" / "relay.json"
    assert _tree(tmp_path) == before, "resolution must never mkdir"


def test_a_legacy_folder_without_the_db_file_is_not_a_store(tmp_path):
    (tmp_path / ".hearthbeat").mkdir()
    (tmp_path / ".hearthbeat" / "relay.json").write_text("{}")
    assert home.resolve_default_db() == (tmp_path / ".disconect" / "disconect.db", None)


def test_the_env_override_wins_and_is_never_a_legacy_read(tmp_path, monkeypatch):
    _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    monkeypatch.setenv("DISCONECT_DB", "~/elsewhere/x.db")
    assert home.resolve_default_db() == (tmp_path / "elsewhere" / "x.db", None)
    assert home.relay_config_path() == tmp_path / "elsewhere" / "relay.json"


def test_relay_config_follows_the_resolved_folder(tmp_path):
    _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    assert home.relay_config_path() == tmp_path / ".hearthbeat" / "relay.json"
    _touch_db(tmp_path / ".disconect", "disconect.db")
    assert home.relay_config_path() == tmp_path / ".disconect" / "relay.json"


def test_the_cli_prints_the_legacy_hint_once_on_stderr_and_moves_nothing(tmp_path, capsys):
    (tmp_path / ".hearthbeat").mkdir()
    with storage.open_for_write(tmp_path / ".hearthbeat" / "hearthbeat.db", "test"):
        pass
    code = cli.main(["status"])
    out, err = capsys.readouterr()
    assert code == 0
    assert err.count(LEGACY_HINT) == 1 and LEGACY_HINT not in out
    assert not (tmp_path / ".disconect").exists()
    assert (tmp_path / ".hearthbeat" / "hearthbeat.db").is_file()


def test_no_hint_for_an_explicit_db_for_help_or_for_the_new_folder(tmp_path, capsys):
    (tmp_path / ".hearthbeat").mkdir()
    with storage.open_for_write(tmp_path / ".hearthbeat" / "hearthbeat.db", "test"):
        pass
    scratch = tmp_path / "other.db"
    with storage.open_for_write(scratch, "test"):
        pass
    assert cli.main(["--db", str(scratch), "status"]) == 0
    assert LEGACY_HINT not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert LEGACY_HINT not in capsys.readouterr().err
    with storage.open_for_write(tmp_path / ".disconect" / "disconect.db", "test"):
        pass
    assert cli.main(["status"]) == 0
    assert LEGACY_HINT not in capsys.readouterr().err


def test_a_missing_store_stays_missing_and_no_folder_appears(tmp_path, capsys):
    assert cli.main(["status"]) == cli.EXIT_NOT_CONFIGURED
    assert not (tmp_path / ".disconect").exists() and not (tmp_path / ".hearthbeat").exists()


def test_old_env_names_warn_once_naming_the_new_one_and_are_not_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEARTHBEAT_DB", str(tmp_path / "ignored.db"))
    monkeypatch.setenv("HEARTHBEAT_KEYS", str(tmp_path / "ignored.keys"))
    assert storage.default_db_path() == tmp_path / ".disconect" / "disconect.db"
    assert cli.main(["status"]) == cli.EXIT_NOT_CONFIGURED
    err = capsys.readouterr().err
    assert err.count("no longer read") == 1
    assert "HEARTHBEAT_DB, HEARTHBEAT_KEYS are no longer read; set DISCONECT_DB, DISCONECT_KEYS instead" in err
    monkeypatch.delenv("HEARTHBEAT_KEYS")
    assert "HEARTHBEAT_DB is no longer read; set DISCONECT_DB instead" == home.legacy_env_warning()[len("warning: "):]


def test_no_warning_without_old_env_names():
    assert home.legacy_env_warning({}) is None
    assert home.legacy_env_warning({"DISCONECT_DB": "x", "HEARTHBEAT_DB": ""}) is None


def test_the_legacy_names_are_the_identity_constants():
    assert (identity.LEGACY_HOMES, identity.LEGACY_DB_FILENAME) == ([".hearthbeat"], "hearthbeat.db")


# ---- F2: the half-done migrate-home state ----

def test_a_half_done_migrate_home_resolves_to_the_renamed_db_in_the_old_folder(tmp_path):
    """Siblings renamed, folder not yet: ~/.hearthbeat/disconect.db is the store, reported as legacy."""
    half = _touch_db(tmp_path / ".hearthbeat", "disconect.db")
    assert home.resolve_default_db() == (half, ".hearthbeat")
    assert home.relay_config_path() == tmp_path / ".hearthbeat" / "relay.json"


def test_the_old_name_wins_over_the_renamed_one_when_both_are_in_the_old_folder(tmp_path):
    _touch_db(tmp_path / ".hearthbeat", "disconect.db")
    old = _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    assert home.resolve_default_db() == (old, ".hearthbeat")


# ---- F1b: a writer never re-creates a legacy folder that migrate-home moved away ----

MOVED = "data folder ~/.hearthbeat has moved; restart DISCOnect"


def _resolved_legacy_then_moved(tmp_path):
    """Resolve under a legacy HOME, then remove the folder (what migrate-home does underneath a running process)."""
    legacy = _touch_db(tmp_path / ".hearthbeat", "hearthbeat.db")
    resolved, name = home.resolve_default_db()
    assert (resolved, name) == (legacy, ".hearthbeat")
    legacy.unlink()
    legacy.parent.rmdir()
    return resolved


def test_open_for_write_refuses_a_moved_legacy_folder_and_creates_nothing(tmp_path):
    resolved = _resolved_legacy_then_moved(tmp_path)
    with pytest.raises(storage.HomeMoved, match=MOVED), storage.open_for_write(resolved, "import"):
        pass
    assert not (tmp_path / ".hearthbeat").exists() and not (tmp_path / ".disconect").exists()


def test_the_write_lock_and_the_key_file_refuse_a_moved_legacy_folder_too(tmp_path):
    from disconect.storage import keys
    from disconect.storage.write_lock import write_lock

    resolved = _resolved_legacy_then_moved(tmp_path)
    with pytest.raises(storage.HomeMoved, match=MOVED), write_lock(resolved, "x"):
        pass
    with pytest.raises(storage.HomeMoved, match=MOVED):
        keys.write_key_file(keys.key_path_for(resolved), {})
    assert not (tmp_path / ".hearthbeat").exists()


def test_the_cli_maps_a_moved_folder_to_the_not_configured_exit_code(tmp_path, capsys):
    resolved = _resolved_legacy_then_moved(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    assert cli.main(["--db", str(resolved), "import", str(src)]) == cli.EXIT_NOT_CONFIGURED
    assert MOVED in capsys.readouterr().err
    assert not (tmp_path / ".hearthbeat").exists()


def test_a_missing_folder_that_is_not_a_legacy_home_is_still_created(tmp_path):
    with storage.open_for_write(tmp_path / "fresh" / "x.db", "test"):
        pass
    assert (tmp_path / "fresh" / "x.db").is_file()
    with storage.open_for_write(tmp_path / ".disconect" / "disconect.db", "test"):
        pass
    assert (tmp_path / ".disconect" / "disconect.db").is_file()


def test_a_bare_tilde_expands_and_a_named_user_is_left_alone_like_the_rust_core(monkeypatch):
    """The Rust twin is `keys.rs::home_expansion`. ``Path.expanduser`` would turn ``~root/x`` into ``/var/root/x`` and
    the two cores would open different folders; with no ``$HOME`` it would fall back to the password database."""
    monkeypatch.setenv("HOME", "/h")
    assert home.expand_user("~root/x") == pathlib.Path("~root/x")
    assert home.expand_user("~nobody") == pathlib.Path("~nobody")
    assert home.expand_user("/abs/~") == pathlib.Path("/abs/~")
    assert home.expand_user("rel/~/x") == pathlib.Path("rel/~/x")
    assert home.expand_user("./~/a") == pathlib.Path("./~/a"), "a folder named ~ (the parts would drop the dot)"
    assert home.expand_user("~\\x") == pathlib.Path("~\\x")
    assert home.expand_user("~") == pathlib.Path("/h")
    assert home.expand_user("~/") == pathlib.Path("/h")
    assert home.expand_user("~/a/b") == pathlib.Path("/h/a/b")
    assert home.expand_user("~//a") == pathlib.Path("/h/a")
    monkeypatch.delenv("HOME")
    assert home.expand_user("~/a") == pathlib.Path("~/a")
    monkeypatch.setenv("HOME", "")
    assert home.expand_user("~/a") == pathlib.Path("~/a"), "an empty HOME is unset on both cores"
    assert home.expand_user("~") == pathlib.Path("~")
    # every relay and override site reads through it: a named user's folder is the relative folder as written
    assert relay_config.open_relay("folder", "~root/x").root == pathlib.Path("~root/x")

