"""Decoder behaviour on synthetic FIT files built through fitdecode's profile."""

import datetime

from fit_builder import FIT_EPOCH, FitBuilder

from disconect.ingest import fit_wellness
from disconect.ingest.fit_wellness import decode_fit, expand_timestamp_16

UTC = datetime.timezone.utc


def _ts16(moment):
    return int((moment - FIT_EPOCH).total_seconds()) & 0xFFFF


def test_monitoring_file_samples_sentinels_and_counters(t0):
    b = FitBuilder("monitoring_b", serial=42)
    b.add("monitoring_info", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=3),
          resting_metabolic_rate=1650)
    b.add("monitoring", timestamp=t0, activity_type="walking", steps=100, active_time=120.0, distance=80.0)
    b.add("monitoring", timestamp_16=_ts16(t0 + datetime.timedelta(minutes=2)), heart_rate=61)
    b.add("monitoring", timestamp_16=_ts16(t0 + datetime.timedelta(minutes=4)), heart_rate=0)
    b.add("stress_level", stress_level_time=t0, stress_level_value=25, raw_uint8={3: 71})
    b.add("stress_level", stress_level_time=t0 + datetime.timedelta(minutes=1), stress_level_value=-1,
          raw_uint8={3: 72})
    b.add("stress_level", stress_level_time=t0 + datetime.timedelta(minutes=2), stress_level_value=-2,
          raw_uint8={3: 255})
    b.add("stress_level", stress_level_time=t0 + datetime.timedelta(minutes=3), stress_level_value=40,
          raw_uint8={3: 150})
    b.add("respiration_rate", timestamp=t0, respiration_rate=14.5)
    b.add("respiration_rate", timestamp=t0 + datetime.timedelta(minutes=1), respiration_rate=-1.0)
    b.add("spo2_data", timestamp=t0, reading_spo2=96, reading_confidence=20, mode="periodic")
    b.add("spo2_data", timestamp=t0 + datetime.timedelta(minutes=1), reading_spo2=90,
          reading_confidence=1, mode="off_wrist")
    b.add("monitoring_hr_data", timestamp=t0, resting_heart_rate=52, current_day_resting_heart_rate=54)
    decoded = decode_fit(b.build())

    assert decoded.stream == "fit:monitoring_b"
    assert decoded.source_scope == "device"
    assert decoded.device_id == "42"
    assert [(o.offset_s) for o in decoded.offsets] == [10800]
    samples = {(s.metric, s.ts_utc): s.value for s in decoded.samples}
    assert samples[("heart_rate", t0 + datetime.timedelta(minutes=2))] == 61
    assert samples[("stress", t0)] == 25
    # the gauge rides on field 3 of the same message, on sentinel frames too; 255 is out of range
    assert samples[("energy_reserve", t0)] == 71
    assert samples[("energy_reserve", t0 + datetime.timedelta(minutes=1))] == 72
    assert ("energy_reserve", t0 + datetime.timedelta(minutes=2)) not in samples, "255 is FIT's invalid marker"
    assert ("energy_reserve", t0 + datetime.timedelta(minutes=3)) not in samples, "150 is outside 0..100"
    assert samples[("stress", t0 + datetime.timedelta(minutes=3))] == 40
    assert samples[("respiration_rate", t0)] == 14.5
    assert samples[("spo2", t0)] == 96
    assert len(decoded.samples) == 7, "sentinels, off-wrist and zero HR must not become samples"
    assert decoded.dropped == {"heart_rate_zero": 1, "stress_sentinel": 2, "respiration_sentinel": 1,
                               "spo2_off_wrist_or_zero": 1}
    daily = {f.metric: f.value for f in decoded.daily}
    assert daily == {"resting_metabolic_rate": 1650, "resting_heart_rate": 52,
                     "resting_heart_rate_current_day": 54}
    assert [(i.activity_type, i.steps, i.distance_m) for i in decoded.intervals] == [("walking", 100, 80.0)]
    assert decoded.start_utc == t0 and decoded.end_utc == t0 + datetime.timedelta(minutes=4)


def test_timestamp_16_expands_forward_and_slightly_backward(t0):
    assert expand_timestamp_16(_ts16(t0 + datetime.timedelta(minutes=2)), t0) == t0 + datetime.timedelta(minutes=2)
    assert expand_timestamp_16(_ts16(t0 - datetime.timedelta(minutes=2)), t0) == t0 - datetime.timedelta(minutes=2)
    # nine hours later still expands forward (16 bits span 18 h)
    later = t0 + datetime.timedelta(hours=9)
    assert expand_timestamp_16(_ts16(later), t0) == later


def test_sleep_file_stage_end_semantics(t0):
    start = t0.replace(hour=21)                      # 00:00 local
    b = FitBuilder("49", created=start)
    b.add("event", timestamp=start, event=74, event_type="start")
    b.add("sleep_level", timestamp=start + datetime.timedelta(minutes=30), sleep_level="light")
    b.add("sleep_level", timestamp=start + datetime.timedelta(minutes=90), sleep_level="deep")
    b.add("sleep_level", timestamp=start + datetime.timedelta(minutes=100), sleep_level="awake")
    b.add("sleep_level", timestamp=start + datetime.timedelta(minutes=100), sleep_level="rem")
    b.add("event", timestamp=start + datetime.timedelta(minutes=100), event=74, event_type="stop")
    b.add("sleep_assessment", overall_sleep_score=81, deep_sleep_score=70, rem_sleep_score=60,
          light_sleep_score=75, awakenings_count=2, average_stress_during_sleep=12.0)
    decoded = decode_fit(b.build())

    sleep = decoded.sleep
    assert decoded.stream == "fit:sleep"
    assert sleep.start_utc == start and sleep.end_utc == start + datetime.timedelta(minutes=100)
    assert [(s.stage, int((s.end_utc - s.start_utc).total_seconds()) // 60) for s in sleep.stages] == [
        ("light", 30), ("deep", 60), ("awake", 10)]
    assert decoded.dropped == {"sleep_stage_zero_length": 1}
    assert (sleep.light_s, sleep.deep_s, sleep.awake_s, sleep.rem_s) == (1800, 3600, 600, 0)
    assert sleep.overall_score == 81 and sleep.awakenings_count == 2 and sleep.avg_stress == 12.0
    assert sleep.quality_score is None, "a score the file did not state stays None"


def test_sleep_file_without_start_event_drops_first_stage(t0):
    b = FitBuilder("49")
    b.add("sleep_level", timestamp=t0, sleep_level="light")
    b.add("sleep_level", timestamp=t0 + datetime.timedelta(minutes=20), sleep_level="deep")
    decoded = decode_fit(b.build())
    assert [s.stage for s in decoded.sleep.stages] == ["deep"]
    assert decoded.dropped == {"sleep_stage_without_start": 1}
    assert decoded.sleep.start_utc == t0


def test_hrv_skin_temp_and_metrics_files(t0):
    b = FitBuilder("68")
    b.add("hrv_status_summary", timestamp=t0, weekly_average=52.5, last_night_average=48.0,
          last_night_5_min_high=70.0, status="balanced")
    b.add("hrv_value", timestamp=t0, value=51.0)
    b.add("hrv_value", timestamp=t0 + datetime.timedelta(minutes=5), value=53.0)
    hrv = decode_fit(b.build())
    assert hrv.stream == "fit:hrv"
    assert {f.metric: f.value for f in hrv.daily} == {"hrv_weekly_average": 52.5, "hrv_last_night_average": 48.0,
                                                      "hrv_last_night_5min_high": 70.0}
    assert [(l.metric, l.label) for l in hrv.labels] == [("hrv_status", "balanced")]
    assert [s.value for s in hrv.samples] == [51.0, 53.0]

    b = FitBuilder("73")
    b.add("skin_temp_overnight", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=3),
          nightly_value=33.5, average_deviation=-0.2, average_7_day_deviation=0.1)
    skin = decode_fit(b.build())
    assert skin.stream == "fit:skin_temp"
    assert {f.metric: round(f.value, 2) for f in skin.daily} == {"skin_temp_nightly": 33.5,
                                                                 "skin_temp_deviation": -0.2,
                                                                 "skin_temp_7day_deviation": 0.1}
    assert skin.offsets[0].offset_s == 10800

    b = FitBuilder("44")
    b.add("max_met_data", update_time=t0, vo2_max=45.3, sport="running")
    metrics = decode_fit(b.build())
    assert metrics.stream == "fit:metrics"
    assert [(f.metric, round(f.value, 1)) for f in metrics.daily] == [("vo2max", 45.3)]
    assert [(l.metric, l.label) for l in metrics.labels] == [("vo2max_sport", "running")]


def test_activity_file_session(t0):
    b = FitBuilder("activity")
    b.add("session", start_time=t0, timestamp=t0 + datetime.timedelta(minutes=30), sport="running",
          sub_sport="generic", total_timer_time=1750.0, total_elapsed_time=1800.0,
          total_distance=5000.0, total_calories=350, avg_heart_rate=150, max_heart_rate=175,
          enhanced_avg_speed=2.85)
    decoded = decode_fit(b.build())
    assert decoded.stream == "fit:activity"
    [activity] = decoded.activities
    assert (activity.sport, activity.distance_m, activity.avg_hr, activity.max_hr) == ("running", 5000.0, 150, 175)
    assert activity.end_utc == t0 + datetime.timedelta(minutes=30)
    assert round(activity.avg_speed_mps, 2) == 2.85


def test_garbage_raises_decode_error():
    try:
        decode_fit(b"not a fit file at all")
    except fit_wellness.FitDecodeError as exc:
        assert exc.kind == "unrecognized_payload"
    else:
        raise AssertionError("garbage must not decode silently")


def test_unknown_messages_are_counted_not_decoded(t0):
    b = FitBuilder("79")
    b.add("timestamp_correlation", timestamp=t0, local_timestamp=t0 + datetime.timedelta(hours=2))
    decoded = decode_fit(b.build())
    assert decoded.stream == "fit:79"
    assert decoded.message_counts == {"file_id": 1, "timestamp_correlation": 1}
    assert decoded.offsets[0].offset_s == 7200


def test_cycles_resolve_to_steps_when_activity_type_comes_through_the_composite_field(t0):
    """Bet 11 slice 0: the watch often writes ``activity_type`` only inside field 24
    (``current_activity_type_intensity``); fitdecode then leaves field 3 as ``cycles`` with
    half-step values. The decoder must report the same step count as a natively typed record."""
    b = FitBuilder("monitoring_b", serial=42)
    b.add("monitoring", timestamp=t0, activity_type="walking", steps=105)
    # 70 = activity_type walking (6) in the low five bits, intensity 2 in the top three
    b.add("monitoring", timestamp=t0 + datetime.timedelta(minutes=1), raw_uint8={24: 70}, cycles=52.5)
    b.add("monitoring", timestamp=t0 + datetime.timedelta(minutes=2), raw_uint8={24: 8}, cycles=52.5)
    decoded = decode_fit(b.build())

    native, composite, sedentary = decoded.intervals
    assert (native.activity_type, native.steps, native.cycles) == ("walking", 105, 105.0)
    assert (composite.activity_type, composite.steps, composite.cycles, composite.intensity) == ("walking", 105, 105.0, 2)
    # a type without the steps subfield keeps the profile's half-step cycles and no step count
    assert (sedentary.activity_type, sedentary.steps, sedentary.cycles) == ("sedentary", None, 52.5)


def test_developer_field_with_a_profile_name_is_never_read(t0):
    """A Connect IQ app can name its own field ``heart_rate``; it is not the watch's reading."""
    b = FitBuilder("monitoring_b", serial=42)
    b.describe_dev_field(0, "heart_rate", "bpm")
    b.add("monitoring", timestamp=t0, heart_rate=61)
    b.add("monitoring", timestamp=t0 + datetime.timedelta(minutes=1), dev_uint8={0: 199})
    b.add("monitoring", timestamp=t0 + datetime.timedelta(minutes=2), heart_rate=63, dev_uint8={0: 200})
    decoded = decode_fit(b.build())
    assert [s.value for s in decoded.samples if s.metric == "heart_rate"] == [61.0, 63.0]


def test_hostile_bytes_never_escape_as_anything_but_a_decode_error():
    import random
    header = FitBuilder("monitoring_b").build()[:14]
    for seed in range(60):
        body = random.Random(seed).randbytes(300)
        try:
            decode_fit(header + body)
        except fit_wellness.FitDecodeError as exc:
            assert exc.kind == "unrecognized_payload"
    assert fit_wellness.scan_clock_offsets(header + b"\xff" * 50) == []
