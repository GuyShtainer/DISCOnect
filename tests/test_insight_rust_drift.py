"""The Rust port of ``insight.py`` (``disconect-core/src/read``) has not drifted from its oracle.

Constants, field names and order, reason codes and the two prose strings are read out of the Rust source
and compared with the Python module they port; the numerics differential fixture is the committed output of
``gen_facts_numerics.py``. Values and behaviour are the job of ``tools/serve_diff.py`` and the Rust parity tests.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import statistics

import monorepo

monorepo.require()
import gen_facts_numerics  # noqa: E402
from disconect import insight  # noqa: E402

CRATE = monorepo.CRATE
INSIGHT_RS = (CRATE / "src" / "read" / "insight.rs").read_text()
NUMERICS_RS = (CRATE / "src" / "read" / "numerics.rs").read_text()


def _rust_const(name: str) -> str:
    found = re.search(rf"pub const {name}: [\w&']+ = ([^;]+);", INSIGHT_RS)
    assert found, f"{name} is not a pub const of insight.rs"
    return found.group(1).strip('"')


def _rust_fields(struct: str) -> list[str]:
    body = INSIGHT_RS.split(f"pub struct {struct} {{", 1)[1].split("}", 1)[0]
    return re.findall(r"pub (\w+):", body)


def test_the_caps_defaults_and_minimum_are_the_pythons():
    for name in ("DEFAULT_WINDOW_DAYS", "DEFAULT_BASELINE_DAYS", "MAX_WINDOW_DAYS", "MAX_BASELINE_DAYS",
                 "MIN_BASELINE_DAYS"):
        assert int(_rust_const(name)) == getattr(insight, name), name
    assert _rust_const("INSUFFICIENT") == insight.INSUFFICIENT


def test_fact_and_comparison_field_names_and_order_are_what_asdict_emits():
    assert _rust_fields("Fact") == [f.name for f in dataclasses.fields(insight.Fact)]
    assert _rust_fields("Comparison") == [f.name for f in dataclasses.fields(insight.Comparison)]


def test_reason_codes_prose_and_the_epsilon_are_in_both_sources():
    python_source = inspect.getsource(insight)
    for literal in ("no_data_in_window", "no_baseline", "baseline_too_thin", "unchanged", "higher", "lower",
                    "mean of the window's daily values", "mean of per-day means of samples",
                    "window_dates", "baseline_from", "baseline_to", "baseline_days_with_data", "aggregation"):
        assert f'"{literal}"' in python_source and (f'"{literal}"' in INSIGHT_RS or f"pub {literal}:" in INSIGHT_RS), literal
    assert "abs(delta) < 1e-9" in python_source and "delta.abs() < 1e-9" in INSIGHT_RS
    assert 'reason_code="ok"' in python_source and 'fact.reason_code = "ok"' in INSIGHT_RS


def test_the_square_root_width_is_the_statistics_modules():
    found = re.search(r"const SQRT_BIT_WIDTH: i64 = (\d+);", NUMERICS_RS)
    assert found and int(found.group(1)) == statistics._sqrt_bit_width


def test_the_numerics_fixture_is_what_the_generator_makes_today():
    committed = gen_facts_numerics.FIXTURE.read_text()
    assert committed == gen_facts_numerics.render(gen_facts_numerics.build()), (
        "regenerate: python tests/gen_facts_numerics.py")
    data = gen_facts_numerics.json.loads(committed)
    assert sum(len(v["v"]) // 16 for v in data["vectors"]) >= 100_000
