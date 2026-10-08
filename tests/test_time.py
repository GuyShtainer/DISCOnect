"""7b-14: `parse_iso_utc` takes a fast path for the store's exact shape and behaves exactly like `strptime` otherwise."""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from disconect.storage import _time
from disconect.storage._time import ISO, NOW_ENV, UTC, now_utc, parse_iso_utc

#: Shapes strptime accepts that the fast path does not (single-digit fields, lowercase marks, non-ASCII
#: digits), shapes neither accepts, and right-shaped impossible dates — each must get strptime's outcome.
PROBES = [
    "2026-03-04T01:02:03Z", "2026-3-4T1:2:3Z", "2026-03-04t01:02:03z", "２026-03-04T01:02:03Z",
    "2026-03-04 01:02:03Z", "2026-03-04T01:02:03+00:00", "2026-03-04T01:02:03", "20260304T010203Z",
    "2026-03-04T01:02:03.5Z", " 2026-03-04T01:02:03Z", "2026-03-04T01:02:03Z\n", "0000-03-04T01:02:03Z",
    "2026-02-30T01:02:03Z", "2026-03-04T01:02:60Z", "2026-13-04T01:02:03Z", "2026-03-04T24:02:03Z", "",
    "2026-03-04T24:00:00Z", "2026-12-31T24:00:00Z",  # 3.14's fromisoformat reads these as the next midnight
    "2026-03-04T01:60:00Z", "2026-03-04T23:59:59Z", "9999-12-31T23:59:59Z", "0001-01-01T00:00:00Z",
    None, b"2026-03-04T01:02:03Z", 20260304,  # not text: strptime's TypeError text
]


def _strptime_outcome(text: str):
    try:
        return datetime.datetime.strptime(text, ISO).replace(tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        return (type(exc), str(exc))


def _outcome(text: str):
    try:
        return parse_iso_utc(text)
    except (ValueError, TypeError) as exc:
        return (type(exc), str(exc))


@pytest.mark.parametrize("text", PROBES)
def test_the_parser_gives_strptimes_outcome_for_every_shape(text):
    assert _outcome(text) == _strptime_outcome(text)


def test_the_store_shape_parses_to_the_same_aware_utc_moment():
    moment = parse_iso_utc("2026-03-14T01:59:26Z")
    assert moment == datetime.datetime(2026, 3, 14, 1, 59, 26, tzinfo=UTC)
    assert moment.tzinfo is UTC
    assert moment.utcoffset() == datetime.timedelta(0)


def test_the_store_shape_never_reaches_strptime(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the exact shape must take the fast path")
    monkeypatch.setattr(_time, "_strptime", refuse)
    assert parse_iso_utc("2026-03-14T01:59:26Z").year == 2026
    with pytest.raises(AssertionError):
        parse_iso_utc("2026-3-14T01:59:26Z")  # not the store's shape: strptime's job


def test_the_pin_still_reads_through_the_parser(monkeypatch):
    monkeypatch.setenv(NOW_ENV, "2026-03-14T01:59:26Z")
    assert now_utc() == datetime.datetime(2026, 3, 14, 1, 59, 26, tzinfo=UTC)
    monkeypatch.setenv(NOW_ENV, "yesterday")
    assert now_utc().year >= 2026


#: The shared table (`tests/fixtures/strptime-stamps.json`): every row's outcome was read off the Python the
#: fixture names, and the Rust core's `parse_store_stamp` (time.rs) pins the same rows — so a Python release that
#: rewords a text or accepts a new shape fails here before the differ, and the fixture is regenerated on purpose.
_TABLE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "strptime-stamps.json").read_text())


@pytest.mark.parametrize(("text", "kind", "outcome"), _TABLE["rows"], ids=repr)
def test_the_shared_table_the_rust_core_mirrors(text, kind, outcome):
    if kind == "ok":
        assert parse_iso_utc(text).strftime(ISO) == outcome
    else:
        with pytest.raises(ValueError) as caught:
            parse_iso_utc(text)
        assert str(caught.value) == outcome


def test_the_shared_table_covers_every_text_and_the_spaced_hour():
    texts = {row[2].split(" ")[0] for row in _TABLE["rows"] if row[1] == "err"}
    assert texts == {"time", "unconverted", "day", "second", "year"}
    assert ["2025-06-15T 0:00:00Z", "ok", "2025-06-15T00:00:00Z"] in _TABLE["rows"]  # 3.14's `%H` takes ` \d`


def test_non_ascii_digits_are_the_one_divergence_from_the_rust_core():
    # `\d` in Python's regex takes U+FF12 etc. and int() reads them; the Rust mirror refuses them (time.rs test)
    assert parse_iso_utc("２０２５-06-15T00:00:00Z").year == 2025
