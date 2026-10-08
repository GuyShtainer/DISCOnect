#!/usr/bin/env python3
"""Generate the numerics differential for ``data.facts``: Python's answers, for the Rust port to match bit for bit.

    python tests/gen_facts_numerics.py            # rewrite ../../disconect-core/tests/fixtures/py_numerics_read.json
    python tests/gen_facts_numerics.py --check    # exit 1 when the committed file is not what this makes today

What it pins:

* ``math.fsum`` and ``statistics.fmean`` (= ``fsum(data) / n``) and ``statistics.stdev`` (exact rational variance,
  one correctly rounded square root) over vectors that are realistic (daily values with 2 decimals), all equal,
  length two, dyadic ties, cancelling, spread over hundreds of orders of magnitude, subnormal, signed zero,
  and long (365 values, the longest baseline);
* ``round(x, 2)`` (``insight._round``) and ``round(x, 3)`` (``queries._clean``) over decimal ties, dyadic ties,
  random bit patterns and the extremes.

Every float travels as 16 hex digits of its IEEE-754 bits, so nothing depends on a repr parser. A result
Python cannot produce (an ``OverflowError`` from ``fsum`` or from the square root, ``stdev`` of one value)
is ``null``; the Rust function must answer ``None`` for exactly those. The numbers are plain: no health data.
The seed is fixed, so the file is the same every run on the same CPython (3.14).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import struct
import sys

import monorepo

FIXTURE = monorepo.CRATE / "tests" / "fixtures" / "py_numerics_read.json"
SEED = 20261003
MIN_VECTOR_VALUES = 110_000
MIN_ROUND_VALUES = 25_000


def bits(value: float) -> str:
    """The IEEE-754 double ``value`` as 16 hex digits."""
    return struct.pack(">d", value).hex()


def from_bits(text: str) -> float:
    """Inverse of :func:`bits`."""
    return struct.unpack(">d", bytes.fromhex(text))[0]


def random_finite(rng: random.Random, low_exp: int, high_exp: int) -> float:
    """A float with a random sign, mantissa and binary exponent in ``[low_exp, high_exp]``."""
    mantissa = rng.getrandbits(52) | (1 << 52)
    return math.ldexp(mantissa, rng.randint(low_exp, high_exp) - 52) * rng.choice((-1.0, 1.0))


def realistic(rng: random.Random) -> list[float]:
    """Daily values: a mean, a spread, two decimals; sometimes integers."""
    centre = rng.choice((0.5, 7.0, 55.0, 70.0, 1500.0, 9000.0, 62000.0))
    spread = centre * rng.choice((0.0, 0.01, 0.1, 0.5))
    digits = rng.choice((0, 1, 2, 2, 2, 3))
    return [round(rng.gauss(centre, spread), digits) for _ in range(rng.randint(2, 40))]


def all_equal(rng: random.Random) -> list[float]:
    value = rng.choice((0.0, -0.0, 1.0, 0.1, 70.0, 1e-300, 1e300, 5e-324, -2.5, rng.uniform(-1e3, 1e3)))
    return [value] * rng.randint(2, 40)


def pair(rng: random.Random) -> list[float]:
    return [round(rng.uniform(-100, 100), rng.choice((0, 2, 5))) if rng.random() < 0.7 else random_finite(rng, -60, 60)
            for _ in range(2)]


def dyadic(rng: random.Random) -> list[float]:
    """Small integers over powers of two: sums and means land exactly on rounding ties."""
    return [rng.randint(-9, 9) / 2 ** rng.randint(0, 60) for _ in range(rng.randint(2, 30))]


def cancelling(rng: random.Random) -> list[float]:
    big = 10.0 ** rng.randint(10, 22)
    values = [big, -big] * rng.randint(1, 4) + [rng.uniform(-1, 1) for _ in range(rng.randint(1, 6))]
    values += [1.0, 1e-16, -1.0] * rng.randint(0, 2)
    rng.shuffle(values)
    return values


def spread(rng: random.Random) -> list[float]:
    """Magnitudes from 1e-320 to 1e300 in one vector."""
    return [rng.choice((-1.0, 1.0)) * rng.uniform(1.0, 10.0) * 10.0 ** rng.randint(-320, 300)
            for _ in range(rng.randint(2, 20))]


def subnormal(rng: random.Random) -> list[float]:
    values = [struct.unpack(">d", struct.pack(">Q", rng.getrandbits(52) | (rng.getrandbits(1) << 63)))[0]
              for _ in range(rng.randint(2, 20))]
    return values + [0.0] * rng.randint(0, 2)


def signed_zeros(rng: random.Random) -> list[float]:
    return [rng.choice((0.0, -0.0, 5e-324, -5e-324)) for _ in range(rng.randint(1, 6))]


def random_bits(rng: random.Random) -> list[float]:
    low = rng.choice((-1000, -100, -20))
    return [random_finite(rng, low, -low) for _ in range(rng.randint(2, 25))]


def long_vector(rng: random.Random) -> list[float]:
    centre = rng.choice((55.0, 9000.0, 0.7))
    return [round(rng.gauss(centre, centre * 0.15), 2) for _ in range(rng.randint(100, 400))]


def single(rng: random.Random) -> list[float]:
    return [rng.choice((rng.uniform(-1e3, 1e3), 0.0, -0.0, 1e308, random_finite(rng, -1000, 1000)))]


def near_overflow(rng: random.Random) -> list[float]:
    """Magnitudes near the float limit: fsum may overflow in the middle, stdev may not fit a float."""
    return [rng.choice((-1.0, 1.0)) * rng.uniform(1e307, 1.7976931348623157e308) for _ in range(rng.randint(2, 5))]


GENERATORS = (
    (realistic, 40), (all_equal, 6), (pair, 8), (dyadic, 10), (cancelling, 8), (spread, 8), (subnormal, 5),
    (signed_zeros, 2), (random_bits, 8), (long_vector, 4), (single, 1), (near_overflow, 2),
)


def answer(function, values: list[float]) -> str | None:
    """``bits(function(values))``, or None where Python raises."""
    try:
        return bits(function(values))
    except (OverflowError, ValueError, statistics.StatisticsError, ZeroDivisionError):
        return None


def make_vectors(rng: random.Random) -> list[dict]:
    weights = [weight for _, weight in GENERATORS]
    vectors: list[dict] = []
    total = 0
    while total < MIN_VECTOR_VALUES:
        generator = rng.choices([g for g, _ in GENERATORS], weights)[0]
        values = generator(rng)
        total += len(values)
        vectors.append({"v": "".join(bits(x) for x in values), "fsum": answer(math.fsum, values),
                        "fmean": answer(statistics.fmean, values), "stdev": answer(statistics.stdev, values)})
    return vectors


def round_inputs(rng: random.Random) -> list[float]:
    values: list[float] = []
    while len(values) < MIN_ROUND_VALUES:
        kind = rng.randrange(8)
        if kind == 0:
            values.append(rng.gauss(rng.choice((50.0, 7000.0, 0.3)), rng.choice((1.0, 100.0, 0.01))))
        elif kind == 1:
            values.append(rng.randint(-99999, 99999) / 8)          # exact ties at two digits
        elif kind == 2:
            values.append(rng.randint(-99999, 99999) / 16)         # exact ties at three digits
        elif kind == 3:
            values.append(float(f"{rng.randint(-9999, 9999)}.{rng.randint(0, 999):03d}5"))   # decimal near-ties
        elif kind == 4:
            values.append(float(f"{rng.randint(-99, 99)}.{rng.randint(0, 99):02d}5"))
        elif kind == 5:
            values.append(random_finite(rng, -1060, 1020))
        elif kind == 6:
            values.append(rng.choice((0.0, -0.0, 5e-324, -5e-324, 0.004999999999999999, 0.005, -0.005, 0.0005,
                                      1e22, 1e300, 1.7976931348623157e308, 2.675, 1.005, 0.125, 0.375)))
        else:
            values.append(rng.randint(-10 ** 15, 10 ** 15) * rng.choice((1.0, 0.1, 0.01, 0.001)))
    return values


def build() -> dict:
    rng = random.Random(SEED)
    vectors = make_vectors(rng)
    rounds = [[bits(x), bits(round(x, 2)), bits(round(x, 3))] for x in round_inputs(rng)]
    return {
        "_note": "Generated by disconect/tests/gen_facts_numerics.py with CPython "
                 f"{sys.version.split()[0]}: fsum/fmean/stdev of float vectors, round(x, 2) and round(x, 3). "
                 "Floats as 16 hex digits of their IEEE-754 bits; null = Python raises. Plain numbers, no health data.",
        "vectors": vectors,
        "rounds": rounds,
    }


def render(data: dict) -> str:
    return json.dumps(data, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file differs")
    args = parser.parse_args()
    text = render(build())
    if args.check:
        same = FIXTURE.exists() and FIXTURE.read_text() == text
        print("py_numerics_read.json is current" if same else "py_numerics_read.json is STALE")
        return 0 if same else 1
    FIXTURE.write_text(text)
    data = json.loads(text)
    values = sum(len(v["v"]) // 16 for v in data["vectors"])
    nulls = {key: sum(v[key] is None for v in data["vectors"]) for key in ("fsum", "fmean", "stdev")}
    print(f"wrote {FIXTURE.name}: {len(data['vectors'])} vectors, {values} values, "
          f"{len(data['rounds'])} round inputs, {FIXTURE.stat().st_size} bytes, python-raises {nulls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
