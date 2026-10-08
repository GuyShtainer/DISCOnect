"""The five relay/pair methods on the Python oracle: the shared check prefix, the parameter shapes, then
``unsupported_transport`` (this core runs no server); ``parse_listen``'s rules, with the vectors the Rust core's
``serve_pair_test`` uses. ``tools/serve_diff.py`` (the two-core differential harness, not in this repository) compares the two cores on the refusals they share."""

from __future__ import annotations

import json

import pytest

from disconect import serve
from disconect.relay import config as relay_config
from disconect.storage import keys
from test_serve import Rig, _encrypt
from test_privacy import _seed

NO_RELAY = {"code": "not_found", "message": "no relay is configured (relay.json in the data folder)"}
GOOD = {"relay.addresses": {}, "relay.serve": {"on": False}, "pair.offer": {"listen": "192.168.1.20:24816"},
        "pair.confirm": {"digits": "123456"}, "pair.cancel": {}}
#: The two stop paths check only ``locked`` and the parameter shape, never the relay prefix.
STOPPERS = ("relay.serve", "pair.cancel")
PREFIXED = {method: params for method, params in GOOD.items() if method not in STOPPERS}
NOTHING_TO_CANCEL = {"code": "not_found", "message": "there is no offer to cancel"}


@pytest.fixture
def plain(db_path):
    _seed(db_path)
    return Rig(db_path)


@pytest.fixture
def encrypted(db_path, monkeypatch, capsys):
    _seed(db_path)
    _encrypt(db_path, monkeypatch)
    capsys.readouterr()
    rig = Rig(db_path)
    from test_serve import PASS
    assert rig.result("key.unlock", passphrase=PASS) == {"unlocked": True}
    return rig


def _relay_json(db_path, body) -> None:
    (db_path.parent / "relay.json").write_text(json.dumps(body))


def test_the_methods_are_registered_after_sync_run_and_before_tools_call():
    names = list(serve.METHODS)
    start = names.index("sync.run") + 1
    assert names[start:start + 6] == ["relay.addresses", "relay.serve", "pair.offer", "pair.confirm", "pair.cancel",
                                      "tools.call"]


def test_a_locked_store_answers_locked_before_anything_else(db_path, monkeypatch, capsys):
    _seed(db_path)
    _encrypt(db_path, monkeypatch)
    capsys.readouterr()
    rig = Rig(db_path)
    _relay_json(db_path, {"lan": "http://127.0.0.1:1"})
    for method in GOOD:
        assert rig.error_code(method) == "locked", method


def test_no_relay_is_not_found_before_the_parameters_are_looked_at(plain):
    for method in PREFIXED:
        assert plain.send(method)["error"] == NO_RELAY, method
        assert plain.send(method, junk=[1])["error"] == NO_RELAY, method
    assert plain.send("relay.serve", on="yes")["error"] == NO_RELAY
    assert plain.send("relay.serve", junk=[1])["error"] == NO_RELAY


def test_the_stop_paths_skip_the_relay_checks(plain, db_path, tmp_path):
    for body in (None, {"lan": "http://127.0.0.1:1"}, {"folder": str(tmp_path / "relay")}):
        if body is not None:
            _relay_json(db_path, body)
        assert plain.result("relay.serve", on=False) == {"serving": False, "url": None}, body
        assert plain.send("pair.cancel")["error"] == NOTHING_TO_CANCEL, body
        assert plain.send("pair.cancel", junk=1)["error"] == NOTHING_TO_CANCEL, body
    key_path = keys.key_path_for(db_path)
    key_path.with_name(key_path.name + keys.NEXT_SUFFIX).write_text("{}")
    assert plain.result("relay.serve", on=False) == {"serving": False, "url": None}
    assert plain.send("pair.cancel")["error"] == NOTHING_TO_CANCEL
    # the shape is still checked, and the other three methods still run the prefix
    assert plain.send("relay.serve", on=False, listen="localhost:1")["error"]["code"] == "bad_params"
    assert plain.send("relay.serve", on="yes")["error"]["code"] == "not_encrypted"


def test_a_lan_relay_is_a_joiner_and_serves_nothing(plain, db_path):
    _relay_json(db_path, {"lan": "http://127.0.0.1:1"})
    for method, params in PREFIXED.items():
        assert plain.send(method, **params)["error"] == {
            "code": "bad_params", "message": "this device is a joiner; it serves nothing"}, method
    assert plain.send("relay.serve", on=True, listen="192.168.1.20:24816")["error"]["code"] == "bad_params"


def test_a_plaintext_store_with_a_folder_relay_is_not_encrypted(plain, db_path, tmp_path):
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    for method, params in PREFIXED.items():
        error = plain.send(method, **params)["error"]
        assert error["code"] == "not_encrypted", method
        assert error["message"] == f"the relay needs an encrypted store: run '{serve.identity.COMMAND} key init' first"


def test_a_key_rotation_in_progress_is_busy_before_the_parameters(encrypted, db_path, tmp_path):
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    key_path = keys.key_path_for(db_path)
    key_path.with_name(key_path.name + keys.NEXT_SUFFIX).write_text("{}")
    for method in PREFIXED:
        assert encrypted.send(method, junk=1)["error"] == {
            "code": "busy", "message": "a key rotation is in progress; finish it first"}, method
    assert encrypted.send("relay.serve", on="yes")["error"]["code"] == "busy"


@pytest.mark.parametrize("method, params, message", [
    ("relay.serve", {}, "on must be true or false"),
    ("relay.serve", {"on": "yes"}, "on must be true or false"),
    ("relay.serve", {"on": 1}, "on must be true or false"),
    ("relay.serve", {"on": None}, "on must be true or false"),
    ("relay.serve", {"on": True}, "listen must be a non-empty string"),
    ("relay.serve", {"on": True, "listen": None}, "listen must be a non-empty string"),
    ("relay.serve", {"on": True, "listen": ""}, "listen must be a non-empty string"),
    ("relay.serve", {"on": False, "listen": 24816}, "listen must be a non-empty string"),
    ("relay.serve", {"on": False, "listen": "localhost:1"}, "listen must be an IP address and a port"),
    ("pair.offer", {}, "listen must be a non-empty string"),
    ("pair.offer", {"listen": None}, "listen must be a non-empty string"),
    ("pair.offer", {"listen": ["127.0.0.1:1"]}, "listen must be a non-empty string"),
    ("pair.offer", {"listen": "0.0.0.0:1"}, "listen needs a concrete address"),
    ("pair.confirm", {}, "digits must be exactly six digits"),
    ("pair.confirm", {"digits": "12345"}, "digits must be exactly six digits"),
    ("pair.confirm", {"digits": "1234567"}, "digits must be exactly six digits"),
    ("pair.confirm", {"digits": "12345a"}, "digits must be exactly six digits"),
    ("pair.confirm", {"digits": 123456}, "digits must be exactly six digits"),
])
def test_the_parameter_shapes_are_bad_params_with_the_rust_texts(encrypted, db_path, tmp_path, method, params, message):
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    error = encrypted.send(method, **params)["error"]
    assert error["code"] == "bad_params" and error["message"].startswith(message), error


def test_after_the_prefix_and_the_shape_every_method_is_unsupported_transport(encrypted, db_path, tmp_path):
    _relay_json(db_path, {"folder": str(tmp_path / "relay")})
    for method, params in PREFIXED.items():
        assert encrypted.send(method, **params)["error"] == {
            "code": "unsupported_transport", "message": "this core runs no LAN server"}, method
    assert encrypted.result("relay.serve", on=False) == {"serving": False, "url": None}
    assert encrypted.send("pair.cancel")["error"] == NOTHING_TO_CANCEL
    assert encrypted.send("relay.serve", on=True, listen="192.168.1.20:24816")["error"]["code"] == "unsupported_transport"
    assert encrypted.send("relay.serve", on=True, listen="[2001:db8::1]:24816")["error"]["code"] == "unsupported_transport"


def test_sync_status_has_serving_null_on_a_plain_store(plain):
    assert plain.result("sync.status")["serving"] is None


def test_sync_status_has_serving_null_on_an_encrypted_store(encrypted):
    assert encrypted.result("sync.status")["serving"] is None


ACCEPTED = [("192.168.77.5:24816", "192.168.77.5", 24816), ("127.0.0.1:1", "127.0.0.1", 1),
            ("10.0.0.1:65535", "10.0.0.1", 65535), ("[::1]:24816", "::1", 24816),
            ("[2001:db8::1]:80", "2001:db8::1", 80)]

#: The Rust core's ``listen_refusals_name_the_rule_and_never_the_address`` vectors (serve_pair_test.rs).
REFUSED = [
    ("localhost:24816", "listen must be an IP address and a port"),
    ("192.168.77.5", "listen must be an IP address and a port"),
    ("192.168.77.5:", "listen needs an explicit port"),
    ("192.168.77.5:0", "listen needs an explicit port"),
    ("192.168.77.5:024816", "listen needs an explicit port"),
    ("192.168.77.5:65536", "listen needs an explicit port"),
    ("192.168.77.5:+4816", "listen needs an explicit port"),
    ("0.0.0.0:24816", "listen needs a concrete address"),
    ("[::]:24816", "listen needs a concrete address"),
    ("[fe80::1]:24816", "listen cannot carry a zone id"),
    ("[fe80::1%en0]:24816", "listen cannot carry a zone id"),
    ("169.254.9.9:24816", "listen cannot carry a zone id"),
    ("::1:24816", "listen must be an IP address and a port"),
    ("[::1]24816", "listen must be an IP address and a port"),
    ("[::ffff:0.0.0.0]:24816", "listen must be an IP address and a port"),
    ("[::ffff:169.254.1.1]:24816", "listen must be an IP address and a port"),
    ("[::ffff:192.168.1.20]:24816", "listen must be an IP address and a port"),
]


@pytest.mark.parametrize("text, ip, port", ACCEPTED)
def test_parse_listen_accepts_a_literal_and_an_explicit_port(text, ip, port):
    found = relay_config.parse_listen(text)
    assert (str(found[0]), found[1]) == (ip, port)


@pytest.mark.parametrize("text, rule", REFUSED)
def test_parse_listen_names_the_rule_and_never_the_input(text, rule):
    with pytest.raises(ValueError) as caught:
        relay_config.parse_listen(text)
    message = str(caught.value)
    assert message.startswith(rule), message
    for fragment in ("192.168.77", "169.254.9", "fe80", "localhost", "en0"):
        assert fragment not in message


def test_pair_forget_is_the_phones_method_and_this_core_always_refuses_it(encrypted):
    """`pair.forget` is Rust-only on iOS. Here it answers `unsupported_transport` first, locked or not, with any params."""
    assert list(serve.METHODS)[-3:] == ["pair.forget", "pair.join", "pair.land"]
    expected = {"code": "unsupported_transport", "message": "this core is not a phone; there is nothing to forget"}
    for params in ({}, {"preview": True}, {"x": [1]}):
        assert encrypted.send("pair.forget", **params)["error"] == expected


# ---- pair.join and pair.land, the phone's methods; this core runs the prefix and then refuses ----

NOT_A_PHONE = {"code": "unsupported_transport", "message": "this core is not a phone; it does not join a pairing"}
CLOCK_EXPIRED = "This offer expired by this phone's clock. Check the date and time."
CLOCK_AHEAD = "This offer is too far ahead of this phone's clock. Check the date and time."
BAD_ADDRESS = "the offer's address cannot be a pairing address"
FORGET_PENDING = "This phone has not finished forgetting its last pairing. Close and reopen the app, then try again."


def _offer(url: str, exp: int) -> str:
    import os

    from disconect import pair
    return pair.encode_offer(os.urandom(32), os.urandom(32), os.urandom(16), os.urandom(16), exp, url)


def test_pair_join_runs_the_phones_prefix_in_order_then_refuses(db_path):
    import time

    rig = Rig(db_path)          # no store yet: nothing here is "already paired"
    now = int(time.time())
    url = "http://192.168.1.20:24816"
    bad = lambda message: {"code": "bad_params", "message": message}   # noqa: E731
    failed = lambda message: {"code": "pair_failed", "message": message}   # noqa: E731
    for params, expected in (
        ({}, bad("offer must be a non-empty string")),
        ({"offer": 12}, bad("offer must be a non-empty string")),
        ({"offer": ""}, bad("offer must be a non-empty string")),
        ({"offer": "hello"}, bad("that is not a pairing offer")),
        ({"offer": "disconect-pair:v2." + "A" * 2000}, bad("that is not a pairing offer")),
        ({"offer": _offer("http://mac.local:24816", now + 600)},
         bad("the offer's address must be an IP address, not a name")),
        ({"offer": _offer("http://mac.local:1", 1000)}, bad("the offer's address must be an IP address, not a name")),
        ({"offer": _offer(url, now + 1500)}, failed(CLOCK_AHEAD)),
        ({"offer": _offer(url, now - 400)}, failed(CLOCK_EXPIRED)),
        ({"offer": _offer(url, now + 600)}, NOT_A_PHONE),
        ({"offer": _offer(url, now - 100)}, failed(CLOCK_EXPIRED)),     # no tolerance past exp
        ({"offer": _offer(url, now - 5)}, failed(CLOCK_EXPIRED)),
        ({"offer": _offer("http://0.0.0.0:24816", 1000)}, bad(BAD_ADDRESS)),       # the class before the clock
        ({"offer": _offer("http://[fd00::5]:24816", now + 600)}, NOT_A_PHONE),
    ):
        assert rig.send("pair.join", **params)["error"] == expected, params
    # nothing of the offer is echoed
    assert "192.168" not in json.dumps(rig.send("pair.join", offer=_offer(url, now - 400)))


def test_pair_join_refuses_addresses_that_are_never_a_pairing_address(db_path):
    import time

    rig = Rig(db_path)
    now = int(time.time())
    for host in ("0.0.0.0", "0.1.2.3", "255.255.255.255", "224.0.0.1", "239.255.255.250", "240.0.0.1", "169.254.1.1",
                 "[::]", "[ff02::1]", "[fe80::1]", "[febf::1]", "[::ffff:0:0]", "[::ffff:a9fe:101]"):
        reply = rig.send("pair.join", offer=_offer(f"http://{host}:24816", now + 600))
        assert reply["error"] == {"code": "bad_params", "message": BAD_ADDRESS}, host
    # loopback, private and global addresses pass the class check (stage 2 is the phone's alone)
    for host in ("127.0.0.1", "[::1]", "10.0.0.5", "192.168.1.20", "100.64.0.1", "[fd00::5]", "8.8.8.8", "[fec0::1]",
                 "[2001:4860:4860::8888]", "223.255.255.255", "169.253.1.1"):
        reply = rig.send("pair.join", offer=_offer(f"http://{host}:24816", now + 600))
        assert reply["error"] == NOT_A_PHONE, host


def test_pair_join_trims_exactly_what_rust_trims(db_path):
    import time

    rig = Rig(db_path)
    good = _offer("http://192.168.1.20:24816", int(time.time()) + 600)
    for lead, trail in ((" ", "\n"), ("\u3000", ""), ("\x85", "\xa0"), ("\x0b\x0c", "\r"), ("\u2028", "\u202f")):
        assert rig.send("pair.join", offer=lead + good + trail)["error"] == NOT_A_PHONE, repr((lead, trail))
    # str.strip() would also strip these; Rust's str::trim does not
    for text in ("\x1c" + good, good + "\x1f", "\x1d" + good, "\u200b" + good):
        assert rig.send("pair.join", offer=text)["error"] == {
            "code": "bad_params", "message": "that is not a pairing offer"}, repr(text[:2])


def test_pair_join_refuses_after_an_unfinished_forget(db_path):
    import time

    rig = Rig(db_path)
    now = int(time.time())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (db_path.parent / "forget.pending").write_bytes(b"")
    url = "http://192.168.1.20:24816"
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == {
        "code": "pair_failed", "message": FORGET_PENDING}
    assert rig.send("pair.join", offer=_offer(url, 1000))["error"]["message"] == CLOCK_EXPIRED   # the clock first
    db_path.write_bytes(b"x")                                                                 # "already paired" first
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"]["message"] == "This phone is already paired"


def test_pair_join_reads_a_lan_relay_file_alone_as_a_failed_landings_leftover(db_path):
    """The rule both cores share: a relay.json that is not a LAN relay is a pairing; a LAN relay of another
    address is one only beside some store's key file (a desktop folder); alone it is a leftover; one naming the
    offer's address is this pairing's."""
    import time

    rig = Rig(db_path)          # no store
    now = int(time.time())
    url = "http://192.168.1.20:24816"
    folder = db_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    relay_file = folder / "relay.json"
    paired = {"code": "pair_failed", "message": "This phone is already paired"}
    relay_file.write_text(json.dumps({"lan": "http://192.168.1.77:24816"}) + "\n")
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == NOT_A_PHONE       # a leftover: the prefix passes
    relay_file.write_text(json.dumps({"lan": url}) + "\n")
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == NOT_A_PHONE       # this pairing's own file
    relay_file.write_text(json.dumps({"folder": "/elsewhere"}))
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == paired            # another transport
    relay_file.write_text(json.dumps({"folder": 5}))
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == paired            # names nothing: other content
    relay_file.write_text(json.dumps({"lan": "http://192.168.1.77:24816"}) + "\n")
    sibling = folder / ("desktop.hbdb" + keys.KEY_FILE_SUFFIX)
    sibling.write_text("{}")
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == paired            # a desktop store's pairing
    relay_file.write_text(json.dumps({"lan": url}) + "\n")
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == NOT_A_PHONE       # the url it names
    relay_file.write_text(json.dumps({"lan": "http://192.168.1.77:24816"}) + "\n")
    sibling.rename(folder / ("desktop.hbdb" + keys.KEY_FILE_SUFFIX + keys.NEXT_SUFFIX))
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == paired            # a rotation in progress too
    (folder / ("desktop.hbdb" + keys.KEY_FILE_SUFFIX + keys.NEXT_SUFFIX)).unlink()
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == NOT_A_PHONE       # alone again
    assert rig.send("pair.join", offer=_offer(url, 1000))["error"]["message"] == CLOCK_EXPIRED   # the clock first


def test_pair_join_refuses_an_already_paired_phone_after_the_expiry_check(db_path):
    import time

    _seed(db_path)             # a store exists here
    rig = Rig(db_path)
    now = int(time.time())
    url = "http://192.168.1.20:24816"
    assert rig.send("pair.join", offer=_offer(url, now + 600))["error"] == {
        "code": "pair_failed", "message": "This phone is already paired"}
    assert rig.send("pair.join", offer=_offer(url, 1000))["error"]["message"] == CLOCK_EXPIRED


def test_pair_land_checks_the_shape_then_refuses(db_path):
    rig = Rig(db_path)
    for params in ({}, {"confirm": "yes"}, {"confirm": 1}, {"confirm": None}):
        assert rig.send("pair.land", **params)["error"] == {
            "code": "bad_params", "message": "confirm must be true or false"}
    for value in (True, False):
        assert rig.send("pair.land", confirm=value)["error"] == NOT_A_PHONE
