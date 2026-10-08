"""7b-14: `parse_iso_utc` takes a fast path for the store's exact shape and behaves exactly like `strptime` otherwise."""

from __future__ import annotations

import datetime

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


#: Python 3.14's texts, pinned: the Rust core's `parse_store_stamp` (time.rs) gives the same ones and the serve
#: layer carries them as `bad_params`, so a Python release that rewords one shows up here before the differ.
MIRRORED_TEXTS = [
    ("2025-06-15 00:00:00", "time data '2025-06-15 00:00:00' does not match format '%Y-%m-%dT%H:%M:%SZ'"),
    ("2025-06-15T00:00:00Z ", "unconverted data remains:  "),
    ("2025-02-30T00:00:00Z", "day 30 must be in range 1..28 for month 2 in year 2025"),
    ("2100-02-29T00:00:00Z", "day 29 must be in range 1..28 for month 2 in year 2100"),
    ("2025-06-15T00:00:60Z", "second must be in 0..59, not 60"),
    ("0000-02-30T00:00:60Z", "year must be in 1..9999, not 0"),
    ("2025-02-30T00:00:60Z", "day 30 must be in range 1..28 for month 2 in year 2025"),
]


@pytest.mark.parametrize(("text", "message"), MIRRORED_TEXTS)
def test_the_error_texts_the_rust_core_mirrors(text, message):
    with pytest.raises(ValueError) as caught:
        parse_iso_utc(text)
    assert str(caught.value) == message
