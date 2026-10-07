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
    assert isinstance(keyring.get_keyring(), fail.Keyring)


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
    monkeypatch.setattr(keyring, "get_keyring", lambda: macOS.Keyring.__new__(macOS.Keyring))
    monkeypatch.setattr(keys, "keychain_get", lambda _key_id: None)
    key_id = secrets.token_bytes(16)
    assert keys.keychain_state(key_id) == keys.KEYCHAIN_STALE
    assert calls and calls[0][:3] == ["/usr/bin/security", "find-generic-password", "-s"] and "-w" not in calls[0]
