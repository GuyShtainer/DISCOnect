"""Decode Garmin wellness FIT files into contract-shaped facts.

Parser: fitdecode (MIT). Never Garmin's FIT SDK -- its license forbids use in
this project (docs/kb/14-prior-art.md). Message and field names below are the
public FIT profile names fitdecode exposes; the mapping to metrics follows the
verified signal catalogue in the learn skill's garmin-health-data-model.md.

Rules the decoder enforces:

* negative ``stress_level_value`` / ``respiration_rate`` are sentinels
  (-1 not worn, -2 unmeasurable), dropped and counted, never stored;
* ``spo2_data`` rows in ``off_wrist`` mode are dropped;
* ``monitoring`` records with only ``timestamp_16`` get their full timestamp
  from the last full timestamp in the stream (FIT's compressed-timestamp rule);
* a ``sleep_level`` record's timestamp is the *end* of the stage it names
  (verified against Garmin Connect's own stage totals on a real corpus: exact
  on every night for deep and awake). The first stage begins at the sleep-start
  event; without that event its start is unknown and it is dropped, not guessed;
* undocumented messages are counted, not decoded. The raw bytes are retained
  by the writer, so they can be decoded later by a replay.
"""

from __future__ import annotations

import datetime
import io
import warnings

import fitdecode

from disconect.ingest.model import (Activity, ClockOffset, DailyFact, DailyLabel, Decoded,
                                     MonitoringInterval, Sample, SleepSession, SleepStage)

UTC = datetime.timezone.utc
FIT_EPOCH = datetime.datetime(1989, 12, 31, tzinfo=UTC)

#: ``event.event`` code that brackets a night in the sleep files (file type 49).
#: Not in fitdecode's profile. Inferred from a real corpus: every sleep file
#: with a ``sleep_assessment`` carries exactly one start and one stop event
#: with this code, and their times match Garmin Connect's sleep window.
SLEEP_EVENT_CODE = "74"

#: ``stress_level`` (message 227) field number 3: the watch's own body-energy gauge, 0–100,
#: one sample per minute, written on sentinel frames too. Not in fitdecode's profile.
#: Provenance (documented reverse engineering, black-box): the message number was already
#: public knowledge (docs/kb/16); the field number and meaning were found by correlating the
#: user's own files with his own account export (fit-lab/fit_fieldscan.py, 2026-10-02, one
#: fenix 8, 201 monitoring files, 21 labelled days). No source listing this field's number
#: was opened for the decode. Observed: daily maximum equal to the vendor's daily high on
#: 100 % of days checked, minimum equal to the low on 90 % (±2 on the rest), sums of positive /
#: negative minute steps within ±5 of the vendor's charged / drained on 95 % / 100 %; integer,
#: 60 s cadence, at most 3 points between consecutive minutes, rises across all 20 device sleep
#: sessions, correlation with the stress value −0.20. Scope: one watch model, one firmware.
ENERGY_FIELD_NUM = 3
ENERGY_FIELD_NAME = "unknown_3"

#: Wellness file types (``file_id.type``) this decoder understands. Others
#: (sport settings, undocumented) are retained raw and only counted.
#: ``monitoring.cycles`` carries steps (scale 1) for these activity types and half-steps
#: (scale 2) otherwise — the FIT profile's subfield rule, applied here when the parser does not.
STEP_ACTIVITY_TYPES = ("walking", "running")

FILE_TYPE_STREAMS = {
    "monitoring_b": "fit:monitoring_b",
    "49": "fit:sleep",
    "68": "fit:hrv",
    "73": "fit:skin_temp",
    "44": "fit:metrics",
    "activity": "fit:activity",
}

#: Decode failures are recorded per file, never allowed to abort an import: hostile or corrupt
#: bytes can make fitdecode raise anything (FitError, struct.error, even AssertionError), so
#: :func:`decode_fit` catches every exception and reports one stable kind.

_SLEEP_ASSESSMENT_FIELDS = {
    "overall_sleep_score": "overall_score",
    "sleep_quality_score": "quality_score",
    "sleep_duration_score": "duration_score",
    "sleep_recovery_score": "recovery_score",
    "deep_sleep_score": "deep_score",
    "rem_sleep_score": "rem_score",
    "light_sleep_score": "light_score",
    "awake_time_score": "awake_time_score",
    "awakenings_count_score": "awakenings_count_score",
    "combined_awake_score": "combined_awake_score",
    "sleep_restlessness_score": "restlessness_score",
    "interruptions_score": "interruptions_score",
    "awakenings_count": "awakenings_count",
    "average_stress_during_sleep": "avg_stress",
}

_HRV_SUMMARY_FIELDS = {
    "weekly_average": "hrv_weekly_average",
    "last_night_average": "hrv_last_night_average",
    "last_night_5_min_high": "hrv_last_night_5min_high",
    "baseline_low_upper": "hrv_baseline_low_upper",
    "baseline_balanced_lower": "hrv_baseline_balanced_lower",
    "baseline_balanced_upper": "hrv_baseline_balanced_upper",
}

_SKIN_TEMP_FIELDS = {
    "nightly_value": "skin_temp_nightly",
    "average_deviation": "skin_temp_deviation",
    "average_7_day_deviation": "skin_temp_7day_deviation",
}


class FitDecodeError(Exception):
    """The file could not be decoded; ``kind`` is a stable class for provenance.

    ``stream``, ``start_utc`` and ``end_utc`` carry whatever the decoder had
    learned before failing (the header's file type, the timestamps seen), so
    a failure can still be placed on the coverage ledger. Each is None when
    the bytes broke before stating it.
    """

    def __init__(self, kind: str, message: str, stream: str | None = None,
                 start_utc: datetime.datetime | None = None, end_utc: datetime.datetime | None = None):
        self.kind = kind
        self.stream = stream
        self.start_utc = start_utc
        self.end_utc = end_utc
        super().__init__(message)


class _SafeProcessor(fitdecode.DefaultDataProcessor):
    """fitdecode 0.11's ``hr`` handler assumes a scalar ``event_timestamp`` and
    raises TypeError on array values seen in real files. Leave arrays raw."""

    def process_message_hr(self, reader, data_message) -> None:  # noqa: D102 - upstream hook
        if not data_message.has_field(fitdecode.profile.FIELD_NUM_HR_EVENT_TIMESTAMP_12):
            return
        for field_data in data_message.get_fields(fitdecode.profile.FIELD_NUM_HR_EVENT_TIMESTAMP):
            if isinstance(field_data.value, (int, float)):
                field_data.value = datetime.datetime.fromtimestamp(
                    fitdecode.FIT_UTC_REFERENCE + field_data.value, UTC)
                field_data.units = None


def _is_dev(field) -> bool:
    return bool(getattr(getattr(field, "field_def", None), "is_dev", False))


def _value(frame: fitdecode.FitDataMessage, name: str):
    """The first *native* field called ``name``. A developer field (a Connect IQ app's own data)
    may reuse a profile name such as ``heart_rate``; it is somebody else's data and never read."""
    for field in frame.fields:
        if field.is_named(name) and not _is_dev(field):   # fitdecode's own rule: name, subfield or parent
            return field.value
    return None


def _datetime(frame: fitdecode.FitDataMessage, name: str) -> datetime.datetime | None:
    value = _value(frame, name)
    return value if isinstance(value, datetime.datetime) else None


def _text(value) -> str | None:
    return None if value is None else str(value)


def _raw_field(frame: fitdecode.FitDataMessage, def_num: int, name: str):
    """The raw value of a field fitdecode's profile does not name, by number or ``unknown_N``."""
    for field in frame.fields:
        definition = getattr(field, "field_def", None)
        if _is_dev(field):
            continue  # a developer field with the same number is somebody else's data
        if (definition is not None and getattr(definition, "def_num", None) == def_num) or field.name == name:
            return field.raw_value
    return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value) -> int | None:
    number = _number(value)
    return None if number is None else int(round(number))


def expand_timestamp_16(timestamp_16: int, last_full: datetime.datetime) -> datetime.datetime:
    """Rebuild a full timestamp from FIT's 16-bit compressed form.

    The low 16 bits are relative to the last full timestamp in the stream. The
    delta is read as signed so a reference written slightly *after* the sample
    (an event marker) does not push the sample eighteen hours into the future.
    """
    base = int((last_full - FIT_EPOCH).total_seconds())
    delta = (int(timestamp_16) - (base & 0xFFFF)) & 0xFFFF
    if delta > 0x7FFF:
        delta -= 0x10000
    return last_full + datetime.timedelta(seconds=delta)


class _FitDecoder:
    def __init__(self) -> None:
        self.out = Decoded(stream="fit:unknown", source_scope="device")
        self.file_type: str | None = None
        self.last_full: datetime.datetime | None = None
        self.sleep_levels: list[tuple[datetime.datetime, str]] = []
        self.sleep_start: datetime.datetime | None = None
        self.sleep_end: datetime.datetime | None = None
        self.assessment: dict[str, float | int | None] | None = None
        self.seen_min: datetime.datetime | None = None
        self.seen_max: datetime.datetime | None = None

    # ---- bookkeeping ----
    def _touch(self, moment: datetime.datetime) -> None:
        if self.seen_min is None or moment < self.seen_min:
            self.seen_min = moment
        if self.seen_max is None or moment > self.seen_max:
            self.seen_max = moment

    def _offset(self, ts_utc: datetime.datetime | None, local: datetime.datetime | None) -> None:
        """A (timestamp, local_timestamp) pair states the watch's UTC offset."""
        if ts_utc is None or local is None:
            return
        offset = int((local.replace(tzinfo=UTC) - ts_utc).total_seconds())
        self.out.offsets.append(ClockOffset(ts_utc, offset))

    # ---- per-message handlers ----
    def file_id(self, frame) -> None:
        self.file_type = _text(_value(frame, "type"))
        self.out.stream = FILE_TYPE_STREAMS.get(self.file_type or "", f"fit:{self.file_type}")
        serial = _value(frame, "serial_number")
        self.out.device_id = _text(serial)
        created = _datetime(frame, "time_created")
        if created is not None:
            self._touch(created)

    def monitoring_info(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        self._offset(ts_utc, _datetime(frame, "local_timestamp"))
        rmr = _number(_value(frame, "resting_metabolic_rate"))
        if ts_utc is not None and rmr is not None and rmr > 0:
            self.out.daily.append(DailyFact("resting_metabolic_rate", rmr, ts_utc=ts_utc))

    def monitoring(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        if ts_utc is None:
            ts16 = _value(frame, "timestamp_16")
            if ts16 is None or self.last_full is None:
                self.out.drop("monitoring_without_timestamp")
                return
            ts_utc = expand_timestamp_16(int(ts16), self.last_full)
            if abs((ts_utc - self.last_full).total_seconds()) > 3600:
                self.out.drop("timestamp_16_far_from_reference")
        self._touch(ts_utc)
        heart_rate = _number(_value(frame, "heart_rate"))
        if heart_rate is not None:
            if heart_rate > 0:
                self.out.samples.append(Sample("heart_rate", ts_utc, heart_rate))
            else:
                self.out.drop("heart_rate_zero")
        activity_type = _text(_value(frame, "activity_type"))
        steps, cycles = _int(_value(frame, "steps")), _number(_value(frame, "cycles"))
        if steps is None and cycles is not None and activity_type in STEP_ACTIVITY_TYPES:
            # fitdecode resolves the ``steps`` subfield of ``cycles`` only when ``activity_type``
            # is a native field; when it arrives through the composite field 24 the record keeps
            # ``cycles`` (scale 2, half-steps). The raw field value is the step count either way,
            # and a resolved record reports it under both names (observed on the export corpus,
            # Bet 11 slice 0: 117 such records in 424 files; see docs/kb/21).
            raw = _raw_field(frame, 3, "cycles")
            if isinstance(raw, int) and not isinstance(raw, bool):
                steps, cycles = raw, float(raw)
        counters = {
            "steps": steps,
            "cycles": cycles,
            "active_time_s": _number(_value(frame, "active_time")),
            "active_calories_kcal": _int(_value(frame, "active_calories")),
            "distance_m": _number(_value(frame, "distance")),
        }
        if activity_type is not None and any(v is not None for v in counters.values()):
            self.out.intervals.append(MonitoringInterval(
                ts_utc=ts_utc, activity_type=activity_type,
                intensity=_int(_value(frame, "intensity")), **counters))

    def monitoring_hr_data(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        if ts_utc is None:
            return
        for field, metric in (("resting_heart_rate", "resting_heart_rate"),
                              ("current_day_resting_heart_rate", "resting_heart_rate_current_day")):
            value = _number(_value(frame, field))
            if value is not None and value > 0:
                self.out.daily.append(DailyFact(metric, value, ts_utc=ts_utc))

    def stress_level(self, frame) -> None:
        ts_utc = _datetime(frame, "stress_level_time")
        if ts_utc is None:
            return
        energy = _raw_field(frame, ENERGY_FIELD_NUM, ENERGY_FIELD_NAME)
        if isinstance(energy, int) and 0 <= energy <= 100:
            self._touch(ts_utc)
            self.out.samples.append(Sample("energy_reserve", ts_utc, float(energy)))
        value = _number(_value(frame, "stress_level_value"))
        if value is None:
            return
        if value < 0:
            self.out.drop("stress_sentinel")
            return
        self._touch(ts_utc)
        self.out.samples.append(Sample("stress", ts_utc, value))

    def respiration_rate(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        value = _number(_value(frame, "respiration_rate"))
        if ts_utc is None or value is None:
            return
        if value < 0:
            self.out.drop("respiration_sentinel")
            return
        self.out.samples.append(Sample("respiration_rate", ts_utc, value))

    def spo2_data(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        value = _number(_value(frame, "reading_spo2"))
        if ts_utc is None or value is None:
            return
        if _text(_value(frame, "mode")) == "off_wrist" or value <= 0:
            self.out.drop("spo2_off_wrist_or_zero")
            return
        self.out.samples.append(Sample("spo2", ts_utc, value))

    def sleep_level(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        level = _text(_value(frame, "sleep_level"))
        if ts_utc is None or level is None:
            return
        self._touch(ts_utc)
        self.sleep_levels.append((ts_utc, level))

    def sleep_assessment(self, frame) -> None:
        self.assessment = {column: _value(frame, field)
                           for field, column in _SLEEP_ASSESSMENT_FIELDS.items()}

    def event(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        if ts_utc is None:
            return
        if self.file_type == "49" and _text(_value(frame, "event")) == SLEEP_EVENT_CODE:
            kind = _text(_value(frame, "event_type"))
            if kind == "start":
                self.sleep_start = ts_utc
            elif kind == "stop":
                self.sleep_end = ts_utc

    def hrv_status_summary(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        if ts_utc is None:
            return
        self._touch(ts_utc)
        for field, metric in _HRV_SUMMARY_FIELDS.items():
            value = _number(_value(frame, field))
            if value is not None:
                self.out.daily.append(DailyFact(metric, value, ts_utc=ts_utc))
        status = _text(_value(frame, "status"))
        if status is not None:
            self.out.labels.append(DailyLabel("hrv_status", status, ts_utc=ts_utc))

    def hrv_value(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        value = _number(_value(frame, "value"))
        if ts_utc is None or value is None:
            return
        self.out.samples.append(Sample("hrv_rmssd", ts_utc, value))

    def skin_temp_overnight(self, frame) -> None:
        ts_utc = _datetime(frame, "timestamp")
        if ts_utc is None:
            return
        self._touch(ts_utc)
        self._offset(ts_utc, _datetime(frame, "local_timestamp"))
        for field, metric in _SKIN_TEMP_FIELDS.items():
            value = _number(_value(frame, field))
            if value is not None:
                self.out.daily.append(DailyFact(metric, value, ts_utc=ts_utc))

    def max_met_data(self, frame) -> None:
        ts_utc = _datetime(frame, "update_time")
        vo2max = _number(_value(frame, "vo2_max"))
        if ts_utc is None or vo2max is None or vo2max <= 0:
            return
        self._touch(ts_utc)
        self.out.daily.append(DailyFact("vo2max", vo2max, ts_utc=ts_utc))
        sport = _text(_value(frame, "sport"))
        if sport is not None:
            self.out.labels.append(DailyLabel("vo2max_sport", sport, ts_utc=ts_utc))

    def timestamp_correlation(self, frame) -> None:
        self._offset(_datetime(frame, "timestamp"), _datetime(frame, "local_timestamp"))

    def session(self, frame) -> None:
        start = _datetime(frame, "start_time")
        if start is None:
            return
        self._touch(start)
        speed = _number(_value(frame, "enhanced_avg_speed"))
        if speed is None:
            speed = _number(_value(frame, "avg_speed"))
        self.out.activities.append(Activity(
            start_utc=start,
            end_utc=_datetime(frame, "timestamp"),
            sport=_text(_value(frame, "sport")),
            sub_sport=_text(_value(frame, "sub_sport")),
            total_timer_s=_number(_value(frame, "total_timer_time")),
            total_elapsed_s=_number(_value(frame, "total_elapsed_time")),
            distance_m=_number(_value(frame, "total_distance")),
            calories_kcal=_int(_value(frame, "total_calories")),
            avg_hr=_int(_value(frame, "avg_heart_rate")),
            max_hr=_int(_value(frame, "max_heart_rate")),
            avg_speed_mps=speed,
            total_ascent_m=_number(_value(frame, "total_ascent")),
            total_descent_m=_number(_value(frame, "total_descent")),
        ))

    HANDLERS = {
        "file_id": file_id, "monitoring_info": monitoring_info, "monitoring": monitoring,
        "monitoring_hr_data": monitoring_hr_data, "stress_level": stress_level,
        "respiration_rate": respiration_rate, "spo2_data": spo2_data, "sleep_level": sleep_level,
        "sleep_assessment": sleep_assessment, "event": event,
        "hrv_status_summary": hrv_status_summary, "hrv_value": hrv_value,
        "skin_temp_overnight": skin_temp_overnight, "max_met_data": max_met_data,
        "timestamp_correlation": timestamp_correlation, "session": session,
    }

    # ---- driving ----
    def feed(self, frame: fitdecode.FitDataMessage) -> None:
        counts = self.out.message_counts
        counts[frame.name] = counts.get(frame.name, 0) + 1
        full = _datetime(frame, "timestamp")
        if full is None:
            # Field 253 is the timestamp in every message, named or not; a compressed-timestamp
            # record after an undocumented message must still expand against it.
            raw = _raw_field(frame, 253, "unknown_253")
            if isinstance(raw, int) and raw >= 0x10000000:
                full = FIT_EPOCH + datetime.timedelta(seconds=raw)
        if full is not None:
            self.last_full = full
        handler = self.HANDLERS.get(frame.name)
        if handler is not None:
            handler(self, frame)

    def _finish_sleep(self) -> None:
        if not self.sleep_levels and self.assessment is None and self.sleep_start is None:
            return
        session = SleepSession(start_utc=self.sleep_start, end_utc=self.sleep_end)
        levels = sorted(self.sleep_levels)
        previous = session.start_utc
        for end, stage in levels:
            if previous is None:
                # No start event: the first stage's beginning is unknown.
                self.out.drop("sleep_stage_without_start")
            elif end > previous:
                session.stages.append(SleepStage(stage, previous, end))
            else:
                self.out.drop("sleep_stage_zero_length")
            previous = end
        totals: dict[str, int] = {}
        for stage in session.stages:
            totals[stage.stage] = totals.get(stage.stage, 0) + int(
                (stage.end_utc - stage.start_utc).total_seconds())
        if session.stages:
            session.deep_s = totals.get("deep", 0)
            session.light_s = totals.get("light", 0)
            session.rem_s = totals.get("rem", 0)
            session.awake_s = totals.get("awake", 0)
        if self.assessment is not None:
            for column, value in self.assessment.items():
                if value is None:
                    continue
                setattr(session, column, float(value) if column == "avg_stress" else int(value))
        if session.end_utc is None and session.stages:
            session.end_utc = session.stages[-1].end_utc
        if session.start_utc is None and session.stages:
            session.start_utc = session.stages[0].start_utc
        self.out.sleep = session

    def result(self) -> Decoded:
        self._finish_sleep()
        self.out.start_utc, self.out.end_utc = self.seen_min, self.seen_max
        if self.out.sleep is not None and self.out.sleep.end_utc is not None:
            self.out.end_utc = max(self.out.end_utc or self.out.sleep.end_utc, self.out.sleep.end_utc)
        return self.out


def decode_fit(data: bytes) -> Decoded:
    """Decode one FIT file's bytes. Raises :class:`FitDecodeError` if unreadable."""
    decoder = _FitDecoder()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            with fitdecode.FitReader(io.BytesIO(data), processor=_SafeProcessor()) as reader:
                for frame in reader:
                    if isinstance(frame, fitdecode.FitDataMessage):
                        decoder.feed(frame)
        except Exception as exc:  # noqa: BLE001 - hostile bytes raise anything (fitdecode asserts too)
            stream = decoder.out.stream if decoder.file_type is not None else None
            raise FitDecodeError("unrecognized_payload", f"{type(exc).__name__}: {exc}", stream,
                                 decoder.seen_min, decoder.seen_max) from exc
    result = decoder.result()
    result.warnings = [str(w.message) for w in caught]
    return result


def scan_clock_offsets(data: bytes) -> list[ClockOffset]:
    """Cheap first pass: only the (timestamp, local_timestamp) pairs a file states.

    Run over a whole batch before writing so every file's local dates resolve
    against offsets from every other file, whatever order they are processed in.
    """
    decoder = _FitDecoder()
    wanted = {"monitoring_info", "skin_temp_overnight", "timestamp_correlation"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data), processor=_SafeProcessor()) as reader:
                for frame in reader:
                    if isinstance(frame, fitdecode.FitDataMessage) and frame.name in wanted:
                        decoder.feed(frame)
    except Exception:  # noqa: BLE001 - the full decode reports the failure; this pass only collects
        return []
    return decoder.out.offsets
