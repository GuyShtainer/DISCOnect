"""Frozen wire constants (docs/kb/24-wire-constants.md): known-answer vectors and verbatim strings.

These strings are inputs to key derivation, AEAD associated data and file formats. They stay
``hearthbeat`` forever, whatever the product is called: changing one makes every existing store,
key file and relay bundle unreadable. The same vectors are asserted by the Rust core
(``keys.rs`` and ``relay/bundle.rs`` test modules). Synthetic data and the committed test key only.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from disconect.relay import bundle
from disconect.storage import keys

CORE = pathlib.Path(__file__).resolve().parents[2] / "disconect-core"
FIXTURES = CORE / "tests" / "fixtures"
PASSPHRASE = "core-test-passphrase-2026"

KEY_ID_HEX = "b4714bc6c191877abbd67d79274a4fd2"
DB_KEY_HEX = "5a0db69ac2f8cc262e5bfb28d4deb8b9ba8637307cea5de707d890654573b162"
ACCOUNT_HEX = "053e1211c6660255d9168e529d6a11ff000433dab305659da7e9aa8dfa92021e"

#: (frozen string, python file, rust file) relative to the two cores' source trees.
FROZEN = [
    ('hearthbeat/db', "storage/keys.py", "src/keys.rs"),
    ('hearthbeat/key-id', "storage/keys.py", "src/keys.rs"),
    ('hearthbeat/keys/v1/', "storage/keys.py", "src/keys.rs"),
    ('hearthbeat-keys', "storage/keys.py", "src/keys.rs"),
    ('hearthbeat/relay/v1/bundle', "relay/bundle.py", "src/relay/bundle.rs"),
    ('hearthbeat/relay-account', "relay/bundle.py", "src/relay/bundle.rs"),
    ('hearthbeat/relay/v1/', "relay/bundle.py", "src/relay/bundle.rs"),
]
PYTHON_SRC = pathlib.Path(keys.__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def master() -> bytes:
    key_file = keys.read_key_file(FIXTURES / "test.keys.json")
    return keys.unlock_with_passphrase(key_file, PASSPHRASE)


@pytest.mark.parametrize("label, py_file, rust_file", FROZEN)
def test_frozen_label_is_in_both_cores(label, py_file, rust_file):
    assert f'"{label}' in (PYTHON_SRC / py_file).read_text(), f"{label!r} missing from {py_file}"
    assert f'"{label}' in (CORE / rust_file).read_text(), f"{label!r} missing from {rust_file}"


def test_derivations_use_the_frozen_labels_verbatim():
    assert bundle._INFO_BUNDLE == b"hearthbeat/relay/v1/bundle"
    assert bundle._INFO_ACCOUNT == b"hearthbeat/relay-account"
    assert bundle._AAD_PREFIX == b"hearthbeat/relay/v1/"
    assert keys._aad("passphrase", bytes.fromhex(KEY_ID_HEX)) == \
        b"hearthbeat/keys/v1/passphrase/" + KEY_ID_HEX.encode()


def test_key_file_format_string_is_frozen():
    assert json.loads((FIXTURES / "test.keys.json").read_text())["format"] == "hearthbeat-keys"
    document = keys._document(bytes(range(32)), "a passphrase of enough length")
    assert document["format"] == "hearthbeat-keys"


def test_known_answer_key_id_and_db_key(master):
    assert keys.key_id_for(master).hex() == KEY_ID_HEX
    assert keys.db_key_hex(master) == DB_KEY_HEX


def test_known_answer_relay_bundle(master):
    meta = json.loads((FIXTURES / "relay" / "known-answer.json").read_text())
    blob = (FIXTURES / "relay" / "known-answer.bundle").read_bytes()
    assert hashlib.sha256(blob).hexdigest() == meta["bundle_sha256"]
    assert bundle.account_for(master) == ACCOUNT_HEX == meta["account"]
    header, records, ranges = bundle.unpack(master, meta["name"], blob)
    assert (len(records), len(ranges)) == (3, 1) == (meta["records"], meta["ranges"])
    assert header["device_id"] == "synthetic-device-a" and header["device_seq"] == 1
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["payload"])
    assert digest.hexdigest() == meta["payload_sha256"]


# ---- the LAN relay token (Bet 12 slice B): a Rust-only label, held by an independent computation ----

LAN_LABEL = "disconect/lan/v1/token"
LAN_MASTER = bytes(range(1, 33))
LAN_TOKEN_KEY = "fb82fb73a463f5df7439df5e66c64e3731a8d8c7e7e6b07ea5715b54c7822350"
LAN_PUT = "v1.1700000000.2011a51d77be5327a5f5018eb22c266c5f137a1a3c4586f1f2617c8c51f6d060"
LAN_GET = "v1.1700000000.71e67dc1ec1cefcf4692eabba5cbd8f4940fa4e97f7c4a18479fc7d15b194d62"


def _lan_token_key(master: bytes) -> bytes:
    """``HKDF-SHA256(master, salt = the account's 64 hex characters as ASCII, info = the label)``, by hand."""
    import hmac

    prk = hmac.new(bundle.account_for(master).encode(), master, hashlib.sha256).digest()
    return hmac.new(prk, LAN_LABEL.encode() + b"\x01", hashlib.sha256).digest()


def _lan_header(key: bytes, method: str, path: str, stamp: int, body: bytes) -> str:
    import hmac

    message = f"{method}\n{path}\n{stamp}\n{hashlib.sha256(body).hexdigest()}".encode()
    return f"v1.{stamp}.{hmac.new(key, message, hashlib.sha256).hexdigest()}"


def test_the_lan_token_label_and_vectors_are_held_by_both_the_rust_source_and_kb24():
    kb = (CORE.parents[1] / "docs" / "kb" / "24-wire-constants.md").read_text()
    rust = (CORE / "src" / "relay" / "lan.rs").read_text()
    for text in (kb, rust):
        assert LAN_LABEL in text
        for vector in (LAN_TOKEN_KEY, LAN_PUT, LAN_GET):
            assert vector in text
    assert f'b"{LAN_LABEL}"' in rust, "the Rust constant is the label verbatim"


def test_the_lan_vectors_follow_from_the_stated_construction():
    key = _lan_token_key(LAN_MASTER)
    assert key.hex() == LAN_TOKEN_KEY
    account = bundle.account_for(LAN_MASTER)
    name = f"{account}/{'ab' * 16}"
    assert _lan_header(key, "PUT", f"/v1/objects/{name}", 1700000000, b"sealed bytes") == LAN_PUT
    assert _lan_header(key, "GET", "/v1/objects", 1700000000, b"") == LAN_GET
