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
]


def _strptime_outcome(text: str):
    try:
        return datetime.datetime.strptime(text, ISO).replace(tzinfo=UTC)
    except ValueError as exc:
        return (type(exc), str(exc))


def _outcome(text: str):
    try:
        return parse_iso_utc(text)
    except ValueError as exc:
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
