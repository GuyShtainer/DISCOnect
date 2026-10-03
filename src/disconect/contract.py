"""The read contract: what every number means, defined once for every outlet.

The CLI, the MCP server and any future UI are adapters over one store. If each
explained units, time zones, sources and missing values on its own they would
eventually disagree, and neither the user nor an AI model could tell which one
is right. So the definitions live here, and every adapter quotes them.

Changing a convention or a metric's meaning changes what external tools see:
treat it as a breaking change and bump `CONTRACT_VERSION`. Adding a metric is
not breaking.
"""

from __future__ import annotations

import dataclasses

CONTRACT_VERSION = "1"

TIME_CONVENTION = (
    "Sample timestamps (ts_utc) are RFC 3339 in UTC with a trailing 'Z'. Daily "
    "values are keyed by the watch's local calendar date (YYYY-MM-DD), resolved "
    "with the UTC offset the watch itself reported nearest to that moment; a "
    "night of sleep is keyed by the local date it ended. The time a file was "
    "imported (imported_at) is not the time a measurement happened and never "
    "stands in for it."
)

MISSING_VALUE_CONVENTION = (
    "Missing means missing: a minute or a day without a sample has no row, and "
    "a series shorter than its span means the watch recorded nothing then. A "
    "value is never filled with 0, the previous value, or an estimate. Readings "
    "the watch itself marks invalid (off wrist, too much motion) are dropped, "
    "not stored as numbers."
)

SOURCE_CONVENTION = (
    "source_scope names the layer a value came from. 'device' is decoded from "
    "the watch's own FIT bytes, whichever way they arrived (USB, a Connect "
    "account export, Gadgetbridge). 'vendor_cloud' is a figure Garmin Connect "
    "computed and delivered as JSON. 'local' is computed here from device data. "
    "The scopes are stored side by side and never merged; every value says "
    "which scope it belongs to."
)

PRIVACY_NOTE = (
    "Reads one local SQLite file. Opens no network connection and no port. "
    "Returns no device serial numbers, file paths, account identifiers, or GPS "
    "coordinates."
)

COVERAGE_CONVENTION = (
    "Coverage explains a missing day. For each metric and source scope, every local day in the "
    "window has exactly one status, decided in this order: 'present' (rows exist; the day may "
    "still be incomplete), 'failed' (a file or record that would have supplied it failed to "
    "decode), 'source_empty' (an imported file spanned the day, or an export file claimed the "
    "window, yet nothing was decoded for it: the source genuinely had nothing), 'not_covered' "
    "(no import has ever covered the day). Metrics marked sparse are recorded only on some days "
    "by nature (a VO2max update, a weigh-in), so their 'source_empty' days are normal. The watch "
    "writes sleep, HRV and skin-temperature files only for nights it recorded, so a day the all-day "
    "monitoring files cover counts as covered for them too. Coverage "
    "is derived from the retained raw files each time it is asked for; it is never guessed."
)

SOURCE_SCOPES = ("device", "vendor_cloud", "local")

#: Daily metrics computed here that answer the same question as a vendor_cloud metric under
#: another name. ``health.source_agreement`` compares these pairs besides identical ids.
COMPARISON_TARGETS: dict[tuple[str, str], tuple[str, str]] = {
    ("energy_reserve_high", "local"): ("body_battery_high", "vendor_cloud"),
    ("energy_reserve_low", "local"): ("body_battery_low", "vendor_cloud"),
    ("energy_reserve_charged", "local"): ("body_battery_charged", "vendor_cloud"),
    ("energy_reserve_drained", "local"): ("body_battery_drained", "vendor_cloud"),
}

#: Which retained streams can supply each (metric, scope). Declared here so a day with no row
#: can be judged against the files that *would* have carried it; the store's observed
#: (metric, scope, stream) triples are unioned in at read time and any undeclared triple is
#: reported as map drift. Keep in step with the decoders (tests enforce observed ⊆ declared).
_UDS = ("json:uds",)
_READINESS = ("json:readiness",)
_HRV_FIT = ("fit:hrv",)
_FIT_SLEEP = ("fit:sleep",)
_FIT_MON = ("fit:monitoring_b",)
STREAMS_FOR: dict[tuple[str, str], tuple[str, ...]] = {
    # sample cadence (device)
    ("heart_rate", "device"): _FIT_MON, ("stress", "device"): _FIT_MON, ("energy_reserve", "device"): _FIT_MON,
    ("respiration_rate", "device"): _FIT_MON, ("spo2", "device"): _FIT_MON,
    ("hrv_rmssd", "device"): _HRV_FIT,
    # dailies derived locally from device samples/counters
    ("steps", "local"): _FIT_MON, ("distance", "local"): _FIT_MON, ("stress_avg", "local"): _FIT_MON,
    ("heart_rate_min", "local"): _FIT_MON, ("heart_rate_max", "local"): _FIT_MON,
    ("energy_reserve_high", "local"): _FIT_MON, ("energy_reserve_low", "local"): _FIT_MON,
    ("energy_reserve_charged", "local"): _FIT_MON, ("energy_reserve_drained", "local"): _FIT_MON,
    # dailies the watch states itself
    ("resting_heart_rate", "device"): _FIT_MON, ("resting_heart_rate_current_day", "device"): _FIT_MON,
    ("resting_metabolic_rate", "device"): _FIT_MON,
    ("sleep_score", "device"): _FIT_SLEEP, ("sleep_duration", "device"): _FIT_SLEEP,
    ("hrv_weekly_average", "device"): _HRV_FIT, ("hrv_last_night_average", "device"): _HRV_FIT,
    ("hrv_last_night_5min_high", "device"): _HRV_FIT, ("hrv_baseline_low_upper", "device"): _HRV_FIT,
    ("hrv_baseline_balanced_lower", "device"): _HRV_FIT, ("hrv_baseline_balanced_upper", "device"): _HRV_FIT,
    ("hrv_status", "device"): _HRV_FIT,
    ("skin_temp_nightly", "device"): ("fit:skin_temp",), ("skin_temp_deviation", "device"): ("fit:skin_temp",),
    ("skin_temp_7day_deviation", "device"): ("fit:skin_temp",),
    ("vo2max", "device"): ("fit:metrics",), ("vo2max_sport", "device"): ("fit:metrics",),
    # dailies Garmin's cloud computed (Connect export JSON)
    ("steps", "vendor_cloud"): _UDS, ("distance", "vendor_cloud"): _UDS,
    ("resting_heart_rate", "vendor_cloud"): _UDS, ("resting_heart_rate_current_day", "vendor_cloud"): _UDS,
    ("heart_rate_min", "vendor_cloud"): _UDS, ("heart_rate_max", "vendor_cloud"): _UDS,
    ("stress_avg", "vendor_cloud"): _UDS, ("spo2_avg", "vendor_cloud"): _UDS, ("spo2_lowest", "vendor_cloud"): _UDS,
    ("respiration_avg_waking", "vendor_cloud"): _UDS, ("respiration_lowest", "vendor_cloud"): _UDS,
    ("respiration_highest", "vendor_cloud"): _UDS,
    ("intensity_minutes_moderate", "vendor_cloud"): _UDS, ("intensity_minutes_vigorous", "vendor_cloud"): _UDS,
    ("calories_total", "vendor_cloud"): _UDS, ("calories_active", "vendor_cloud"): _UDS,
    ("calories_bmr", "vendor_cloud"): _UDS,
    ("body_battery_high", "vendor_cloud"): _UDS, ("body_battery_low", "vendor_cloud"): _UDS,
    ("body_battery_charged", "vendor_cloud"): _UDS, ("body_battery_drained", "vendor_cloud"): _UDS,
    ("active_minutes", "vendor_cloud"): _UDS, ("highly_active_minutes", "vendor_cloud"): _UDS,
    ("hydration_ml", "vendor_cloud"): _UDS, ("sweat_loss_ml", "vendor_cloud"): _UDS,
    ("sleep_score", "vendor_cloud"): ("json:sleep",), ("sleep_duration", "vendor_cloud"): ("json:sleep",),
    ("training_readiness", "vendor_cloud"): _READINESS, ("readiness_factor_sleep", "vendor_cloud"): _READINESS,
    ("readiness_factor_recovery_time", "vendor_cloud"): _READINESS,
    ("readiness_factor_load_ratio", "vendor_cloud"): _READINESS,
    ("readiness_factor_stress_history", "vendor_cloud"): _READINESS,
    ("readiness_factor_hrv", "vendor_cloud"): _READINESS, ("readiness_factor_sleep_history", "vendor_cloud"): _READINESS,
    ("hrv_weekly_average", "vendor_cloud"): _READINESS, ("recovery_time", "vendor_cloud"): _READINESS,
    ("training_readiness_level", "vendor_cloud"): _READINESS, ("training_readiness_context", "vendor_cloud"): _READINESS,
    ("vo2max", "vendor_cloud"): ("json:vo2max",), ("vo2max_sport", "vendor_cloud"): ("json:vo2max",),
    ("training_load_acute", "vendor_cloud"): ("json:training_load",),
    ("training_load_chronic", "vendor_cloud"): ("json:training_load",),
    ("training_load_ratio", "vendor_cloud"): ("json:training_load",),
    ("training_load_status", "vendor_cloud"): ("json:training_load",),
    ("endurance_score", "vendor_cloud"): ("json:endurance",),
    ("fitness_age", "vendor_cloud"): ("json:fitness_age",),
    ("weight_kg", "vendor_cloud"): ("json:biometrics",),
}

#: Metrics that exist only on the days an event produced them; their empty days are not gaps.
SPARSE_METRICS = frozenset({
    "vo2max", "vo2max_sport", "weight_kg", "fitness_age", "endurance_score", "hydration_ml",
    "sweat_loss_ml", "hrv_baseline_low_upper", "hrv_baseline_balanced_lower",
    "hrv_baseline_balanced_upper", "resting_metabolic_rate", "spo2", "spo2_avg", "spo2_lowest",
    "training_load_acute", "training_load_chronic", "training_load_ratio", "training_load_status",
})

#: How often a metric is observed. Decides which table holds it and how a
#: series is served: samples are aggregated per day, dailies are returned as-is.
CADENCE_SAMPLE = "sample"
CADENCE_DAILY = "daily"


@dataclasses.dataclass(frozen=True)
class MetricContract:
    """One metric's public definition.

    ``unit`` is a short machine-readable string (``bpm``, ``mL/kg/min``); prose
    lives in ``description`` so schemas never change with UI language.
    """

    metric: str
    unit: str
    cadence: str
    description: str


METRICS: tuple[MetricContract, ...] = (
    # --- sample cadence: rows in metric_samples ---
    MetricContract("heart_rate", "bpm", CADENCE_SAMPLE,
                   "Wrist heart rate from all-day monitoring, every minute or two."),
    MetricContract("stress", "score", CADENCE_SAMPLE,
                   "Stress level 0-100 once a minute; invalid readings dropped."),
    MetricContract("respiration_rate", "breaths/min", CADENCE_SAMPLE,
                   "Respiration rate once a minute; invalid readings dropped."),
    MetricContract("spo2", "%", CADENCE_SAMPLE,
                   "Pulse oximetry reading (periodic or spot check); off-wrist readings dropped."),
    MetricContract("hrv_rmssd", "ms", CADENCE_SAMPLE,
                   "Five-minute heart-rate variability (RMSSD) measured during sleep."),
    MetricContract("energy_reserve", "score", CADENCE_SAMPLE,
                   "The watch's own body-energy gauge, 0-100, one reading a minute: it rises with rest "
                   "and sleep and falls with stress and effort. Read from the watch's files, not estimated."),
    # --- daily cadence: rows in daily_metrics ---
    MetricContract("steps", "steps", CADENCE_DAILY,
                   "Steps for the local day. From device data this is the sum over activity "
                   "types of each type's highest cumulative count that day."),
    MetricContract("distance", "m", CADENCE_DAILY, "Distance walked and run for the day."),
    MetricContract("resting_heart_rate", "bpm", CADENCE_DAILY,
                   "Resting heart rate as the watch reports it for the day."),
    MetricContract("resting_heart_rate_current_day", "bpm", CADENCE_DAILY,
                   "The watch's running resting-heart-rate estimate for the current day."),
    MetricContract("heart_rate_min", "bpm", CADENCE_DAILY, "Lowest heart rate of the day."),
    MetricContract("heart_rate_max", "bpm", CADENCE_DAILY, "Highest heart rate of the day."),
    MetricContract("stress_avg", "score", CADENCE_DAILY, "Average stress level over the whole day."),
    MetricContract("energy_reserve_high", "score", CADENCE_DAILY, "Highest energy-reserve reading of the local day."),
    MetricContract("energy_reserve_low", "score", CADENCE_DAILY, "Lowest energy-reserve reading of the local day."),
    MetricContract("energy_reserve_charged", "score", CADENCE_DAILY,
                   "Energy reserve gained over the local day: the sum of the gauge's upward steps between "
                   "consecutive readings at most 15 minutes apart, counted from the day's first reading."),
    MetricContract("energy_reserve_drained", "score", CADENCE_DAILY,
                   "Energy reserve spent over the local day: the sum of the gauge's downward steps between "
                   "consecutive readings at most 15 minutes apart, counted from the day's first reading."),
    MetricContract("spo2_avg", "%", CADENCE_DAILY, "Average pulse oximetry reading for the day."),
    MetricContract("spo2_lowest", "%", CADENCE_DAILY, "Lowest pulse oximetry reading for the day."),
    MetricContract("respiration_avg_waking", "breaths/min", CADENCE_DAILY,
                   "Average respiration rate while awake."),
    MetricContract("respiration_lowest", "breaths/min", CADENCE_DAILY, "Lowest respiration rate of the day."),
    MetricContract("respiration_highest", "breaths/min", CADENCE_DAILY, "Highest respiration rate of the day."),
    MetricContract("intensity_minutes_moderate", "min", CADENCE_DAILY, "Moderate-intensity minutes."),
    MetricContract("intensity_minutes_vigorous", "min", CADENCE_DAILY, "Vigorous-intensity minutes."),
    MetricContract("calories_total", "kcal", CADENCE_DAILY, "Total energy expenditure for the day."),
    MetricContract("calories_active", "kcal", CADENCE_DAILY, "Active energy expenditure for the day."),
    MetricContract("calories_bmr", "kcal", CADENCE_DAILY, "Basal metabolic energy for the day."),
    MetricContract("resting_metabolic_rate", "kcal/day", CADENCE_DAILY,
                   "Resting metabolic rate the watch used for the day."),
    MetricContract("sleep_score", "score", CADENCE_DAILY, "Overall sleep score 0-100 for the night ending that day."),
    MetricContract("sleep_duration", "min", CADENCE_DAILY,
                   "Minutes asleep (deep + light + REM) for the night ending that day."),
    MetricContract("hrv_weekly_average", "ms", CADENCE_DAILY, "Seven-day average of nightly HRV."),
    MetricContract("hrv_last_night_average", "ms", CADENCE_DAILY, "Average HRV of the last night."),
    MetricContract("hrv_last_night_5min_high", "ms", CADENCE_DAILY, "Highest five-minute HRV of the last night."),
    MetricContract("hrv_baseline_low_upper", "ms", CADENCE_DAILY,
                   "Upper bound of the personal 'low' HRV band the watch reports."),
    MetricContract("hrv_baseline_balanced_lower", "ms", CADENCE_DAILY,
                   "Lower bound of the personal 'balanced' HRV band the watch reports."),
    MetricContract("hrv_baseline_balanced_upper", "ms", CADENCE_DAILY,
                   "Upper bound of the personal 'balanced' HRV band the watch reports."),
    MetricContract("skin_temp_nightly", "degC", CADENCE_DAILY,
                   "Overnight skin temperature value as the watch reports it."),
    MetricContract("skin_temp_deviation", "degC", CADENCE_DAILY,
                   "Overnight skin temperature deviation from the personal baseline."),
    MetricContract("skin_temp_7day_deviation", "degC", CADENCE_DAILY,
                   "Seven-day average skin temperature deviation."),
    MetricContract("vo2max", "mL/kg/min", CADENCE_DAILY,
                   "VO2max estimate on the day the watch updated it."),
    MetricContract("body_battery_high", "score", CADENCE_DAILY, "Highest body battery level of the day."),
    MetricContract("body_battery_low", "score", CADENCE_DAILY, "Lowest body battery level of the day."),
    MetricContract("body_battery_charged", "score", CADENCE_DAILY, "Body battery gained over the day."),
    MetricContract("body_battery_drained", "score", CADENCE_DAILY, "Body battery spent over the day."),
    MetricContract("training_readiness", "score", CADENCE_DAILY,
                   "Training readiness score 0-100, the morning value when one exists."),
    MetricContract("readiness_factor_sleep", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of last night's sleep to readiness."),
    MetricContract("readiness_factor_recovery_time", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of remaining recovery time to readiness."),
    MetricContract("readiness_factor_load_ratio", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of the acute:chronic load ratio to readiness."),
    MetricContract("readiness_factor_stress_history", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of recent stress to readiness."),
    MetricContract("readiness_factor_hrv", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of HRV status to readiness."),
    MetricContract("readiness_factor_sleep_history", "%", CADENCE_DAILY,
                   "Vendor's stated contribution of recent sleep history to readiness."),
    MetricContract("recovery_time", "h", CADENCE_DAILY, "Recovery time remaining, as reported."),
    MetricContract("training_load_acute", "load", CADENCE_DAILY,
                   "Acute (seven-day) training load as reported, alongside training_load_chronic "
                   "and training_load_ratio."),
    # --- appended: remaining Connect-export sections (training load, endurance, fitness age, ----
    # --- biometrics, UDS active-minutes/hydration) beyond the daily spine, sleep, readiness and --
    # --- VO2max. Do not reorder or rename entries above this block. ---
    MetricContract("training_load_chronic", "load", CADENCE_DAILY,
                   "Chronic (four-week) training load as reported, alongside training_load_acute "
                   "and training_load_ratio."),
    MetricContract("training_load_ratio", "ratio", CADENCE_DAILY,
                   "Ratio of acute to chronic training load as reported."),
    MetricContract("endurance_score", "score", CADENCE_DAILY,
                   "Endurance Score as the cloud computes it for the day."),
    MetricContract("fitness_age", "years", CADENCE_DAILY,
                   "Estimated fitness age as of the assessment's date."),
    MetricContract("weight_kg", "kg", CADENCE_DAILY,
                   "Body weight as logged in the vendor's biometrics; grams are converted to kilograms."),
    MetricContract("active_minutes", "min", CADENCE_DAILY,
                   "Minutes of active movement for the day, from the daily wellness rollup."),
    MetricContract("highly_active_minutes", "min", CADENCE_DAILY,
                   "Minutes of highly active movement for the day, from the daily wellness rollup."),
    MetricContract("hydration_ml", "mL", CADENCE_DAILY, "Fluid intake logged for the day."),
    MetricContract("sweat_loss_ml", "mL", CADENCE_DAILY, "Estimated sweat loss for the day."),
)

#: Daily values that are labels rather than numbers; served from daily_labels.
LABELS: tuple[MetricContract, ...] = (
    MetricContract("hrv_status", "label", CADENCE_DAILY,
                   "HRV status band: balanced, unbalanced, low, poor, or none."),
    MetricContract("training_readiness_level", "label", CADENCE_DAILY,
                   "Readiness band: prime, high, moderate, low, or poor."),
    MetricContract("training_readiness_context", "label", CADENCE_DAILY,
                   "Which of the day's readiness updates the score was taken from."),
    MetricContract("vo2max_sport", "label", CADENCE_DAILY, "Sport the VO2max estimate belongs to."),
    # --- appended: remaining Connect-export sections, see the matching block in METRICS above. ---
    MetricContract("training_load_status", "label", CADENCE_DAILY,
                   "Acute:chronic training-load status band, lower-cased as the cloud reports it."),
)

_BY_NAME = {item.metric: item for item in METRICS}
_LABELS_BY_NAME = {item.metric: item for item in LABELS}


def metric_names() -> list[str]:
    """Every numeric metric name, in contract order; feeds the MCP enum."""
    return [item.metric for item in METRICS]


def label_names() -> list[str]:
    """Every label metric name, in contract order."""
    return [item.metric for item in LABELS]


def metric(name: str) -> MetricContract:
    """Return the contract for ``name``; raise KeyError for a metric not defined here."""
    return _BY_NAME[name]


def unit_for(name: str) -> str | None:
    """Unit string for a numeric or label metric, or None if undefined."""
    item = _BY_NAME.get(name) or _LABELS_BY_NAME.get(name)
    return item.unit if item else None


def cadence_for(name: str) -> str | None:
    """'sample' or 'daily' for a numeric metric, or None if undefined."""
    item = _BY_NAME.get(name)
    return item.cadence if item else None


def as_dict() -> dict:
    """The whole contract as plain data, for ``disconect contract --json`` and MCP."""
    return {
        "contract_version": CONTRACT_VERSION,
        "time": TIME_CONVENTION,
        "missing_values": MISSING_VALUE_CONVENTION,
        "sources": SOURCE_CONVENTION,
        "privacy": PRIVACY_NOTE,
        "coverage": COVERAGE_CONVENTION,
        "source_scopes": list(SOURCE_SCOPES),
        "streams_for": [{"metric": metric, "source_scope": scope, "streams": list(streams)}
                        for (metric, scope), streams in STREAMS_FOR.items()],
        "sparse_metrics": sorted(SPARSE_METRICS),
        "metrics": [dataclasses.asdict(item) for item in METRICS],
        "labels": [dataclasses.asdict(item) for item in LABELS],
    }
