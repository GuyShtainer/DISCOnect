"""Shared fixtures. All data is synthetic; nothing here came from a real watch."""

from __future__ import annotations

import datetime
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # for fit_builder

UTC = datetime.timezone.utc


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "hearthbeat.db"


@pytest.fixture
def t0() -> datetime.datetime:
    """A fixed moment: 06:00 UTC = 09:00 on a watch running UTC+3."""
    return datetime.datetime(2025, 6, 15, 6, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Every test: cheap Argon2id, an in-memory keychain, no real HOME, no env passphrase, no unlock cache."""
    import keyring
    import keyring.backend
    from disconect import storage
    from disconect.storage import keys

    class _MemoryKeyring(keyring.backend.KeyringBackend):
        priority = 1

        def __init__(self):
            self.items = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.items:
                raise keyring.errors.PasswordDeleteError("absent")
            del self.items[(service, username)]

    memory = _MemoryKeyring()
    keyring.set_keyring(memory)
    keys.set_kdf_params({"m_kib": 8 * 1024, "t": 1, "p": 1, "v": 0x13})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(keys.PASSPHRASE_ENV, raising=False)
    monkeypatch.delenv(keys.KEYS_ENV, raising=False)
    storage._unlocked.clear()
    keys.forget_session()
    yield memory
    storage._unlocked.clear()
    keys.forget_session()
    keys.set_kdf_params(None)


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to connect or resolve a name fails the test: the core must work with no network at all."""
    import socket

    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
