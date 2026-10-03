#!/usr/bin/env python3
"""Generate the Rust ``fit`` module's oracle fixtures.

Synthetic mode (default): the decoder's own test scenarios, built with ``fit_builder``, written
as ``<name>.fit.bin`` + ``<name>.expected.json`` under
``projects/disconect-core/tests/fixtures/synthetic/`` (synthetic bytes, serial 42, no personal
data; ``test_core_parity.py`` fails when a regenerated set differs from the committed one).

Corpus mode: ``--corpus <folder of .fit> --out <folder>`` writes one ``<stem>.expected.json``
per real file into a scratch folder that the Rust crate's ignored test reads
(``DISCONECT_TEST_FIT_DIR`` / ``DISCONECT_TEST_FIT_EXPECTED``). Real bytes and their JSON never
enter the repo.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from decoded_json import decoded_to_json  # noqa: E402
from fit_builder import FIT_EPOCH, FitBuilder  # noqa: E402

from disconect.ingest import fit_wellness  # noqa: E402

UTC = datetime.timezone.utc
T0 = datetime.datetime(2025, 6, 15, 6, 0, tzinfo=UTC)
SYNTHETIC_DIR = pathlib.Path(__file__).resolve().parents[2] / "disconect-core" / "tests" / "fixtures" / "synthetic"


def _ts16(moment):
    return int((moment - FIT_EPOCH).total_seconds()) & 0xFFFF


def _m(minutes: float) -> datetime.timedelta:
    return datetime.timedelta(minutes=minutes)


def scenarios() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    t0 = T0

    b = FitBuilder("monitoring_b", serial=42)
    b.add("monitoring_info", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=3), resting_metabolic_rate=1650)
    b.add("monitoring", timestamp=t0, activity_type="walking", steps=100, active_time=120.0, distance=80.0)
    b.add("monitoring", timestamp_16=_ts16(t0 + _m(2)), heart_rate=61)
    b.add("monitoring", timestamp_16=_ts16(t0 + _m(4)), heart_rate=0)
    b.add("stress_level", stress_level_time=t0, stress_level_value=25, raw_uint8={3: 71})
    b.add("stress_level", stress_level_time=t0 + _m(1), stress_level_value=-1, raw_uint8={3: 72})
    b.add("stress_level", stress_level_time=t0 + _m(2), stress_level_value=-2, raw_uint8={3: 255})
    b.add("stress_level", stress_level_time=t0 + _m(3), stress_level_value=40, raw_uint8={3: 150})
    b.add("respiration_rate", timestamp=t0, respiration_rate=14.5)
    b.add("respiration_rate", timestamp=t0 + _m(1), respiration_rate=-1.0)
    b.add("spo2_data", timestamp=t0, reading_spo2=96, reading_confidence=20, mode="periodic")
    b.add("spo2_data", timestamp=t0 + _m(1), reading_spo2=90, reading_confidence=1, mode="off_wrist")
    b.add("monitoring_hr_data", timestamp=t0, resting_heart_rate=52, current_day_resting_heart_rate=54)
    out["monitoring_sentinels_counters"] = b.build()

    b = FitBuilder("monitoring_b", serial=42)
    b.add("monitoring", timestamp=t0, activity_type="walking", steps=105)
    b.add("monitoring", timestamp=t0 + _m(1), raw_uint8={24: 70}, cycles=52.5)
    b.add("monitoring", timestamp=t0 + _m(2), raw_uint8={24: 8}, cycles=52.5)
    b.add("monitoring", timestamp=t0 + _m(3), activity_type="running", steps=24)
    b.add("monitoring", timestamp=t0 + _m(4), activity_type="cycling", cycles=30.0)
    out["monitoring_cycles_resolution"] = b.build()

    b = FitBuilder("monitoring_b", serial=42)
    b.add("monitoring", timestamp=t0, activity_type="walking", steps=10)
    b.add("monitoring", timestamp_16=_ts16(t0 - _m(2)), heart_rate=70)           # slightly backward
    b.add("monitoring", timestamp_16=_ts16(t0 + datetime.timedelta(hours=9)), heart_rate=72)  # far forward
    b.add("monitoring", timestamp_16=_ts16(t0 + _m(1)), heart_rate=73)
    out["monitoring_timestamp16"] = b.build()

    start = t0.replace(hour=21)
    b = FitBuilder("49", created=start)
    b.add("event", timestamp=start, event=74, event_type="start")
    b.add("sleep_level", timestamp=start + _m(30), sleep_level="light")
    b.add("sleep_level", timestamp=start + _m(90), sleep_level="deep")
    b.add("sleep_level", timestamp=start + _m(100), sleep_level="awake")
    b.add("sleep_level", timestamp=start + _m(100), sleep_level="rem")
    b.add("event", timestamp=start + _m(100), event=74, event_type="stop")
    b.add("sleep_assessment", overall_sleep_score=81, deep_sleep_score=70, rem_sleep_score=60,
          light_sleep_score=75, awakenings_count=2, average_stress_during_sleep=12.0)
    out["sleep_stage_end_semantics"] = b.build()

    b = FitBuilder("49")
    b.add("sleep_level", timestamp=t0, sleep_level="light")
    b.add("sleep_level", timestamp=t0 + _m(20), sleep_level="deep")
    out["sleep_without_start_event"] = b.build()

    b = FitBuilder("68")
    b.add("hrv_status_summary", timestamp=t0, weekly_average=52.5, last_night_average=48.0,
          last_night_5_min_high=70.0, status="balanced")
    b.add("hrv_value", timestamp=t0, value=51.0)
    b.add("hrv_value", timestamp=t0 + _m(5), value=53.0)
    out["hrv_status"] = b.build()

    b = FitBuilder("73")
    b.add("skin_temp_overnight", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=3),
          nightly_value=33.5, average_deviation=-0.2, average_7_day_deviation=0.1)
    out["skin_temp"] = b.build()

    b = FitBuilder("44")
    b.add("max_met_data", update_time=t0, vo2_max=45.3, sport="running")
    out["metrics_vo2max"] = b.build()

    b = FitBuilder("activity")
    b.add("session", start_time=t0, timestamp=t0 + _m(30), sport="running", sub_sport="generic",
          total_timer_time=1750.0, total_elapsed_time=1800.0, total_distance=5000.0, total_calories=350,
          avg_heart_rate=150, max_heart_rate=175, enhanced_avg_speed=2.85)
    b.add("timestamp_correlation", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=3))
    out["activity_session"] = b.build()

    b = FitBuilder("79")
    b.add("monitoring_info", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=2))
    out["unknown_type_counted_only"] = b.build()

    b = FitBuilder("monitoring_b", serial=42)
    b.describe_dev_field(0, "heart_rate", "bpm")
    b.add("monitoring", timestamp=t0, heart_rate=61)
    b.add("monitoring", timestamp=t0 + _m(1), dev_uint8={0: 199})
    b.add("monitoring", timestamp=t0 + _m(2), heart_rate=63, dev_uint8={0: 200})
    out["developer_field_named_heart_rate"] = b.build()
    return out


def expected_for(data: bytes) -> dict:
    try:
        return {"ok": decoded_to_json(fit_wellness.decode_fit(data))}
    except fit_wellness.FitDecodeError as exc:
        return {"error": {"kind": exc.kind, "stream": exc.stream,
                          "start_utc": exc.start_utc and exc.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "end_utc": exc.end_utc and exc.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}}


def write_synthetic(target: pathlib.Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for name, data in scenarios().items():
        (target / f"{name}.fit.bin").write_bytes(data)
        (target / f"{name}.expected.json").write_text(json.dumps(expected_for(data), indent=1, sort_keys=True) + "\n")
        names.append(name)
    garbage = b"not a fit file at all"
    (target / "garbage.fit.bin").write_bytes(garbage)
    (target / "garbage.expected.json").write_text(json.dumps(expected_for(garbage), indent=1, sort_keys=True) + "\n")
    return names + ["garbage"]


def write_corpus(corpus: pathlib.Path, out: pathlib.Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(corpus.glob("*.fit")):
        (out / f"{path.stem}.expected.json").write_text(json.dumps(expected_for(path.read_bytes()), sort_keys=True))
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--synthetic-dir", type=pathlib.Path, default=SYNTHETIC_DIR)
    args = parser.parse_args(argv)
    if args.corpus:
        print(f"{write_corpus(args.corpus, args.out)} expected files written to {args.out}")
    else:
        names = write_synthetic(args.synthetic_dir)
        print(f"{len(names)} synthetic fixtures written to {args.synthetic_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
