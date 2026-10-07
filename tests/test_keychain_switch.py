"""The Python core's keychain never touches the login keychain under a test backend (ios-toolkit finding, 2026-10-07):
``DISCONECT_KEYCHAIN=fail`` is honoured like the Rust core's switch, and the macOS ``security`` item probe runs only
when the live macOS backend is the active one."""

from __future__ import annotations

import secrets
import subprocess

import keyring
import pytest
from keyring.backends import fail, macOS

from disconect.storage import keys


@pytest.fixture
def no_spawn(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the keychain probe spawned a process under a test backend")
    monkeypatch.setattr(subprocess, "run", refuse)


def test_a_memory_backend_never_probes_the_login_keychain(_isolated_secrets, no_spawn):
    key_id = secrets.token_bytes(16)
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_ABSENT
    keys.keychain_set(key_id, secrets.token_bytes(32))
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_CACHED


def test_the_fail_switch_is_the_rust_twin(_isolated_secrets, no_spawn, monkeypatch):
    monkeypatch.setenv(keys.KEYCHAIN_BACKEND_ENV, "fail")
    key_id = secrets.token_bytes(16)
    assert keys.keychain_get(key_id) is None
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_ABSENT
    with pytest.raises(keyring.errors.NoKeyringError):
        keys.keychain_set(key_id, secrets.token_bytes(32))
    with pytest.raises(keyring.errors.NoKeyringError):  # the twin of Rust's NoBackend on delete
        keys.keychain_delete(key_id)
    assert isinstance(keys._keychain().get_keyring(), fail.Keyring)
    assert not isinstance(keyring.get_keyring(), fail.Keyring), "the switch never rewrites keyring's global backend"
    assert keys.keychain_delete_legacy(key_id) is False  # documented: a keychain failure is ignored
    assert keys.keychain_after_rotate(secrets.token_bytes(32), secrets.token_bytes(32)) is None  # nothing cached: nothing to say
    assert keys.is_keychain_error(keyring.errors.NoKeyringError("x")) and not keys.is_keychain_error(ValueError("x"))


def test_a_keychain_that_refuses_the_write_after_a_rotate_is_a_note_not_an_error(_isolated_secrets, no_spawn, monkeypatch):
    """The words were shown; a keychain that reads but refuses the write must not raise past the rekey."""
    old_master, new_master = secrets.token_bytes(32), secrets.token_bytes(32)
    keys.keychain_set(keys.key_id_for(old_master), old_master)

    def refuse(*_args, **_kwargs):
        raise keyring.errors.KeyringLocked("locked")
    monkeypatch.setattr(keys, "keychain_set", refuse)
    note = keys.keychain_after_rotate(old_master, new_master)
    assert note and note.startswith("keychain not updated (KeyringLocked")
    with pytest.raises(ValueError):  # anything that is not a keychain failure still propagates
        monkeypatch.setattr(keys, "keychain_get", lambda _key_id: (_ for _ in ()).throw(ValueError("x")))
        keys.keychain_after_rotate(old_master, new_master)


def test_the_switch_is_not_sticky(_isolated_secrets, no_spawn, monkeypatch):
    key_id = secrets.token_bytes(16)
    monkeypatch.setenv(keys.KEYCHAIN_BACKEND_ENV, "fail")
    assert keys.keychain_get(key_id) is None
    monkeypatch.delenv(keys.KEYCHAIN_BACKEND_ENV)
    keys.keychain_set(key_id, secrets.token_bytes(32))  # the memory backend again, no NoKeyringError
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_CACHED


def test_a_macos_backend_on_another_keychain_file_is_not_the_login_keychain(_isolated_secrets, no_spawn, monkeypatch):
    monkeypatch.setattr(keys.sys, "platform", "darwin")
    backend = macOS.Keyring.__new__(macOS.Keyring)
    backend.keychain = "/tmp/scratch.keychain-db"  # KEYCHAIN_PATH in keyring's macOS backend
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keys, "keychain_get", lambda _key_id: None)
    assert keys.keychain_state(secrets.token_bytes(16)) == keys.KEYCHAIN_ABSENT


def test_the_probe_runs_only_for_the_live_macos_backend(_isolated_secrets, monkeypatch):
    """The gate, not the login keychain: the backend is reported as macOS and the probe is a stub."""
    if not hasattr(macOS, "Keyring"):
        pytest.skip("no macOS backend class on this platform")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(keys.sys, "platform", "darwin")
    backend = macOS.Keyring.__new__(macOS.Keyring)
    backend.keychain = None  # the login keychain, not a KEYCHAIN_PATH file
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keys, "keychain_get", lambda _key_id: None)
    key_id = secrets.token_bytes(16)
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_STALE
    assert calls and calls[0][:3] == ["/usr/bin/security", "find-generic-password", "-s"]
    assert "-w" not in calls[0] and "-g" not in calls[0], "attributes only: never the secret, never the access dialog"
