"""Pure functions of the pairing protocol (ADR 0011 as amended: protocol v2).

This module is the independent oracle of the Rust implementation (``disconect-core``'s ``pair``): it was
written from the text of ADR 0011 and the pairing design notes only, so a shared misreading cannot hide, and its
known-answer vectors are pinned by ``tests/test_wire_constants.py`` and the Rust twin's tests. The Python core runs no pairing
(ADR 0003: what is wire-pure is twinned, what is a server is not), so nothing here touches a network,
a file or a clock; every function maps bytes to bytes. Offer text is a QR-photo-grade secret: no error
message ever contains it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

OFFER_PREFIX = "disconect-pair:v2."
LABEL = b"disconect/pair/v2"
LABEL_CONFIRM = b"disconect/pair/v2/confirm"
LABEL_OFFERER = b"disconect/pair/v2/offerer"
LABEL_SAS = b"disconect/pair/v2/sas"
LABEL_PAYLOAD = b"disconect/pair/v2/payload"
#: The commitment's domain: ``c = SHA-256(LABEL_COMMIT || N_o)``.
LABEL_COMMIT = b"disconect/pair/v2/commit"
MSG_CONFIRM = b"confirm"
MSG_OFFERER = b"offerer"
MSG_SAS = b"sas"
EXPIRY_S = 900
_NONCE = bytes(12)
_OFFER_KEYS = ("v", "pub", "c", "s", "id", "exp", "url")
_HEX_LENGTHS = {"pub": 32, "c": 32, "s": 16, "id": 16}
#: The offerer's ``202`` body: ``N_o (32) || HMAC(K_offerer, "offerer" || N_o) (32)``.
OFFERER_REPLY_LEN = 64
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_HEX_CHARS = frozenset("0123456789abcdef")


class PairError(ValueError):
    """A pairing input was refused. The message never contains the offer text."""


@dataclass(frozen=True)
class Offer:
    """A parsed offer: ``pub``, ``c``, ``s`` and ``id`` as raw bytes, ``exp`` in unix seconds, ``url`` verbatim."""

    pub: bytes
    c: bytes
    s: bytes
    id: bytes
    exp: int
    url: str


@dataclass(frozen=True)
class Keys:
    """``K`` and the four subkeys derived from it (each 32 bytes)."""

    k: bytes
    confirm: bytes
    offerer: bytes
    sas: bytes
    payload: bytes


def _check_len(name: str, value: bytes, length: int) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != length:
        raise PairError(f"{name} must be exactly {length} bytes")


def _check_exp(exp: int) -> None:
    if isinstance(exp, bool) or not isinstance(exp, int) or not 0 <= exp < 2**64:
        raise PairError("exp must be a non-negative 64-bit integer")


def commit(n_o: bytes) -> bytes:
    """``c = SHA-256("disconect/pair/v2/commit" || N_o)``: the offerer's commitment to its 32-byte SAS nonce."""
    _check_len("N_o", n_o, 32)
    return hashlib.sha256(LABEL_COMMIT + bytes(n_o)).digest()


def encode_offer(pub: bytes, c: bytes, s: bytes, id: bytes, exp: int, url: str) -> str:  # noqa: A002 - wire field name
    """Return ``disconect-pair:v2.<base64url, no padding, of the compact JSON>`` in the key order v,pub,c,s,id,exp,url."""
    _check_len("pub", pub, 32)
    _check_len("c", c, 32)
    _check_len("s", s, 16)
    _check_len("id", id, 16)
    _check_exp(exp)
    document = {"v": 2, "pub": pub.hex(), "c": c.hex(), "s": s.hex(), "id": id.hex(), "exp": exp, "url": url}
    compact = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return OFFER_PREFIX + base64.urlsafe_b64encode(compact).rstrip(b"=").decode("ascii")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise PairError("offer has a duplicate key")
    return dict(pairs)


def _decode_body(text: str) -> bytes:
    if not isinstance(text, str) or not text.startswith(OFFER_PREFIX):
        raise PairError("offer has the wrong prefix")
    body = text[len(OFFER_PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", body):
        raise PairError("offer body is not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        raise PairError("offer body is not valid base64url") from None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != body:
        raise PairError("offer body is not canonical base64url (non-zero trailing bits)")
    return raw


def _hex_field(document: dict[str, object], name: str) -> bytes:
    value = document[name]
    length = _HEX_LENGTHS[name]
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{2 * length}}}", value):
        raise PairError(f"{name} must be {2 * length} lowercase hex characters")
    return bytes.fromhex(value)


def _ipv4_ok(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(
        part.isdigit() and (part == "0" or not part.startswith("0")) and int(part) <= 255 for part in parts)


def _ipv6_ok(text: str) -> bool:
    if ":::" in text:
        return False
    halves = text.split("::")
    if len(halves) > 2:
        return False
    groups = [group for half in halves if half for group in half.split(":")]
    if not all(1 <= len(group) <= 4 and set(group) <= _HEX_CHARS for group in groups):
        return False
    return len(groups) <= 7 if len(halves) == 2 else len(groups) == 8


def _label_ok(label: str) -> bool:
    return 1 <= len(label) <= 63 and set(label) <= _LABEL_CHARS and not label.startswith("-") and not label.endswith("-")


def _host_ok(host: str) -> bool:
    """Offer URL grammar: an IPv4 address, a bracketed IPv6 address or a lowercase hostname."""
    if host.startswith("["):
        return host.endswith("]") and _ipv6_ok(host[1:-1])
    if host and set(host) <= set("0123456789."):
        return _ipv4_ok(host)
    return 1 <= len(host) <= 253 and all(_label_ok(label) for label in host.split("."))


def _port_ok(port: str) -> bool:
    return port.isdigit() and port.isascii() and not port.startswith("0") and 1 <= len(port) <= 5 and int(port) <= 65535


def _check_url(url: object) -> str:
    """The offer URL is exactly ``http://`` host ``:`` port, nothing else."""
    rest = url[len("http://"):] if isinstance(url, str) and url.startswith("http://") else None
    host, _, port = rest.rpartition(":") if rest is not None else ("", "", "")
    if rest is None or not _port_ok(port) or not _host_ok(host):
        raise PairError("url must be http://host:port — IPv4, bracketed IPv6 or lowercase hostname, "
                        "an explicit port 1..65535 without a leading zero, no path or trailing slash")
    return url


def parse_offer(text: str) -> Offer:
    """Strictly parse offer text; raise :class:`PairError` (never echoing ``text``) on any deviation."""
    raw = _decode_body(text)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PairError("offer body is not valid JSON") from None
    if not isinstance(document, dict) or set(document) != set(_OFFER_KEYS):
        raise PairError("offer must have exactly the keys v, pub, c, s, id, exp, url")
    version = document["v"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        raise PairError("offer version must be the integer 2")
    exp = document["exp"]
    _check_exp(exp)
    return Offer(
        pub=_hex_field(document, "pub"),
        c=_hex_field(document, "c"),
        s=_hex_field(document, "s"),
        id=_hex_field(document, "id"),
        exp=exp,
        url=_check_url(document["url"]),
    )


def transcript(offer_pub: bytes, joiner_pub: bytes, id: bytes, exp: int, c: bytes) -> bytes:  # noqa: A002
    """``"disconect/pair/v2" || 0x00 || offer_pub || joiner_pub || id || exp (8 bytes big-endian) || c`` (138 bytes).

    ``c`` is the last field of the transcript although it follows ``pub`` in the offer JSON.
    """
    _check_len("offer_pub", offer_pub, 32)
    _check_len("joiner_pub", joiner_pub, 32)
    _check_len("id", id, 16)
    _check_exp(exp)
    _check_len("c", c, 32)
    return LABEL + b"\x00" + bytes(offer_pub) + bytes(joiner_pub) + bytes(id) + exp.to_bytes(8, "big") + bytes(c)


def _expand(prk: bytes, label: bytes) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=label).derive(prk)


def derive_keys(dh_secret: bytes, s: bytes, transcript_bytes: bytes) -> Keys:
    """``K = HKDF-SHA256(salt=s, ikm=dh, info=transcript, L=32)``; subkeys by HKDF-Expand from ``K`` (no re-extract)."""
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=s, info=transcript_bytes).derive(dh_secret)
    return Keys(
        k=k,
        confirm=_expand(k, LABEL_CONFIRM),
        offerer=_expand(k, LABEL_OFFERER),
        sas=_expand(k, LABEL_SAS),
        payload=_expand(k, LABEL_PAYLOAD),
    )


def joiner_tag(keys: Keys) -> bytes:
    """``HMAC-SHA256(K_confirm, "confirm")``."""
    return hmac.new(keys.confirm, MSG_CONFIRM, hashlib.sha256).digest()


def offerer_tag(keys: Keys, n_o: bytes) -> bytes:
    """``HMAC-SHA256(K_offerer, "offerer" || N_o)`` (a 39-byte message, no separator)."""
    _check_len("N_o", n_o, 32)
    return hmac.new(keys.offerer, MSG_OFFERER + bytes(n_o), hashlib.sha256).digest()


def offerer_reply(keys: Keys, n_o: bytes) -> bytes:
    """The offerer's ``202`` body: ``N_o || offerer_tag`` (64 bytes)."""
    return bytes(n_o) + offerer_tag(keys, n_o)


def open_offerer_reply(keys: Keys, c: bytes, body: bytes) -> bytes:
    """Check the offerer's ``202`` body and return ``N_o``: exactly 64 bytes, then the reveal matches the
    offer's commitment ``c``, then the tag verifies — in that order, each in constant time. The joiner shows
    no code unless all three pass. The commitment check is the critical one and is never dropped because the
    tag also covers ``N_o``."""
    _check_len("c", c, 32)
    if not isinstance(body, (bytes, bytearray)) or len(body) != OFFERER_REPLY_LEN:
        raise PairError("the offerer did not confirm")
    n_o, tag = bytes(body[:32]), bytes(body[32:])
    if not hmac.compare_digest(commit(n_o), bytes(c)):
        raise PairError("the offerer did not confirm")
    if not hmac.compare_digest(offerer_tag(keys, n_o), tag):
        raise PairError("the offerer did not confirm")
    return n_o


def sas(keys: Keys, n_o: bytes) -> int:
    """Big-endian u32 of the first 4 bytes of ``HMAC-SHA256(K_sas, "sas" || N_o)`` (35 bytes), modulo 10**6."""
    _check_len("N_o", n_o, 32)
    digest = hmac.new(keys.sas, MSG_SAS + bytes(n_o), hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big") % 10**6


def sas_text(keys: Keys, n_o: bytes) -> str:
    """The SAS as six digits, zero-padded."""
    return f"{sas(keys, n_o):06d}"


def seal_payload(keys: Keys, transcript_bytes: bytes, plaintext: bytes) -> bytes:
    """ChaCha20-Poly1305 under ``K_payload``, nonce 12 zero bytes, AAD the transcript. Seal once per key."""
    if len(plaintext) < 32:
        raise PairError("payload plaintext must hold at least the 32-byte master")
    return ChaCha20Poly1305(keys.payload).encrypt(_NONCE, plaintext, transcript_bytes)


def open_payload(keys: Keys, transcript_bytes: bytes, sealed: bytes) -> bytes:
    """Open a sealed payload; refuse a bad tag, wrong AAD or key, and a plaintext shorter than 32 bytes."""
    try:
        plaintext = ChaCha20Poly1305(keys.payload).decrypt(_NONCE, sealed, transcript_bytes)
    except InvalidTag:
        raise PairError("payload did not authenticate") from None
    if len(plaintext) < 32:
        raise PairError("payload plaintext is shorter than the 32-byte master")
    return plaintext


def dh(private_bytes: bytes, peer_pub_bytes: bytes) -> bytes:
    """X25519; a non-contributory result (low-order or all-zero peer key) raises :class:`PairError`."""
    _check_len("private key", private_bytes, 32)
    _check_len("peer public key", peer_pub_bytes, 32)
    private = X25519PrivateKey.from_private_bytes(bytes(private_bytes))
    try:
        return private.exchange(X25519PublicKey.from_public_bytes(bytes(peer_pub_bytes)))
    except ValueError:
        raise PairError("non-contributory key exchange refused") from None
