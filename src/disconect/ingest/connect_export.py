"""Import a Garmin Connect account data export ("Export Your Data").

A one-time cloud pull the *user* performs, so a new user can backfill history
on day one. It is not an ongoing dependency. Format spec and traps:
the Connect account export format (not described in this repository). The bundled FIT corpus is decoded by
the same decoder as a USB pull (source_scope 'device'); the JSON daily figures
are Garmin's computed layer and land as 'vendor_cloud', side by side.

PII: filenames embed the account email and profile id. Labels used in reports
go through :func:`mask_label`; nothing else about a path is ever recorded.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import pathlib
import re
import zipfile
from collections.abc import Iterator

from disconect.ingest import live
from disconect.ingest.model import DailyFact, DailyLabel, Decoded, SleepSession
from disconect.ingest.writer import Writer

UTC = datetime.timezone.utc
EXPORT_ROOT = "DI_CONNECT"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_NUMBER = re.compile(r"\d{7,}")


def mask_label(name: str) -> str:
    """A filename safe to put in a report: email and long ids replaced."""
    return _LONG_NUMBER.sub("{id}", _EMAIL.sub("{email}", name))


def _parse_gmt(text: str | None) -> datetime.datetime | None:
    """Connect's ``YYYY-MM-DDTHH:MM:SS.0`` GMT strings (fractional part optional)."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        return datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _calendar_date(value) -> str | None:
    """``calendarDate`` is an ISO string in most files and epoch-ms in a few."""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    moment = _epoch_ms_to_utc(value)
    return moment.date().isoformat() if moment else None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value) -> int | None:
    number = _num(value)
    return None if number is None else int(round(number))


def _epoch_ms_to_utc(value) -> datetime.datetime | None:
    """An epoch-millisecond timestamp (int/float, as some Connect files use) as a UTC datetime."""
    number = _num(value)
    return None if number is None else datetime.datetime.fromtimestamp(number / 1000, UTC)


def _fact(decoded: Decoded, date: str, metric: str, value, *, minimum: float | None = None) -> None:
    number = _num(value)
    if number is None:
        return
    if minimum is not None and number < minimum:
        decoded.drop(f"{metric}_below_minimum")
        return
    decoded.daily.append(DailyFact(metric, number, date=date))


# ---- UDSFile: the daily spine ----

def decode_uds_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``UDSFile`` day -> (source_key, facts). None if the day has no date."""
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:uds", source_scope="vendor_cloud")
    decoded.start_utc = _parse_gmt(record.get("wellnessStartTimeGmt"))
    decoded.end_utc = _parse_gmt(record.get("wellnessEndTimeGmt"))
    if record.get("includesWellnessData") is False:
        decoded.drop("uds_day_without_wellness_data")
        return date, decoded
    _fact(decoded, date, "steps", record.get("totalSteps"))
    _fact(decoded, date, "distance", record.get("totalDistanceMeters"))
    _fact(decoded, date, "resting_heart_rate", record.get("restingHeartRate"), minimum=1)
    _fact(decoded, date, "resting_heart_rate_current_day", record.get("currentDayRestingHeartRate"),
          minimum=1)
    _fact(decoded, date, "heart_rate_min", record.get("minHeartRate"), minimum=1)
    _fact(decoded, date, "heart_rate_max", record.get("maxHeartRate"), minimum=1)
    _fact(decoded, date, "intensity_minutes_moderate", record.get("moderateIntensityMinutes"))
    _fact(decoded, date, "intensity_minutes_vigorous", record.get("vigorousIntensityMinutes"))
    _fact(decoded, date, "calories_total", record.get("totalKilocalories"))
    _fact(decoded, date, "calories_active", record.get("activeKilocalories"))
    _fact(decoded, date, "calories_bmr", record.get("bmrKilocalories"))
    _fact(decoded, date, "spo2_avg", record.get("averageSpo2Value"), minimum=1)
    _fact(decoded, date, "spo2_lowest", record.get("lowestSpo2Value"), minimum=1)
    stress = record.get("allDayStress") or {}
    for aggregate in stress.get("aggregatorList") or []:
        if isinstance(aggregate, dict) and aggregate.get("type") == "TOTAL":
            _fact(decoded, date, "stress_avg", aggregate.get("averageStressLevel"), minimum=0)
    respiration = record.get("respiration") or {}
    _fact(decoded, date, "respiration_avg_waking", respiration.get("avgWakingRespirationValue"), minimum=0)
    _fact(decoded, date, "respiration_lowest", respiration.get("lowestRespirationValue"), minimum=0)
    _fact(decoded, date, "respiration_highest", respiration.get("highestRespirationValue"), minimum=0)
    battery = record.get("bodyBattery") or {}
    _fact(decoded, date, "body_battery_charged", battery.get("chargedValue"))
    _fact(decoded, date, "body_battery_drained", battery.get("drainedValue"))
    for stat in battery.get("bodyBatteryStatList") or []:
        if not isinstance(stat, dict):
            continue
        kind = stat.get("bodyBatteryStatType")
        if kind == "HIGHEST":
            _fact(decoded, date, "body_battery_high", stat.get("statsValue"))
        elif kind == "LOWEST":
            _fact(decoded, date, "body_battery_low", stat.get("statsValue"))
    active_seconds = _num(record.get("activeSeconds"))
    if active_seconds is not None:
        _fact(decoded, date, "active_minutes", active_seconds / 60.0)
    highly_active_seconds = _num(record.get("highlyActiveSeconds"))
    if highly_active_seconds is not None:
        _fact(decoded, date, "highly_active_minutes", highly_active_seconds / 60.0)
    hydration = record.get("hydration") or {}
    _fact(decoded, date, "hydration_ml", hydration.get("valueInML"))
    _fact(decoded, date, "sweat_loss_ml", hydration.get("sweatLossInML"))
    return date, decoded


# ---- sleepData ----

_SLEEP_SCORE_FIELDS = {
    "overallScore": "overall_score", "qualityScore": "quality_score",
    "durationScore": "duration_score", "recoveryScore": "recovery_score",
    "deepScore": "deep_score", "remScore": "rem_score", "lightScore": "light_score",
    "awakeningsCountScore": "awakenings_count_score", "awakeTimeScore": "awake_time_score",
    "combinedAwakeScore": "combined_awake_score", "restfulnessScore": "restlessness_score",
    "interruptionsScore": "interruptions_score",
}


def decode_sleep_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``sleepData`` night -> (source_key, facts). None for the empty stubs."""
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:sleep", source_scope="vendor_cloud")
    session = SleepSession(
        start_utc=_parse_gmt(record.get("sleepStartTimestampGMT")),
        end_utc=_parse_gmt(record.get("sleepEndTimestampGMT")),
        date=date,
        deep_s=_int(record.get("deepSleepSeconds")),
        light_s=_int(record.get("lightSleepSeconds")),
        rem_s=_int(record.get("remSleepSeconds")),
        awake_s=_int(record.get("awakeSleepSeconds")),
        unmeasurable_s=_int(record.get("unmeasurableSeconds")),
        awakenings_count=_int(record.get("awakeCount")),
        avg_stress=_num(record.get("avgSleepStress")),
        avg_respiration=_num(record.get("averageRespiration")),
        lowest_respiration=_num(record.get("lowestRespiration")),
        highest_respiration=_num(record.get("highestRespiration")),
        retro=bool(record.get("retro", False)),
    )
    scores = record.get("sleepScores") or {}
    for field, column in _SLEEP_SCORE_FIELDS.items():
        setattr(session, column, _int(scores.get(field)))
    spo2 = record.get("spo2SleepSummary") or {}
    session.avg_spo2 = _num(spo2.get("averageSPO2"))
    session.lowest_spo2 = _int(spo2.get("lowestSPO2"))
    session.avg_hr = _num(spo2.get("averageHR"))
    if record.get("napList"):
        decoded.drop("naps_not_imported", len(record["napList"]))
    decoded.sleep = session
    decoded.start_utc, decoded.end_utc = session.start_utc, session.end_utc
    return date, decoded


# ---- TrainingReadinessDTO ----

_READINESS_FACTORS = {
    "sleepScoreFactorPercent": "readiness_factor_sleep",
    "recoveryTimeFactorPercent": "readiness_factor_recovery_time",
    "acwrFactorPercent": "readiness_factor_load_ratio",
    "stressHistoryFactorPercent": "readiness_factor_stress_history",
    "hrvFactorPercent": "readiness_factor_hrv",
    "sleepHistoryFactorPercent": "readiness_factor_sleep_history",
}
#: The morning reset is the day's canonical readiness; later contexts are
#: intra-day updates (post-exercise resets, realtime variable updates).
_MORNING_CONTEXT = "AFTER_WAKEUP_RESET"


def decode_readiness_records(records: list[dict]) -> Iterator[tuple[str, dict, Decoded]]:
    """All ``TrainingReadinessDTO`` records -> one raw record each; one daily value per date.

    The daily value is the morning reset when the day has one, else the latest
    update of the day. Duplicates across window files collapse on timestamp.
    """
    by_date: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for record in records:
        date = _calendar_date(record.get("calendarDate"))
        stamp = record.get("timestamp")
        if date is None or not isinstance(stamp, str) or stamp in seen:
            continue
        seen.add(stamp)
        by_date.setdefault(date, []).append(record)
    for date, day_records in by_date.items():
        morning = [r for r in day_records if r.get("inputContext") == _MORNING_CONTEXT]
        chosen = max(morning or day_records, key=lambda r: r.get("timestamp", ""))
        for record in day_records:
            decoded = Decoded(stream="json:readiness", source_scope="vendor_cloud")
            decoded.start_utc = decoded.end_utc = _parse_gmt(record.get("timestamp"))
            if record is chosen:
                _fact(decoded, date, "training_readiness", record.get("score"))
                for field, metric in _READINESS_FACTORS.items():
                    _fact(decoded, date, metric, record.get(field))
                _fact(decoded, date, "hrv_weekly_average", record.get("hrvWeeklyAverage"), minimum=0.1)
                _fact(decoded, date, "recovery_time", record.get("recoveryTime"))
                # training_load_acute is owned by MetricsAcuteTrainingLoad now (decode_training_load_record);
                # this endpoint's acuteLoad echo would just be a redundant, less-complete duplicate.
                level = record.get("level")
                if isinstance(level, str):
                    decoded.labels.append(DailyLabel("training_readiness_level", level.lower(), date=date))
                context = record.get("inputContext")
                if isinstance(context, str):
                    decoded.labels.append(DailyLabel("training_readiness_context", context.lower(), date=date))
            yield f"{date}|{record['timestamp']}", record, decoded


# ---- MetricsMaxMetData ----

def decode_maxmet_record(record: dict) -> tuple[str, Decoded] | None:
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:vo2max", source_scope="vendor_cloud")
    decoded.start_utc = decoded.end_utc = _parse_gmt(record.get("updateTimestamp"))
    _fact(decoded, date, "vo2max", record.get("vo2MaxValue"), minimum=0.1)
    sport = record.get("sport")
    if isinstance(sport, str):
        decoded.labels.append(DailyLabel("vo2max_sport", sport.lower(), date=date))
    return f"{date}|{record.get('updateTimestamp', '')}", decoded


# ---- MetricsAcuteTrainingLoad ----

def decode_training_load_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``MetricsAcuteTrainingLoad`` day -> (source_key, facts).

    ``calendarDate`` and ``timestamp`` are epoch-ms ints here, not the ISO
    strings most other Connect files use.
    """
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:training_load", source_scope="vendor_cloud")
    decoded.start_utc = decoded.end_utc = _epoch_ms_to_utc(record.get("timestamp"))
    _fact(decoded, date, "training_load_acute", record.get("dailyTrainingLoadAcute"))
    _fact(decoded, date, "training_load_chronic", record.get("dailyTrainingLoadChronic"))
    _fact(decoded, date, "training_load_ratio", record.get("dailyAcuteChronicWorkloadRatio"))
    status = record.get("acwrStatus")
    if isinstance(status, str):
        decoded.labels.append(DailyLabel("training_load_status", status.lower(), date=date))
    return f"{date}|{record.get('timestamp', '')}", decoded


# ---- EnduranceScore ----

def decode_endurance_score_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``EnduranceScore`` day -> (source_key, facts). Dates are epoch-ms, like HillScore."""
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:endurance", source_scope="vendor_cloud")
    decoded.start_utc = decoded.end_utc = _epoch_ms_to_utc(record.get("timestamp"))
    _fact(decoded, date, "endurance_score", record.get("overallScore"))
    return f"{date}|{record.get('timestamp', '')}", decoded


# ---- HillScore ----

def decode_hill_score_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``HillScore`` day -> (source_key, facts).

    Profiled on a real export: the only fields besides ids/timestamps are
    ``hillScoreClassificationId`` (an undocumented band) and
    ``hillScoreFeedbackPhraseId`` (a stable id for a feedback *phrase*, not a
    value -- see the KB doc's free-text-vs-phrase-id gotcha). Neither
    unambiguously names a numeric hill score, so nothing is imported; the
    record is still retained and counted so the gap stays visible rather than
    silently vanishing into "ignored".
    """
    date = _calendar_date(record.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:hill", source_scope="vendor_cloud")
    decoded.start_utc = decoded.end_utc = _epoch_ms_to_utc(record.get("timestamp"))
    decoded.drop("hill_score_fields_ambiguous")
    return f"{date}|{record.get('timestamp', '')}", decoded


# ---- fitnessAgeData ----

def decode_fitness_age_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``fitnessAgeData`` assessment -> (source_key, facts). Only currentBioAge is imported."""
    date = _calendar_date(record.get("asOfDateGmt"))
    if date is None:
        return None
    decoded = Decoded(stream="json:fitness_age", source_scope="vendor_cloud")
    decoded.start_utc = decoded.end_utc = _parse_gmt(record.get("asOfDateGmt"))
    _fact(decoded, date, "fitness_age", record.get("currentBioAge"))
    return f"{date}|{record.get('createTimestamp', '')}", decoded


# ---- userBioMetrics ----

def decode_bio_metrics_record(record: dict) -> tuple[str, Decoded] | None:
    """One ``userBioMetrics`` snapshot -> (source_key, facts). Weight only; height is skipped.

    Garmin stores weight in grams when the number is large; anything above
    1000 is converted to kilograms.
    """
    meta = record.get("metaData") or {}
    date = _calendar_date(meta.get("calendarDate"))
    if date is None:
        return None
    decoded = Decoded(stream="json:biometrics", source_scope="vendor_cloud")
    weight = record.get("weight") or {}
    decoded.start_utc = decoded.end_utc = _parse_gmt(weight.get("timestampGMT"))
    number = _num(weight.get("weight"))
    if number is not None:
        _fact(decoded, date, "weight_kg", number / 1000.0 if number > 1000 else number)
    return f"{date}|{record.get('version', '')}", decoded


#: Per-record decoders by ``raw_records.stream``: the one table both the importer
#: and ``reparse`` dispatch on, so a stream can never be importable but not replayable.
RECORD_DECODERS = {
    "json:uds": decode_uds_record,
    "json:sleep": decode_sleep_record,
    "json:vo2max": decode_maxmet_record,
    "json:training_load": decode_training_load_record,
    "json:endurance": decode_endurance_score_record,
    "json:hill": decode_hill_score_record,
    "json:fitness_age": decode_fitness_age_record,
    "json:biometrics": decode_bio_metrics_record,
    "json:live": live.decode_live_record,
}
#: Streams whose canonical value is decided across all their records at once.
BATCH_STREAMS = frozenset({"json:readiness"})


# ---- walking the export ----

class _Entry:
    """A file inside the export, whether on disk or inside a zip.

    ``written_day`` is the file's own modification date (UTC): the day the
    export was produced, which no claimed window can honestly run past.
    """

    def __init__(self, relpath: str, reader, written_day: str | None = None):
        self.relpath = relpath.replace("\\", "/")
        self._reader = reader
        self.written_day = written_day

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]

    def read(self) -> bytes:
        return self._reader()


def _iter_directory(root: pathlib.Path) -> Iterator[_Entry]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            written = datetime.datetime.fromtimestamp(path.stat().st_mtime, UTC).date().isoformat()
            yield _Entry(str(path.relative_to(root)), path.read_bytes, written)


def _iter_zip(archive: zipfile.ZipFile) -> Iterator[_Entry]:
    for info in sorted(archive.infolist(), key=lambda i: i.filename):
        if not info.is_dir():
            written = datetime.date(*info.date_time[:3]).isoformat() if info.date_time[0] >= 1980 else None
            yield _Entry(info.filename, lambda i=info: archive.read(i), written)


def _connect_section(relpath: str) -> str | None:
    """The DI_CONNECT sub-folder an entry belongs to, or None if outside DI_CONNECT."""
    parts = relpath.split("/")
    if EXPORT_ROOT in parts:
        index = parts.index(EXPORT_ROOT)
        return parts[index + 1] if len(parts) > index + 2 else None
    return None


def looks_like_export(path: pathlib.Path) -> bool:
    """True for an extracted export root, its DI_CONNECT folder, or the outer zip."""
    path = pathlib.Path(path)
    if path.is_dir():
        return path.name == EXPORT_ROOT or (path / EXPORT_ROOT).is_dir()
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return any(name.split("/")[0] == EXPORT_ROOT or f"/{EXPORT_ROOT}/" in name
                           for name in archive.namelist())
        except zipfile.BadZipFile:
            return False
    return False


def _entries(path: pathlib.Path) -> Iterator[_Entry]:
    if path.is_dir():
        root = path.parent if path.name == EXPORT_ROOT else path
        yield from _iter_directory(root)
    else:
        with zipfile.ZipFile(path) as archive:
            yield from _iter_zip(archive)


def _load_json(raw: bytes) -> list[dict] | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        return [data]
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else None


def collect_fit_members(path: pathlib.Path) -> Iterator[tuple[str, bytes]]:
    """Every ``.fit`` inside the export's nested ``UploadedFiles`` zips, as (label, bytes)."""
    for entry in _entries(pathlib.Path(path)):
        if _connect_section(entry.relpath) != "DI-Connect-Uploaded-Files" or not entry.name.lower().endswith(".zip"):
            continue
        with zipfile.ZipFile(io.BytesIO(entry.read())) as inner:
            for info in sorted(inner.infolist(), key=lambda i: i.filename):
                if not info.is_dir() and info.filename.lower().endswith(".fit"):
                    yield mask_label(info.filename), inner.read(info)


#: Which export file family feeds which stream: (section, name test, stream). A file
#: outside this table is counted as ignored. Keep in step with RECORD_DECODERS/BATCH_STREAMS.
FILE_FAMILIES: tuple[tuple[str, str, str, str], ...] = (
    ("DI-Connect-Aggregator", "prefix", "UDSFile_", "json:uds"),
    ("DI-Connect-Wellness", "suffix", "_sleepData.json", "json:sleep"),
    ("DI-Connect-Metrics", "prefix", "TrainingReadinessDTO_", "json:readiness"),
    ("DI-Connect-Metrics", "prefix", "MetricsMaxMetData_", "json:vo2max"),
    ("DI-Connect-Metrics", "prefix", "MetricsAcuteTrainingLoad_", "json:training_load"),
    ("DI-Connect-Metrics", "prefix", "EnduranceScore_", "json:endurance"),
    ("DI-Connect-Metrics", "prefix", "HillScore_", "json:hill"),
    ("DI-Connect-Wellness", "suffix", "_fitnessAgeData.json", "json:fitness_age"),
    ("DI-Connect-Wellness", "suffix", "_userBioMetrics.json", "json:biometrics"),
)
#: Why a record of a family produced nothing, by stream (counted in ``stats.dropped``).
_NO_DATE_REASON = {"json:uds": "uds_without_date", "json:sleep": "sleep_stub_without_date"}

_ISO_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")
_COMPACT_RANGE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})_(\d{4})(\d{2})(\d{2})(?!\d)")


def stream_for_entry(section: str | None, name: str) -> str | None:
    """The stream a JSON export file belongs to, or None if the importer does not read it."""
    if section is None or not name.lower().endswith(".json"):
        return None
    for family_section, how, needle, stream in FILE_FAMILIES:
        if section != family_section:
            continue
        if (how == "prefix" and name.startswith(needle)) or (how == "suffix" and name.endswith(needle)):
            return stream
    return None


def date_range_in_name(name: str) -> tuple[str, str] | None:
    """The ``(from_day, to_day)`` window an export file name states, if any.

    Windowed families name their period either as ``YYYY-MM-DD_YYYY-MM-DD``
    (UDSFile, sleepData) or as ``YYYYMMDD_YYYYMMDD`` (the Metrics families);
    the pair may sit anywhere in the name. Snapshot files (fitness age,
    biometrics) carry no window and return None. Bounds are checked to be
    real dates and ordered; anything else returns None rather than a guess.
    """
    match = _ISO_RANGE.search(name)
    if match:
        candidate = (match.group(1), match.group(2))
    else:
        match = _COMPACT_RANGE.search(name)
        if not match:
            return None
        groups = match.groups()
        candidate = ("-".join(groups[0:3]), "-".join(groups[3:6]))
    try:
        first, last = (datetime.date.fromisoformat(day) for day in candidate)
    except ValueError:
        return None
    return candidate if first <= last else None


def _noon_utc(day: str) -> datetime.datetime:
    """Midday UTC of a local date: the instant that resolves to that date under any offset within +/-12 h."""
    return datetime.datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)


def _import_entry(entry: _Entry, stream: str, writer: Writer) -> None:
    """Feed one recognised JSON file to ``writer``: its claimed window, then every record."""
    label = mask_label(entry.name)
    window = date_range_in_name(entry.name)
    if window is not None and entry.written_day is not None and entry.written_day < window[1]:
        window = (window[0], max(window[0], entry.written_day))  # a file cannot vouch for days after it was written
    if window is not None:
        writer.record_export_range(stream, *window)
    data = entry.read()
    records = _load_json(data)
    if records is None:
        bounds = {"start_utc": _noon_utc(window[0]), "end_utc": _noon_utc(window[1])} if window else {}
        writer.record_parse_failure(stream, label, "bad_json", "file is not valid JSON or not a JSON array/object",
                                    payload_hash=hashlib.sha256(data).hexdigest(), **bounds)
        return
    if stream in BATCH_STREAMS:
        for key, record, decoded in decode_readiness_records(records):
            writer.write_json_record(stream, key, record, decoded, label)
        return
    decoder = RECORD_DECODERS[stream]
    for record in records:
        result = decoder(record)
        if result is None:
            reason = _NO_DATE_REASON.get(stream)
            if reason:
                writer.stats.dropped[reason] = writer.stats.dropped.get(reason, 0) + 1
            continue
        key, decoded = result
        writer.write_json_record(stream, key, record, decoded, label)


def import_connect_export(path: pathlib.Path, writer: Writer, progress=None, cancel=None) -> None:
    """Feed every recognised part of the export to ``writer``.

    JSON families handled are listed in :data:`FILE_FAMILIES` (the daily spine
    UDSFile, sleep, training readiness, VO2max, acute/chronic training load,
    endurance score, hill score, fitness age and body weight). Everything else
    in the export is ignored and counted. The FIT corpus must be handled by the
    caller through :func:`collect_fit_members` (it needs the clock-offset
    pre-pass the caller orchestrates). Each file's claimed date window (from
    its name) is recorded for the coverage ledger, and a file that is not
    valid JSON is recorded as a failure instead of being skipped silently.
    ``progress(done, None, stream)`` is called after each imported file; ``cancel()`` is asked
    before each one (``sources.CancelCheck``): True ends the phase there, unread, with
    ``writer.stats.cancelled`` set.
    """
    path = pathlib.Path(path)
    done = 0
    for entry in _entries(path):
        stream = stream_for_entry(_connect_section(entry.relpath), entry.name)
        if stream is None:
            writer.stats.ignored += 1
            continue
        if cancel is not None and cancel():
            writer.stats.cancelled = True
            return
        _import_entry(entry, stream, writer)
        done += 1
        if progress is not None:
            progress(done, None, stream)
