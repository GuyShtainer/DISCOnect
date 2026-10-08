#!/usr/bin/env python3
"""Generate the Rust ``canon`` module's oracle fixtures (canonical JSON byte identity).

For each invented case ``<name>`` it writes, under
``projects/disconect-core/tests/fixtures/canon/``:

* ``<name>.json``     the input bytes (``json.dumps(obj, indent=1, ensure_ascii=False)``: unsorted,
                      raw UTF-8; a few cases are hand-written texts that a dict cannot produce);
* ``<name>.expected`` what the Python oracle stores for that record:
                      ``json.dumps(record, sort_keys=True, separators=(",", ":"))`` as bytes.

``py_str.json`` is a list of ``[input_json_text, str(json.loads(text))]`` pairs (source keys are
built with ``str()``). Everything is invented; ``test_core_parity.py`` fails when a regenerated
set differs from the committed one.
"""

from __future__ import annotations

import json
import pathlib
import sys

import monorepo

CANON_DIR = monorepo.CRATE / "tests" / "fixtures" / "canon"

#: Cases built from Python objects (their input is ``json.dumps(obj, indent=1, ensure_ascii=False)``).
OBJECT_CASES: dict[str, object] = {
    "unicode_keys_and_strings": {"é": "ü", "日本語": "テスト", "z": "Ünïcode ☃", "a": ""},
    "astral_plane": {"\U0001F600": "\U0001F680 rocket", "plain": "\U00010348", "\uffff": "\ufffe"},
    "control_chars": {"c": "".join(chr(n) for n in range(0x20)) + "\x7f", "del": "\x7f", "mix": "a\tb\nc\rd\be\ff\x00g"},
    "quotes_backslashes_slashes": {'q"k': 'say "hi"', "b\\k": "C:\\dir\\file", "slash": "a/b//c", "mixed": "\\\"/"},
    "floats_exponent_thresholds": [1e-5, 0.0001, 0.00012345, 1e15, 1e16, 123456789012345.6, 1.5e300, 5e-324, -2.5e-7, 1.7976931348623157e308],
    "floats_plain": [70.0, 0.1, 1234.5678, -0.0, 0.0, 1.0, -1.5, 100.0, 3.14159, 2.0e3],
    # Exact decimal ties at the 16th digit: Python (and ryu) keep the even digit, Rust's own
    # `{:e}` / Display rounds the other way.
    "floats_shortest_tie": [623721.3168945313, 1228471191644118.3, 1077083921405962.3, 0.30000000000000004, 2.675],
    "ints": [0, -1, 1, 2**53, 2**53 + 1, 2**63 - 1, -(2**63), 12345678901234],
    "nested_unsorted": {"zeta": {"y": 1, "x": {"b": [3, 2, 1], "a": None}}, "alpha": [{"d": 1, "c": 2}, {"b": 3, "a": 4}], "mid": {"k": {"j": {"i": 1}}}},
    "empty_containers": {"o": {}, "a": [], "s": "", "nested": [[], {}, [[]], [{}]]},
    "literals": {"t": True, "f": False, "n": None, "list": [True, False, None]},
    "deep_nesting": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": [1, [2, [3, {"z": 0, "y": 1}]]]}}}}}}}},
    "top_level_scalars_array": [1, 2.5, "x", None, True],
    "keys_sort_by_code_point": {"b": 1, "B": 2, "a": 3, "_": 4, "é": 5, "z": 6, "\U0001F600": 7, "\uffff": 8, "10": 9, "9": 10},
    "record_like": {"calendarDate": "2025-06-15", "minHeartRate": 48, "avgStress": 31.5, "bodyBatteryChargedValue": 70.0,
                    "values": [{"timestamp": 1750000000000, "level": 25.25}, {"level": 26, "timestamp": 1750000060000}]},
    "empty_string_key": {"": 1, "a": {"": [None]}},
}

#: Hand-written input texts (what a dict cannot say: duplicate keys, escapes, odd spacing).
TEXT_CASES: dict[str, str] = {
    "duplicate_keys": '{"b": 1, "a": 2, "b": 3, "c": {"x": 1, "x": 2}}',
    "escaped_input": '{"k\\u00e9y": "\\u00e9\\/\\ud83d\\ude00\\ud83D\\uDE00", "u": "\\u0041\\u007f\\u0000"}',
    "compact_input_with_exponents": '{"a":1E2,"b":1.5e+3,"c":-0.0e0,"d":[1e-7,0.0,1.0E16,100e-2]}',
    "whitespace_everywhere": ' \n\t{ "b" : [ 1 , 2 ] ,\r\n "a" :\t{ } } \n',
    "integer_forms": '[0, -0, 7]',
}

PY_STR_TEXTS = [
    "0", "-1", "-0", "9007199254740992", "9223372036854775807", "-9223372036854775808",
    "1.0", "70.0", "-0.0", "0.1", "1234.5678", "1e-5", "0.0001", "0.00012", "1e15", "1e16", "1.5e300",
    "5e-324", "1E2", "1.5E+3", "123456789012345.6", "1e22", "-2.5e-7", "623721.3168945313", "1228471191644118.3",
    "null", "true", "false", '"abc"', '""', '"\\u00e9"', '"2025-06-15"', '"True"',
]


def cases() -> dict[str, str]:
    """name -> input text."""
    out = {name: json.dumps(obj, indent=1, ensure_ascii=False) for name, obj in OBJECT_CASES.items()}
    out.update(TEXT_CASES)
    return out


def write_canon(target: pathlib.Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for name, text in sorted(cases().items()):
        record = json.loads(text)
        (target / f"{name}.json").write_bytes(text.encode("utf-8"))
        (target / f"{name}.expected").write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        names.append(name)
    pairs = [[text, str(json.loads(text))] for text in PY_STR_TEXTS]
    (target / "py_str.json").write_text(json.dumps(pairs, indent=1) + "\n")
    return names


def main() -> int:
    names = write_canon(CANON_DIR)
    print(f"wrote {len(names)} canonical-JSON cases + py_str.json to {CANON_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
