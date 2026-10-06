"""Known-answer and negative vectors of the pairing oracle (docs/kb/24-wire-constants.md, ADR 0011).

Inputs are the kb section's; every output is computed by ``disconect.pair`` (written from the ADR text)
and compared with the value the Rust core froze. Synthetic keys and the committed test key file only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from disconect import pair

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "disconect-core" / "tests" / "fixtures" / "test.keys.json"
OFFERER_PRIV = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
JOINER_PRIV = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
S = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
ID = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
ID_ZERO_SAS = bytes.fromhex("202122232425262728292a2b2c2d2e2f")
EXP = 1700000900
URL = "http://127.0.0.1:8321"
MASTER = bytes(range(1, 33))

OFFERER_PUB = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
JOINER_PUB = "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
DH = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
OFFER_TEXT = (
    "disconect-pair:v1.eyJ2IjoxLCJwdWIiOiI4NTIwZjAwOTg5MzBhNzU0NzQ4YjdkZGNiNDNlZjc1YTBkYmYzYTBkMjYzODFhZjRlYmE0YTk4ZWFh"
    "OWI0ZTZhIiwicyI6IjAwMDEwMjAzMDQwNTA2MDcwODA5MGEwYjBjMGQwZTBmIiwiaWQiOiIxMDExMTIxMzE0MTUxNjE3MTgxOTFhMWIxYzFkMWUx"
    "ZiIsImV4cCI6MTcwMDAwMDkwMCwidXJsIjoiaHR0cDovLzEyNy4wLjAuMTo4MzIxIn0"
)
TRANSCRIPT = (
    "646973636f6e6563742f706169722f7631008520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f101112131415161718191a1b1c1d1e1f000000006553f484"
)
K = "ced06a2f9929a397d15f1eed614939527cbf65a1b87ead87d92bf714d083a96d"
K_CONFIRM = "43bc5e1b2ddf975af5927e7391ce2d818820340955e5ca9149502b5114b4e288"
K_OFFERER = "280ec7743588865a0236e5c91676b85e97f32f5c729ac4588c018c718681ee59"
K_SAS = "fb7bf7c18b7584b942d3a33e4a792be0f0ae57e64ab5bf06c255508f381ee41f"
K_PAYLOAD = "fed979831974c36409bc0eb9bffcd486aa43cb5c8e0c39fd970311d9a2a14dc5"
JOINER_TAG = "c01f965e6760e8a34c78782384e65b8a03d759347479c0872c161e39bc10eb50"
OFFERER_TAG = "19052201486a1adea5232cae76e49ae810cabfd4910646a1d5545a29431bfffc"
FIXTURE_SHA = "cd538c0cddf607a878a8424c3dcbbd58f9e2ba54529b550f18f813f446b01190"
SEALED_SHA = "6c682055dea98d3a4c341f35f2022d54bb7b7d9488ffdc4d97efacb92f8ba8ba"
LOW_ORDER = bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800")


def _public(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _session(id_bytes: bytes = ID):
    shared = pair.dh(OFFERER_PRIV, bytes.fromhex(JOINER_PUB))
    wire = pair.transcript(bytes.fromhex(OFFERER_PUB), bytes.fromhex(JOINER_PUB), id_bytes, EXP)
    return wire, pair.derive_keys(shared, S, wire)


def test_public_keys_are_the_rfc_7748_values():
    assert _public(OFFERER_PRIV).hex() == OFFERER_PUB
    assert _public(JOINER_PRIV).hex() == JOINER_PUB


def test_dh_agrees_both_directions():
    assert pair.dh(OFFERER_PRIV, bytes.fromhex(JOINER_PUB)).hex() == DH
    assert pair.dh(JOINER_PRIV, bytes.fromhex(OFFERER_PUB)).hex() == DH


def test_offer_text_and_roundtrip():
    text = pair.encode_offer(bytes.fromhex(OFFERER_PUB), S, ID, EXP, URL)
    assert text == OFFER_TEXT
    assert "=" not in text
    offer = pair.parse_offer(text)
    assert (offer.pub.hex(), offer.s, offer.id, offer.exp, offer.url) == (OFFERER_PUB, S, ID, EXP, URL)


def test_transcript_and_key_schedule():
    wire, keys = _session()
    assert wire.hex() == TRANSCRIPT and len(wire) == 106
    assert keys.k.hex() == K
    assert keys.confirm.hex() == K_CONFIRM
    assert keys.offerer.hex() == K_OFFERER
    assert keys.sas.hex() == K_SAS
    assert keys.payload.hex() == K_PAYLOAD


def test_tags_and_sas():
    _, keys = _session()
    assert pair.joiner_tag(keys).hex() == JOINER_TAG
    assert pair.offerer_tag(keys).hex() == OFFERER_TAG
    assert pair.sas(keys) == 686008
    assert pair.sas_text(keys) == "686008"


def test_leading_zero_sas_case():
    _, keys = _session(ID_ZERO_SAS)
    assert pair.sas(keys) == 6182
    assert pair.sas_text(keys) == "006182"


def test_sealed_payload_vector_and_roundtrip():
    key_file = FIXTURE.read_bytes()
    assert hashlib.sha256(key_file).hexdigest() == FIXTURE_SHA and len(key_file) == 635
    wire, keys = _session()
    sealed = pair.seal_payload(keys, wire, MASTER + key_file)
    assert len(sealed) == 683
    assert hashlib.sha256(sealed).hexdigest() == SEALED_SHA
    assert pair.open_payload(keys, wire, sealed) == MASTER + key_file


def _offer_text(document_json: bytes, pad: bool = False) -> str:
    body = base64.urlsafe_b64encode(document_json)
    return pair.OFFER_PREFIX + (body if pad else body.rstrip(b"=")).decode()


def _doc(**changes) -> bytes:
    document = {"v": 1, "pub": OFFERER_PUB, "s": S.hex(), "id": ID.hex(), "exp": EXP, "url": URL}
    document.update(changes)
    return json.dumps(document, separators=(",", ":")).encode()


def _bad_offers() -> dict[str, str]:
    good = _doc()
    return {
        "uppercase hex": _offer_text(_doc(pub=OFFERER_PUB.upper())),
        "duplicate key": _offer_text(good[:-1] + b',"v":1}'),
        "unknown key": _offer_text(good[:-1] + b',"x":1}'),
        "v=2": _offer_text(_doc(v=2)),
        "v=true": _offer_text(_doc(v=True)),
        "padded base64": _offer_text(_doc(url="http://127.0.0.1:832"), pad=True),
        "port 0": _offer_text(_doc(url="http://127.0.0.1:0")),
        "port too large": _offer_text(_doc(url="http://127.0.0.1:65536")),
        "no port": _offer_text(_doc(url="http://127.0.0.1")),
        "https": _offer_text(_doc(url="https://127.0.0.1:8321")),
        "trailing slash": _offer_text(_doc(url=URL + "/")),
        "float exp": _offer_text(_doc(exp=1700000900.0)),
        "negative exp": _offer_text(_doc(exp=-1)),
        "bool exp": _offer_text(_doc(exp=True)),
        "wrong hex length": _offer_text(_doc(s="00" * 15)),
        "wrong prefix": "disconect-pair:v2." + OFFER_TEXT[len(pair.OFFER_PREFIX):],
        "no prefix": OFFER_TEXT[len(pair.OFFER_PREFIX):],
        "not base64": pair.OFFER_PREFIX + "!!!",
        "not json": _offer_text(b"nope"),
        "missing key": _offer_text(json.dumps({"v": 1}).encode()),
    }


_URL_VECTORS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "pair-offer-urls.json").read_text())


@pytest.mark.parametrize("url", _URL_VECTORS["accepted"])
def test_offer_url_grammar_accepts_the_shared_vectors(url):
    """kb/24 § offer URL grammar: the vectors every parser of the offer text shares (both cores, the phone)."""
    assert pair.parse_offer(_offer_text(_doc(url=url))).url == url


@pytest.mark.parametrize("name", list(_URL_VECTORS["refused"]))
def test_offer_url_grammar_refuses_the_shared_vectors(name):
    url = _URL_VECTORS["refused"][name]
    text = _offer_text(_doc(url=url))
    with pytest.raises(pair.PairError) as caught:
        pair.parse_offer(text)
    assert url not in str(caught.value) and text[len(pair.OFFER_PREFIX):] not in str(caught.value)


def test_non_canonical_base64url_trailing_bits_are_refused():
    """``urlsafe_b64decode`` accepts non-zero bits past the last byte; the offer body must re-encode to itself."""
    body = OFFER_TEXT[len(pair.OFFER_PREFIX):]
    assert len(body) % 4, "the frozen vector ends in a partial group"
    flipped = body[:-1] + ("C" if body[-1] == "B" else "B")  # index 1 or 2: non-zero low bits in a partial group
    with pytest.raises(pair.PairError, match="canonical"):
        pair.parse_offer(pair.OFFER_PREFIX + flipped)
    assert len(_URL_VECTORS["accepted"]) >= 12 and len(_URL_VECTORS["refused"]) >= 44


def test_the_padded_case_is_really_padded():
    assert _bad_offers()["padded base64"].endswith("=")


@pytest.mark.parametrize("name", list(_bad_offers()))
def test_negative_offer_is_refused_without_echoing_it(name):
    text = _bad_offers()[name]
    with pytest.raises(pair.PairError) as caught:
        pair.parse_offer(text)
    assert text[len(pair.OFFER_PREFIX):] not in str(caught.value)


def test_key_order_is_frozen_and_good_offer_still_parses():
    assert pair.parse_offer(_offer_text(_doc())).exp == EXP
    assert base64.urlsafe_b64decode(OFFER_TEXT[len(pair.OFFER_PREFIX):] + "=").startswith(b'{"v":1,"pub":')


@pytest.mark.parametrize("peer", [bytes(32), LOW_ORDER], ids=["all-zero", "order-8"])
def test_low_order_public_keys_are_refused(peer):
    with pytest.raises(pair.PairError):
        pair.dh(OFFERER_PRIV, peer)


def test_payload_negatives():
    wire, keys = _session()
    sealed = pair.seal_payload(keys, wire, MASTER + b"keyfile")
    flipped = bytes([sealed[0] ^ 1]) + sealed[1:]
    _, other = _session(ID_ZERO_SAS)
    with pytest.raises(pair.PairError):
        pair.open_payload(keys, wire, flipped)
    with pytest.raises(pair.PairError):
        pair.open_payload(keys, wire + b"\x00", sealed)
    with pytest.raises(pair.PairError):
        pair.open_payload(other, wire, sealed)
    with pytest.raises(pair.PairError):
        pair.open_payload(keys, wire, b"")
    with pytest.raises(pair.PairError):
        pair.seal_payload(keys, wire, MASTER[:31])


def test_a_short_plaintext_sealed_elsewhere_is_refused_on_open():
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    wire, keys = _session()
    sealed = ChaCha20Poly1305(keys.payload).encrypt(bytes(12), b"short", wire)
    with pytest.raises(pair.PairError):
        pair.open_payload(keys, wire, sealed)
