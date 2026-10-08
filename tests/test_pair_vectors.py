"""Known-answer and negative vectors of the pairing oracle (docs/kb/24-wire-constants.md, ADR 0011 v2 / 12-G).

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

import monorepo
from disconect import pair

FIXTURE = monorepo.CRATE / "tests" / "fixtures" / "test.keys.json"   # the shared key vectors, committed with the crate
OFFERER_PRIV = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
JOINER_PRIV = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
S = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
ID = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
ID_ZERO_SAS = bytes.fromhex("2a2122232425262728292a2b2c2d2e2f")  # first byte 0x20.. stepped until the SAS < 100000
N_O = bytes(range(0x30, 0x50))
C = "2c08e0bd71af02fba14734f4b59043959d66c04a2e1a12defb09dbd127697930"
EXP = 1700000900
URL = "http://127.0.0.1:8321"
MASTER = bytes(range(1, 33))

OFFERER_PUB = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
JOINER_PUB = "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
DH = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
OFFER_TEXT = (
    "disconect-pair:v2.eyJ2IjoyLCJwdWIiOiI4NTIwZjAwOTg5MzBhNzU0NzQ4YjdkZGNiNDNlZjc1YTBkYmYzYTBkMjYzODFhZjRlYmE0YTk4ZWFh"
    "OWI0ZTZhIiwiYyI6IjJjMDhlMGJkNzFhZjAyZmJhMTQ3MzRmNGI1OTA0Mzk1OWQ2NmMwNGEyZTFhMTJkZWZiMDlkYmQxMjc2OTc5MzAiLCJzIjoiMDAw"
    "MTAyMDMwNDA1MDYwNzA4MDkwYTBiMGMwZDBlMGYiLCJpZCI6IjEwMTExMjEzMTQxNTE2MTcxODE5MWExYjFjMWQxZTFmIiwiZXhwIjoxNzAwMDAwOTAw"
    "LCJ1cmwiOiJodHRwOi8vMTI3LjAuMC4xOjgzMjEifQ"
)
TRANSCRIPT = (
    "646973636f6e6563742f706169722f7632008520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f101112131415161718191a1b1c1d1e1f000000006553f484"
    "2c08e0bd71af02fba14734f4b59043959d66c04a2e1a12defb09dbd127697930"
)
K = "042ab2cd8fdd674d708af0e38242bca48e3d8223d769240fa5236596df173cae"
K_CONFIRM = "ade5e5ac831e2c078ac5ff75e8f29fabaaf4b85b97ede4d782de3e7a014d1bd1"
K_OFFERER = "5879aadcb2e23ffeab104838fa5324e5f3ae7183ca924fab9c433e6fa1ec1a26"
K_SAS = "35dd953a91fbacfbfcf705fa7195101bbc84f949093fa6ff0e45d5a03eee4a14"
K_PAYLOAD = "cf3239ae77e6b39c37128c877751f47f2bea3ca7190d50d0eddfdf9b710507f4"
JOINER_TAG = "f75be896e432534bae6eaf9d00c55ea4b4a33deb5a9de7cbb38daad14aba009f"
OFFERER_TAG = "5410268e5d3b5b62d787ef89c94d374e1b143bd57ff5b498f10e614b72f66d74"
FIXTURE_SHA = "cd538c0cddf607a878a8424c3dcbbd58f9e2ba54529b550f18f813f446b01190"
SEALED_SHA = "aabbdb81b8207c224001bd2906c4e98eca196c7f65adc52b54d797e36e7b754b"
LOW_ORDER = bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800")


def _public(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _session(id_bytes: bytes = ID):
    shared = pair.dh(OFFERER_PRIV, bytes.fromhex(JOINER_PUB))
    wire = pair.transcript(bytes.fromhex(OFFERER_PUB), bytes.fromhex(JOINER_PUB), id_bytes, EXP, bytes.fromhex(C))
    return wire, pair.derive_keys(shared, S, wire)


def test_public_keys_are_the_rfc_7748_values():
    assert _public(OFFERER_PRIV).hex() == OFFERER_PUB
    assert _public(JOINER_PRIV).hex() == JOINER_PUB


def test_dh_agrees_both_directions():
    assert pair.dh(OFFERER_PRIV, bytes.fromhex(JOINER_PUB)).hex() == DH
    assert pair.dh(JOINER_PRIV, bytes.fromhex(OFFERER_PUB)).hex() == DH


def test_commitment_vector():
    assert pair.commit(N_O).hex() == C
    assert pair.commit(N_O) == hashlib.sha256(b"disconect/pair/v2/commit" + N_O).digest()


def test_offer_text_and_roundtrip():
    text = pair.encode_offer(bytes.fromhex(OFFERER_PUB), bytes.fromhex(C), S, ID, EXP, URL)
    assert text == OFFER_TEXT
    assert "=" not in text and len(text) == 388
    offer = pair.parse_offer(text)
    assert (offer.pub.hex(), offer.c.hex(), offer.s, offer.id, offer.exp, offer.url) == (OFFERER_PUB, C, S, ID, EXP, URL)


def test_transcript_and_key_schedule():
    wire, keys = _session()
    assert wire.hex() == TRANSCRIPT and len(wire) == 138
    assert wire[106:] == bytes.fromhex(C), "c is the last transcript field"
    assert keys.k.hex() == K
    assert keys.confirm.hex() == K_CONFIRM
    assert keys.offerer.hex() == K_OFFERER
    assert keys.sas.hex() == K_SAS
    assert keys.payload.hex() == K_PAYLOAD


def test_tags_and_sas():
    _, keys = _session()
    assert pair.joiner_tag(keys).hex() == JOINER_TAG
    assert pair.offerer_tag(keys, N_O).hex() == OFFERER_TAG
    assert pair.offerer_reply(keys, N_O) == N_O + bytes.fromhex(OFFERER_TAG)
    assert pair.sas(keys, N_O) == 658698
    assert pair.sas_text(keys, N_O) == "658698"


def test_the_offerer_reply_opens_to_the_nonce():
    _, keys = _session()
    assert pair.open_offerer_reply(keys, bytes.fromhex(C), pair.offerer_reply(keys, N_O)) == N_O


def test_offerer_reply_negatives():
    """The joiner shows no code on any of these: wrong length, a reveal that does not match c (right tag for
    it or not), the right reveal with a wrong tag, a reply under another session's keys."""
    _, keys = _session()
    c = bytes.fromhex(C)
    good = pair.offerer_reply(keys, N_O)
    other_nonce = bytes(range(0x50, 0x70))
    _, other = _session(ID_ZERO_SAS)
    bad = {
        "32 bytes": good[:32],
        "96 bytes": good + bytes(32),
        "empty": b"",
        "wrong reveal, tag over it": pair.offerer_reply(keys, other_nonce),
        "wrong reveal, right tag": other_nonce + good[32:],
        "right reveal, wrong tag": N_O + bytes([good[32] ^ 1]) + good[33:],
        "another session's reply": pair.offerer_reply(other, N_O),
    }
    for name, body in bad.items():
        with pytest.raises(pair.PairError, match="did not confirm"):
            pair.open_offerer_reply(keys, c, body)
        assert name
    with pytest.raises(pair.PairError):
        pair.open_offerer_reply(keys, pair.commit(other_nonce), good)  # the offer promised another nonce


def test_leading_zero_sas_case():
    _, keys = _session(ID_ZERO_SAS)
    assert pair.sas(keys, N_O) == 47069
    assert pair.sas_text(keys, N_O) == "047069"


@monorepo.needs_monorepo
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
    document = {"v": 2, "pub": OFFERER_PUB, "c": C, "s": S.hex(), "id": ID.hex(), "exp": EXP, "url": URL}
    document.update(changes)
    return json.dumps(document, separators=(",", ":")).encode()


def _bad_offers() -> dict[str, str]:
    good = _doc()
    return {
        "uppercase hex": _offer_text(_doc(pub=OFFERER_PUB.upper())),
        "duplicate key": _offer_text(good[:-1] + b',"v":2}'),
        "duplicate c": _offer_text(good[:-1] + b',"c":"' + C.encode() + b'"}'),
        "unknown key": _offer_text(good[:-1] + b',"x":1}'),
        "v=1": _offer_text(_doc(v=1)),
        "uppercase c": _offer_text(_doc(c=C.upper())),
        "short c": _offer_text(_doc(c=C[:62])),
        "missing c": _offer_text(json.dumps({k: v for k, v in json.loads(_doc()).items() if k != "c"}, separators=(",", ":")).encode()),
        "v=true": _offer_text(_doc(v=True)),
        "padded base64": _offer_text(_doc(url="http://127.0.0.1:83"), pad=True),
        "port 0": _offer_text(_doc(url="http://127.0.0.1:0")),
        "port too large": _offer_text(_doc(url="http://127.0.0.1:65536")),
        "no port": _offer_text(_doc(url="http://127.0.0.1")),
        "https": _offer_text(_doc(url="https://127.0.0.1:8321")),
        "trailing slash": _offer_text(_doc(url=URL + "/")),
        "float exp": _offer_text(_doc(exp=1700000900.0)),
        "negative exp": _offer_text(_doc(exp=-1)),
        "bool exp": _offer_text(_doc(exp=True)),
        "wrong hex length": _offer_text(_doc(s="00" * 15)),
        "wrong prefix": "disconect-pair:v1." + OFFER_TEXT[len(pair.OFFER_PREFIX):],
        "no prefix": OFFER_TEXT[len(pair.OFFER_PREFIX):],
        "not base64": pair.OFFER_PREFIX + "!!!",
        "not json": _offer_text(b"nope"),
        "missing key": _offer_text(json.dumps({"v": 2}).encode()),
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
    body = OFFER_TEXT[len(pair.OFFER_PREFIX):]
    assert base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).startswith(b'{"v":2,"pub":"' + OFFERER_PUB.encode() + b'","c":')


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
