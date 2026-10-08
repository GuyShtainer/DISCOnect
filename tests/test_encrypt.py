"""Encryption at rest: conversion, keyed opens, backups, recovery, downgrade refusal, and no leaks."""

import io
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from disconect import cli, storage
from disconect.ingest import sources
from disconect.storage import backup, encrypt, keys, migrations
from test_import import _build_export

PASS = "a perfectly fine passphrase"
PASS2 = "another perfectly fine passphrase"


def _populate(tmp_path, db_path):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    with storage.open_for_write(db_path, "test") as conn:
        sources.import_path(root, conn)
        return conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0]


def _run(argv, capsys, env_pass=PASS):
    if env_pass is not None:
        os.environ[keys.PASSPHRASE_ENV] = env_pass
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _unlock_env(passphrase=PASS):
    os.environ[keys.PASSPHRASE_ENV] = passphrase


def _init(db_path, capsys):
    code, out, err = _run(["--db", str(db_path), "key", "init"], capsys)
    assert code == cli.EXIT_OK, err
    storage._unlocked.clear(); keys.forget_session()          # later steps must unlock on their own, like a fresh process would
    return out, err


def test_init_encrypts_and_plaintext_cannot_open(tmp_path, db_path, capsys):
    rows = _populate(tmp_path, db_path)
    version_before = migrations.SCHEMA_VERSION
    out, err = _init(db_path, capsys)
    assert "recovery phrase NOT shown" in err, "words never go to a non-terminal"
    assert storage.is_encrypted_file(db_path) is True
    assert not db_path.read_bytes().startswith(storage.SQLITE_MAGIC)
    plain = storage.sqlite.connect(str(db_path))
    with pytest.raises(storage.DatabaseError):
        plain.execute("SELECT count(*) FROM daily_metrics").fetchone()
    plain.close()
    _unlock_env()
    conn = storage.open_read_only(db_path)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == rows
    assert migrations.current_version(conn) == version_before, "sqlcipher_export drops user_version; we carry it"
    conn.close()
    rollback = pathlib.Path(str(db_path) + encrypt.ROLLBACK_SUFFIX)
    assert rollback.exists() and rollback.read_bytes().startswith(storage.SQLITE_MAGIC)
    assert not pathlib.Path(str(db_path) + "-wal").exists()
    code, out, err = _run(["--db", str(db_path), "encrypt", "--purge-plaintext"], capsys)
    assert code == 0 and not rollback.exists() and "Time Machine" in out


def test_status_and_import_work_on_encrypted_store(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    code, out, err = _run(["--db", str(db_path), "status", "--days", "3650"], capsys)
    assert code == 0 and "coverage" in out
    code, out, err = _run(["--db", str(db_path), "import", str(tmp_path / "export")], capsys)
    assert code == 0 and out.startswith("ok:")


def test_wrong_passphrase_and_missing_key_file(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    code, out, err = _run(["--db", str(db_path), "status"], capsys, env_pass="definitely not the passphrase")
    assert code == cli.EXIT_LOCKED and "wrong passphrase" in err
    key_path = keys.key_path_for(db_path)
    key_path.rename(key_path.with_name("gone.json"))
    code, out, err = _run(["--db", str(db_path), "status"], capsys, env_pass=None)
    assert code == cli.EXIT_LOCKED and "no key file" in err and "not a database" not in err


def test_downgrade_is_refused(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    rollback = pathlib.Path(str(db_path) + encrypt.ROLLBACK_SUFFIX)
    db_path.unlink()
    rollback.rename(db_path)  # an attacker (or a mistake) puts the plaintext back
    code, out, err = _run(["--db", str(db_path), "status"], capsys)
    assert code == cli.EXIT_LOCKED and "plaintext but a key file exists" in err
    code, out, err = _run(["--db", str(db_path), "import", str(tmp_path / "export")], capsys)
    assert code == cli.EXIT_LOCKED, "no import may ever write plaintext beside a key file"


def test_no_unlock_path_is_locked_not_a_hang(tmp_path, db_path, capsys, monkeypatch):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, out, err = _run(["--db", str(db_path), "status"], capsys, env_pass=None)
    assert code == cli.EXIT_LOCKED and "key cache" in err


def test_keychain_cache_unlocks_without_passphrase(tmp_path, db_path, capsys, _isolated_secrets):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    code, out, err = _run(["--db", str(db_path), "key", "cache"], capsys)
    assert code == 0 and len(_isolated_secrets.items) == 1
    (service, account), value = next(iter(_isolated_secrets.items.items()))
    assert service == "disconect-cli" and re.fullmatch(r"[0-9a-f]{32}", account), "keychain item named by key_id, not a path"
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _run(["--db", str(db_path), "status"], capsys, env_pass=None)
    assert code == 0
    code, out, err = _run(["--db", str(db_path), "key", "cache", "--remove"], capsys)
    assert code == 0 and not _isolated_secrets.items


def test_cache_remove_also_deletes_the_item_an_older_build_left_under_its_service(
        tmp_path, db_path, capsys, _isolated_secrets):
    """F4 of the 02a review: the old CLI keychain service ``hearthbeat`` must not keep an orphaned copy of the key."""
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    assert _run(["--db", str(db_path), "key", "cache"], capsys)[0] == 0
    (_, key_id), master_hex = next(iter(_isolated_secrets.items.items()))
    legacy = ("hearthbeat", key_id)
    _isolated_secrets.items[legacy] = master_hex
    other = ("hearthbeat", "0" * 32)                       # another account under the old service stays
    _isolated_secrets.items[other] = "x"
    code, out, err = _run(["--db", str(db_path), "key", "cache", "--remove"], capsys)
    assert code == 0, err
    assert legacy not in _isolated_secrets.items and ("disconect-cli", key_id) not in _isolated_secrets.items
    assert other in _isolated_secrets.items
    # nothing cached under either service: ignored, not an error
    assert _run(["--db", str(db_path), "key", "cache", "--remove"], capsys)[0] == 0


def test_rotate_recovery_deletes_the_old_keys_item_under_the_older_service(
        tmp_path, db_path, capsys, monkeypatch, _isolated_secrets):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    old_master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)
    old_id = keys.key_id_for(old_master).hex()
    _isolated_secrets.items[("hearthbeat", old_id)] = old_master.hex()
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _rotate(db_path, capsys, monkeypatch)
    assert code == 0, err
    assert ("hearthbeat", old_id) not in _isolated_secrets.items


def test_backup_and_restore_stay_encrypted(tmp_path, db_path, capsys):
    rows = _populate(tmp_path, db_path)
    _init(db_path, capsys)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys)
    assert code == 0
    manifest = json.loads(out)
    snapshot = backup.default_backup_dir(db_path) / manifest["file"]
    assert manifest["encrypted"] is True and storage.is_encrypted_file(snapshot) is True
    assert snapshot.with_name(snapshot.name + keys.KEY_FILE_SUFFIX).exists(), "words + any key-file copy recovers"
    code, out, err = _run(["--db", str(db_path), "restore", "--yes", str(snapshot)], capsys)
    assert code == 0, err
    _unlock_env()
    conn = storage.open_read_only(db_path)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == rows
    conn.close()


def test_plaintext_snapshot_is_converted_on_restore_and_by_encrypt(tmp_path, db_path, capsys):
    rows = _populate(tmp_path, db_path)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys, env_pass=None)
    snapshot = backup.default_backup_dir(db_path) / json.loads(out)["file"]
    assert storage.is_encrypted_file(snapshot) is False
    _init(db_path, capsys)   # encrypt_store converts the existing plaintext snapshot too
    assert storage.is_encrypted_file(snapshot) is True
    assert json.loads(snapshot.with_name(snapshot.name + backup.MANIFEST_SUFFIX).read_text())["encrypted"] is True
    # a plaintext snapshot arriving from elsewhere is converted on restore
    foreign = backup.default_backup_dir(db_path) / "hearthbeat-20200101T000000Z.db"
    plain_rollback = pathlib.Path(str(snapshot) + encrypt.ROLLBACK_SUFFIX)
    assert plain_rollback.exists()
    import shutil
    shutil.copy2(plain_rollback, foreign)
    backup.refresh_manifest(foreign, None)
    code, out, err = _run(["--db", str(db_path), "--json", "restore", "--yes", str(foreign)], capsys)
    assert code == 0, err
    assert json.loads(out)["converted_to_ciphertext"] is True and storage.is_encrypted_file(db_path) is True
    _unlock_env()
    conn = storage.open_read_only(db_path)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == rows
    conn.close()


def test_recovery_words_rebuild_the_key_file(tmp_path, db_path, capsys):
    rows = _populate(tmp_path, db_path)
    _init(db_path, capsys)
    key_path = keys.key_path_for(db_path)
    master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    words = keys.words_for(master)
    key_path.unlink()
    storage._unlocked.clear(); keys.forget_session()
    os.environ["DISCONECT_RECOVERY_WORDS"] = words
    code, out, err = _run(["--db", str(db_path), "key", "recover"], capsys, env_pass=PASS2)
    assert code == 0, err
    assert "DISCONECT_RECOVERY_WORDS" not in os.environ
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _run(["--db", str(db_path), "status"], capsys, env_pass=PASS2)
    assert code == 0
    os.environ["DISCONECT_RECOVERY_WORDS"] = "abandon " * 24
    code, out, err = _run(["--db", str(db_path), "key", "recover"], capsys, env_pass=PASS2)
    assert code == cli.EXIT_LOCKED


def test_change_passphrase_keeps_db_key_and_rotate_recovery_changes_it(tmp_path, db_path, capsys, monkeypatch):
    rows = _populate(tmp_path, db_path)
    _init(db_path, capsys)
    import disconect.cli as cli_module
    monkeypatch.setattr(cli_module, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli_module, "_show_words_once", lambda master: False)
    key_path = keys.key_path_for(db_path)
    master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    storage._unlocked.clear(); keys.forget_session()
    os.environ[keys.PASSPHRASE_ENV] = PASS
    # change-passphrase asks twice: current (via env, consumed) then new; feed the new one through env again
    import disconect.cli as cli_module
    answers = iter([PASS2])
    monkeypatch.setattr(cli_module, "_ask_passphrase", lambda prompt, confirm: next(answers))
    if True:
        code, out, err = _run(["--db", str(db_path), "key", "change-passphrase"], capsys)
        assert code == 0, err
        assert keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS2) == master
        storage._unlocked.clear(); keys.forget_session()
        answers = iter([PASS2])
        code, out, err = _run(["--db", str(db_path), "--json", "key", "rotate-recovery"], capsys, env_pass=PASS2)
        assert code == 0, err
        new_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS2)
        assert new_master != master
        storage._unlocked.clear(); keys.forget_session()
        with pytest.raises(storage.DatabaseError):
            probe = storage.connect(db_path, read_only=True, master=master)
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _unlock_env(PASS2)
        conn = storage.open_read_only(db_path)
        assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == rows
        conn.close()


def test_production_kdf_params_are_recorded_and_cost_is_real(tmp_path, db_path):
    import time
    keys.set_kdf_params(None)
    key_path = tmp_path / "prod.keys.json"
    started = time.perf_counter()
    keys.create(key_path, PASS)
    elapsed = time.perf_counter() - started
    recorded = keys.read_key_file(key_path).wraps[0]["params"]
    assert recorded == keys.KDF_PARAMS and recorded["m_kib"] == 256 * 1024 and recorded["p"] == 1
    assert elapsed > 0.05, f"production Argon2id should not be this cheap ({elapsed:.3f}s)"
    lowered = json.loads(key_path.read_text())
    lowered["wraps"][0]["params"]["m_kib"] = 1024
    key_path.write_text(json.dumps(lowered))
    with pytest.raises(keys.KeyFileCorrupt):
        keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)


def test_key_file_shape_and_permissions(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    key_path = keys.key_path_for(db_path)
    assert key_path.stat().st_mode & 0o777 == 0o600 and key_path.parent.stat().st_mode & 0o777 == 0o700
    document = json.loads(key_path.read_text())
    assert set(document) == {"format", "format_version", "key_id", "created_at", "sqlcipher", "wraps"}
    wrap = document["wraps"][0]
    assert set(wrap) == {"purpose", "kdf", "params", "salt", "aead", "nonce", "ct"} and wrap["aead"] == "chacha20poly1305"
    text = key_path.read_text()
    assert "/Users" not in text and os.environ.get("USER", "\x00") not in text and str(tmp_path) not in text


def _leak_forms(secret: bytes):
    import base64
    return {secret.hex(), base64.b64encode(secret).decode(), base64.urlsafe_b64encode(secret).decode()}


def test_key_material_never_appears_in_output_or_files(tmp_path, db_path, capsys, monkeypatch):
    _populate(tmp_path, db_path)
    transcript = []
    import disconect.cli as cli_module
    monkeypatch.setattr(cli_module, "_ask_passphrase", lambda prompt, confirm: PASS2)
    monkeypatch.setattr(cli_module, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli_module, "_show_words_once", lambda master: False)
    for argv, env_pass in [
        (["key", "init"], PASS), (["key", "status"], PASS), (["status"], PASS), (["key", "cache"], PASS),
        (["backup"], None), (["encrypt"], PASS), (["status"], "wrong wrong wrong wrong"),
        (["key", "cache", "--remove"], PASS), (["status"], None), (["encrypt", "--purge-plaintext"], PASS),
        (["key", "change-passphrase"], PASS), (["--json", "key", "status"], PASS2),
    ]:
        code, out, err = _run(["--db", str(db_path)] + argv, capsys, env_pass=env_pass)
        transcript.append(f"$ {' '.join(argv)} -> {code}\n{out}{err}")
    key_path = keys.key_path_for(db_path)
    master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS2)
    words = keys.words_for(master)
    os.environ["DISCONECT_RECOVERY_WORDS"] = words
    code, out, err = _run(["--db", str(db_path), "key", "recover"], capsys, env_pass=PASS2)
    transcript.append(f"$ key recover -> {code}\n{out}{err}")
    code, out, err = _run(["--db", str(db_path), "--json", "key", "rotate-recovery"], capsys, env_pass=PASS2)
    transcript.append(f"$ key rotate-recovery -> {code}\n{out}{err}")
    assert code == 0, err
    rotated = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS2)
    secrets_ = (_leak_forms(master) | _leak_forms(keys.db_key(master)) | _leak_forms(rotated)
                | _leak_forms(keys.db_key(rotated)) | {PASS, PASS2, words, keys.words_for(rotated)})
    blob = "\n".join(transcript)
    for secret in secrets_:
        assert secret not in blob, "key material in CLI output"
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix != ".fit" and "export" not in path.parts:
            data = path.read_bytes()
            for secret in secrets_:
                assert secret.encode() not in data, f"key material in {path.name}"
                if len(secret) == 64:
                    assert bytes.fromhex(secret) not in data, f"raw key bytes in {path.name}"


def test_subprocess_wrong_passphrase_is_exit_9_without_a_traceback(tmp_path, db_path, capsys):
    """A real child process (production KDF floor applies) with a wrong passphrase: exit 9, no key, no traceback."""
    _populate(tmp_path, db_path)
    keys.set_kdf_params(None)
    _init(db_path, capsys)
    master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)
    env = {**os.environ, keys.PASSPHRASE_ENV: "nope nope nope nope", "HOME": str(tmp_path),
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    result = subprocess.run([sys.executable, "-m", "disconect.cli", "--db", str(db_path), "status"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == cli.EXIT_LOCKED and "wrong passphrase" in result.stderr and "Traceback" not in result.stderr
    for secret in _leak_forms(master) | _leak_forms(keys.db_key(master)) | {PASS}:
        assert secret not in result.stdout + result.stderr


def _rotate(db_path, capsys, monkeypatch, passphrase=PASS):
    import disconect.cli as cli_module
    monkeypatch.setattr(cli_module, "_ask_passphrase", lambda prompt, confirm: passphrase)
    monkeypatch.setattr(cli_module, "_can_show_words", lambda: True)
    monkeypatch.setattr(cli_module, "_show_words_once", lambda master: False)
    return _run(["--db", str(db_path), "--json", "key", "rotate-recovery"], capsys, env_pass=passphrase)


def test_rotation_refreshes_snapshots_and_pre_restore_copies(tmp_path, db_path, capsys, monkeypatch):
    rows = _populate(tmp_path, db_path)
    _init(db_path, capsys)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys)
    snapshot = backup.default_backup_dir(db_path) / json.loads(out)["file"]
    code, out, err = _run(["--db", str(db_path), "restore", "--yes", str(snapshot)], capsys)
    pre_restore = next(db_path.parent.glob(db_path.name + ".pre-restore-*"))
    key_path = keys.key_path_for(db_path)
    old_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _rotate(db_path, capsys, monkeypatch)
    assert code == 0, err
    result = json.loads(out)
    assert result["snapshots"] == [snapshot.name] and result["copies"] == [pre_restore.name]
    new_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    assert new_master != old_master
    for path in (db_path, snapshot, pre_restore):
        with pytest.raises(storage.DatabaseError):
            storage.connect(path, read_only=True, master=old_master).execute("SELECT count(*) FROM sqlite_master").fetchone()
    # snapshot manifest + key-file copy follow the new key, so restore still verifies
    _unlock_env()
    storage._unlocked.clear(); keys.forget_session()
    assert backup.verify_backup(snapshot, new_master)["encrypted"] is True
    copy = keys.read_key_file(snapshot.with_name(snapshot.name + keys.KEY_FILE_SUFFIX))
    assert keys.unlock_with_passphrase(copy, PASS) == new_master
    code, out, err = _run(["--db", str(db_path), "restore", "--yes", str(snapshot)], capsys)
    assert code == 0, err
    assert not key_path.with_name(key_path.name + keys.NEXT_SUFFIX).exists()


def test_rotation_aborts_cleanly_when_a_snapshot_is_under_another_key(tmp_path, db_path, capsys, monkeypatch):
    """Pre-flight: a foreign snapshot means nothing is rekeyed and the live DB still opens with the old key."""
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    key_path = keys.key_path_for(db_path)
    master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    foreign_dir = backup.default_backup_dir(db_path)
    foreign_dir.mkdir(exist_ok=True)
    foreign = foreign_dir / "hearthbeat-20200101T000000Z.db"
    other = storage.connect(foreign, read_only=False, master=b"\x01" * 32)
    other.execute("CREATE TABLE t(x)")
    other.close()
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _rotate(db_path, capsys, monkeypatch)
    assert code == cli.EXIT_FAILED and "does not open with the current key" in err and "Traceback" not in err
    assert keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS) == master
    assert not key_path.with_name(key_path.name + keys.NEXT_SUFFIX).exists()
    _unlock_env()
    storage._unlocked.clear(); keys.forget_session()
    storage.open_read_only(db_path).close()


def test_interrupted_rotation_is_finished_from_the_next_key_file(tmp_path, db_path, capsys, monkeypatch):
    """Crash after rekey but before the key file swap: the .next file opens the DB and is promoted."""
    rows = _populate(tmp_path, db_path)
    _init(db_path, capsys)
    key_path = keys.key_path_for(db_path)
    old_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    new_master = b"\x07" * 32
    keys.write_key_file(key_path.with_name(key_path.name + keys.NEXT_SUFFIX), keys._document(new_master, PASS))
    conn = storage.connect(db_path, read_only=False, master=old_master)
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.execute(f"PRAGMA rekey = \"x'{keys.db_key_hex(new_master)}'\"")
    conn.close()
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _run(["--db", str(db_path), "status"], capsys)
    assert code == 0 and "finished an interrupted key rotation" in err
    assert keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS) == new_master
    assert not key_path.with_name(key_path.name + keys.NEXT_SUFFIX).exists()


def test_purge_refuses_on_a_plaintext_store_and_spares_unconverted_snapshots(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys, env_pass=None)
    snapshot = backup.default_backup_dir(db_path) / json.loads(out)["file"]
    code, out, err = _run(["--db", str(db_path), "encrypt", "--purge-plaintext"], capsys, env_pass=None)
    assert code == cli.EXIT_FAILED and "refusing to purge" in err and snapshot.exists()


@pytest.mark.parametrize("mutate", [
    lambda d: d["wraps"][0]["params"].pop("v"),
    lambda d: d["wraps"][0]["params"].__setitem__("p", 0),
    lambda d: d["wraps"][0]["params"].__setitem__("m_kib", 64 * 1024 * 1024 * 1024),
    lambda d: d["wraps"].__setitem__(0, "not a dict"),
    lambda d: d["wraps"][0].__setitem__("salt", "AAAA"),
], ids=["missing-v", "p0", "huge-m", "wrap-not-dict", "short-salt"])
def test_malformed_key_files_are_locked_not_crashes(tmp_path, db_path, capsys, mutate):
    _populate(tmp_path, db_path)
    _init(db_path, capsys)
    key_path = keys.key_path_for(db_path)
    document = json.loads(key_path.read_text())
    mutate(document)
    key_path.write_text(json.dumps(document))
    code, out, err = _run(["--db", str(db_path), "status"], capsys)
    assert code == cli.EXIT_LOCKED and "Traceback" not in err


# ---- backup prefixes ----

def _as_old_build(snapshot):
    """Rename a snapshot and its manifest to the prefix an earlier build wrote (`hearthbeat-*`)."""
    old = snapshot.with_name("hearthbeat-" + snapshot.name.split("-", 1)[1])
    snapshot.rename(old)
    manifest = snapshot.with_name(snapshot.name + backup.MANIFEST_SUFFIX)
    manifest.rename(old.with_name(old.name + backup.MANIFEST_SUFFIX))
    return old


def test_new_snapshots_use_the_current_prefix_and_both_prefixes_are_listed_newest_first(tmp_path, db_path, capsys):
    _populate(tmp_path, db_path)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys, env_pass=None)
    first = backup.default_backup_dir(db_path) / json.loads(out)["file"]
    assert first.name.startswith("disconect-") and first.name.endswith(".db")
    old = _as_old_build(first)
    older = old.with_name("hearthbeat-20200101T000000Z.db")
    import shutil
    shutil.copy2(old, older)
    newer = old.with_name("disconect-29990101T000000Z.db")
    shutil.copy2(old, newer)
    assert [p.name for p in backup.snapshot_files(old.parent)] == [older.name, old.name, newer.name]
    listed = backup.list_backups(old.parent)
    assert len(listed) == 3
    assert listed[0]["file"] == newer.name and listed[2]["file"] == older.name, "newest stamp first across prefixes"


def test_an_old_prefix_plaintext_snapshot_is_still_encrypted_purged_and_rotated(tmp_path, db_path, capsys, monkeypatch):
    rows = _populate(tmp_path, db_path)
    code, out, err = _run(["--db", str(db_path), "--json", "backup"], capsys, env_pass=None)
    old = _as_old_build(backup.default_backup_dir(db_path) / json.loads(out)["file"])
    assert storage.is_encrypted_file(old) is False
    _init(db_path, capsys)                                   # encrypt_store: finds the old prefix
    assert storage.is_encrypted_file(old) is True, "plaintext snapshot left behind"
    rollback = pathlib.Path(str(old) + encrypt.ROLLBACK_SUFFIX)
    assert rollback.exists() and storage.is_encrypted_file(rollback) is False
    code, out, err = _run(["--db", str(db_path), "--json", "encrypt", "--purge-plaintext"], capsys)
    assert code == 0 and old.name + encrypt.ROLLBACK_SUFFIX in json.loads(out)["removed"] and not rollback.exists()
    storage._unlocked.clear(); keys.forget_session()
    key_path = keys.key_path_for(db_path)
    old_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    storage._unlocked.clear(); keys.forget_session()
    code, out, err = _rotate(db_path, capsys, monkeypatch)
    assert code == 0, err
    assert json.loads(out)["snapshots"] == [old.name], "rotate-recovery must rekey the old-prefix snapshot"
    new_master = keys.unlock_with_passphrase(keys.read_key_file(key_path), PASS)
    with pytest.raises(storage.DatabaseError):
        storage.connect(old, read_only=True, master=old_master).execute("SELECT count(*) FROM sqlite_master").fetchone()
    assert backup.verify_backup(old, new_master)["encrypted"] is True
