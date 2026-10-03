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
