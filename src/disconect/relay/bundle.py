"""Bundle format and cryptography (``docs/relay-protocol.md``).

object      = version(1) ‖ salt(32) ‖ nonce(12) ‖ ChaCha20-Poly1305(k, nonce, padded, AAD)
k           = HKDF-SHA256(master, salt, info="hearthbeat/relay/v1/bundle")
AAD         = "hearthbeat/relay/v1/" ‖ account ‖ "/" ‖ object name     (binds the object to its name)
padded      = u64be(len(z)) ‖ z ‖ zeros, to the Padmé size with a 64 KiB floor
z           = zlib(JSON lines: header, then {"t":"r",...raw_records row...} and {"t":"x",...export_range...})
account     = hex(HKDF-SHA256(master, no salt, info="hearthbeat/relay-account"))   (64 hex)
object name = account ‖ "/" ‖ 32 random hex

A different master fails authentication on every object; a renamed object fails too (AAD);
a flipped byte fails; a truncated object fails. The record count and everything else that
describes the data sit inside the ciphertext.
"""

from __future__ import annotations

import base64
import json
import secrets
import struct
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_VERSION = 1
_INFO_BUNDLE = b"hearthbeat/relay/v1/bundle"
_INFO_ACCOUNT = b"hearthbeat/relay-account"
_AAD_PREFIX = b"hearthbeat/relay/v1/"
PAD_FLOOR = 64 * 1024
MAX_PLAINTEXT = 64 * 1024 * 1024
SALT_LEN, NONCE_LEN = 32, 12

#: Columns of a raw_records row that travel (``id`` never does).
RECORD_COLUMNS = ("stream", "source_key", "source_scope", "transport", "device_id", "start_utc", "end_utc",
                  "payload_kind", "payload", "payload_hash", "payload_bytes", "imported_at")


class BundleRejected(Exception):
    """The object could not be authenticated, decoded or validated. Carries a class-name-safe reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def account_for(master: bytes) -> str:
    return HKDF(hashes.SHA256(), 32, None, _INFO_ACCOUNT).derive(master).hex()


def new_name(account: str) -> str:
    return f"{account}/{secrets.token_hex(16)}"


def padme(length: int) -> int:
    """Padmé (PURBs, Nikitin et al. 2019): round ``length`` up so only O(log log n) bits of it leak."""
    length = max(length, PAD_FLOOR)
    exponent = length.bit_length() - 1
    s = exponent.bit_length()
    low_bits = exponent - s
    mask = (1 << low_bits) - 1
    return (length + mask) & ~mask


def _key(master: bytes, salt: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt, _INFO_BUNDLE).derive(master)


def _aad(account: str, name: str) -> bytes:
    return _AAD_PREFIX + account.encode() + b"/" + name.encode()


def encode_record(row: dict) -> dict:
    out = {"t": "r"}
    for column in RECORD_COLUMNS:
        value = row[column]
        out[column] = base64.b64encode(value).decode() if column == "payload" else value
    return out


def decode_record(item: dict) -> dict:
    row = {column: item.get(column) for column in RECORD_COLUMNS}
    row["payload"] = base64.b64decode(item["payload"], validate=True)
    return row


def pack(master: bytes, name: str, header: dict, records: list[dict], ranges: list[dict]) -> bytes:
    """Encrypt ``records`` (raw_records rows) and ``ranges`` (export_ranges rows) under ``master``."""
    lines = [json.dumps({"t": "h", **header}, sort_keys=True, separators=(",", ":"))]
    lines += [json.dumps(encode_record(r), sort_keys=True, separators=(",", ":")) for r in records]
    lines += [json.dumps({"t": "x", "stream": x["stream"], "from_day": x["from_day"], "to_day": x["to_day"]},
                         sort_keys=True, separators=(",", ":")) for x in ranges]
    return seal_lines(master, name, lines)


def seal_lines(master: bytes, name: str, lines: list[str]) -> bytes:
    """The sealing step on already-serialised lines (tests craft malformed bundles through it)."""
    account = account_for(master)
    if not name.startswith(account + "/"):
        raise ValueError("bundle name must live under the master's account")
    z = zlib.compress("\n".join(lines).encode("utf-8"), 9)
    body = struct.pack(">Q", len(z)) + z
    padded = body + bytes(padme(len(body)) - len(body))
    salt, nonce = secrets.token_bytes(SALT_LEN), secrets.token_bytes(NONCE_LEN)
    sealed = ChaCha20Poly1305(_key(master, salt)).encrypt(nonce, padded, _aad(account, name))
    return bytes([FORMAT_VERSION]) + salt + nonce + sealed


def unpack(master: bytes, name: str, blob: bytes) -> tuple[dict, list[dict], list[dict]]:
    """Authenticate and decode an object. Raises :class:`BundleRejected` on any defect."""
    account = account_for(master)
    if len(blob) < 1 + SALT_LEN + NONCE_LEN + 16:
        raise BundleRejected("truncated")
    if blob[0] != FORMAT_VERSION:
        raise BundleRejected("unknown_format")
    salt = blob[1:1 + SALT_LEN]
    nonce = blob[1 + SALT_LEN:1 + SALT_LEN + NONCE_LEN]
    try:
        padded = ChaCha20Poly1305(_key(master, salt)).decrypt(nonce, blob[1 + SALT_LEN + NONCE_LEN:],
                                                              _aad(account, name))
    except InvalidTag as exc:
        raise BundleRejected("authentication_failed") from exc
    try:
        (length,) = struct.unpack(">Q", padded[:8])
        if length > MAX_PLAINTEXT or 8 + length > len(padded):
            raise BundleRejected("bad_length")
        inflater = zlib.decompressobj()
        text = inflater.decompress(padded[8:8 + length], MAX_PLAINTEXT)   # a zlib bomb stops at the cap
        if inflater.unconsumed_tail:
            raise BundleRejected("too_large")
        items = [json.loads(line) for line in text.decode("utf-8").split("\n") if line]
    except BundleRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever the defect, it is a rejection, never an escape
        raise BundleRejected(type(exc).__name__) from exc
    try:
        return _validate(items)
    except BundleRejected:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BundleRejected("bad_bundle") from exc


def _validate(items: list) -> tuple[dict, list[dict], list[dict]]:
    if not items or not isinstance(items[0], dict) or items[0].get("t") != "h":
        raise BundleRejected("missing_header")
    header = {k: v for k, v in items[0].items() if k != "t"}
    if header.get("format") != FORMAT_VERSION:
        raise BundleRejected("unknown_format")
    if not isinstance(header.get("device_id"), str) or not isinstance(header.get("device_seq"), int) \
            or not (header.get("prev") is None or isinstance(header["prev"], str)) \
            or not isinstance(header.get("created_utc"), str):
        raise BundleRejected("bad_header")
    records, ranges = [], []
    for item in items[1:]:
        if not isinstance(item, dict):
            raise BundleRejected("bad_bundle")
        kind = item.get("t")
        if kind == "r":
            row = decode_record(item)
            if not all(isinstance(row[c], str) for c in ("stream", "source_key", "source_scope", "transport",
                                                           "payload_kind", "payload_hash", "imported_at")) \
                    or not isinstance(row["payload_bytes"], int):
                raise BundleRejected("bad_record")
            records.append(row)
        elif kind == "x":
            rng = {k: item[k] for k in ("stream", "from_day", "to_day")}
            if not all(isinstance(v, str) for v in rng.values()):
                raise BundleRejected("bad_range")
            ranges.append(rng)
        else:
            raise BundleRejected("bad_bundle")
    return header, records, ranges
