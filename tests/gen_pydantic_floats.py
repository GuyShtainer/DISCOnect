#!/usr/bin/env python3
"""Measure how ``pydantic_core.to_json`` writes a float: ``fixtures/mcp/pydantic_floats.json``.

    python tests/gen_pydantic_floats.py          # rewrite the fixture

The Python MCP server builds ``content[0].text`` with ``pydantic_core.to_json(result, indent=2)`` (SDK
``func_metadata``), so the Rust server must write floats the same way, byte for byte. The rule is *measured*
here, not typed in: a grid of powers of ten (1e-10 .. 1e22) with several mantissas, the neighbours of both
thresholds, shortest-repr ties, negative zero, floats that are whole numbers, and a seeded random sample.
Each entry holds the float's bits (exact), Python's ``repr`` (what ``json.dumps`` writes) and pydantic's text.

What the table shows (2.46.5, checked over 500 000 floats): the shortest round-trip digits, plain notation iff
1e-5 <= |x| < 1e16 (``json.dumps``: 1e-4 <= |x| < 1e16), otherwise ``d[.ddd]e<sign><exponent>`` with an explicit
sign and no zero padding (``1e-7``, ``1.5e+16``); a whole number keeps ``.0``; ``-0.0`` stays signed; NaN and
the infinities are bare words.
"""

from __future__ import annotations

import json
import pathlib
import random
import struct

import pydantic_core

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp" / "pydantic_floats.json"
SEED = 11
RANDOM_BITS = 400
RANDOM_DECIMALS = 400


def bits_of(value: float) -> str:
    return f"{struct.unpack('<Q', struct.pack('<d', value))[0]:016x}"


def candidates() -> list[float]:
    """Every float the table holds, in a fixed order."""
    values: list[float] = []
    for exponent in range(-10, 23):
        for mantissa in ("1", "1.5", "9.99", "1.2345678901234567", "9.999999999999999"):
            values.append(float(f"{mantissa}e{exponent}"))
    for exponent in range(-30, 31, 5):
        values += [float(f"1e{exponent}"), -float(f"2.5e{exponent}")]
    values += [1e-5, 9.999999999999999e-6, 1.0000000000000002e-5, 0.0001, 0.00009999999999999999, 1e15,
               9999999999999998.0, 1e16, 1.0000000000000002e16, 0.1 + 0.2, 0.1, 1 / 3, 2 / 3, 5e-324,
               2.2250738585072014e-308, 1.7976931348623157e308, float(2**53), float(2**53 + 2), float(2**63),
               123456789012345678.0, 0.0, -0.0, 1.0, -1.0, 100.0, 1500.0, 76.5, 0.5, 2.5, 1e22, 1e23,
               float("inf"), float("-inf"), float("nan")]
    rng = random.Random(SEED)
    for _ in range(RANDOM_BITS):
        value = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
        if value == value:
            values.append(value)
    for _ in range(RANDOM_DECIMALS):
        values.append(float(f"{rng.randint(1, 10 ** rng.randint(1, 17))}e{rng.randint(-25, 25)}"))
    return values


def table() -> dict:
    entries = []
    for value in candidates():
        entries.append({"bits": bits_of(value), "repr": repr(value),
                        "pydantic": pydantic_core.to_json(value).decode("ascii")})
    return {"notes": ["Measured by tests/gen_pydantic_floats.py from pydantic_core.to_json; never edited by hand.",
                      "bits: the IEEE-754 double as 16 hex digits; repr: Python's repr; pydantic: its JSON text."],
            "pydantic_core": pydantic_core.__version__, "floats": entries}


def rendered() -> str:
    return json.dumps(table(), indent=1, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    FIXTURE.write_text(rendered(), encoding="utf-8")
    print(f"wrote {FIXTURE.name}: {len(table()['floats'])} floats")
