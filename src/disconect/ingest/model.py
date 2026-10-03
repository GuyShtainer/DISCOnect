"""What a decoder hands the writer. Plain dataclasses, no SQL, no fitdecode types.

Daily facts carry either the UTC moment they were observed (the writer resolves
the local date from the watch's clock offsets) or an explicit ``date`` when the
source already states one (Connect JSON). Never both.
"""

from __future__ import annotations

import dataclasses
import datetime


@dataclasses.dataclass(frozen=True)
class Sample:
    metric: str
    ts_utc: datetime.datetime
    value: float


@dataclasses.dataclass(frozen=True)
class DailyFact:
    metric: str
    value: float
    ts_utc: datetime.datetime | None = None
    date: str | None = None


@dataclasses.dataclass(frozen=True)
class DailyLabel:
    metric: str
    label: str
    ts_utc: datetime.datetime | None = None
    date: str | None = None


@dataclasses.dataclass(frozen=True)
class MonitoringInterval:
    ts_utc: datetime.datetime
    activity_type: str
    steps: int | None = None
    cycles: float | None = None
    active_time_s: float | None = None
    active_calories_kcal: int | None = None
    distance_m: float | None = None
    intensity: int | None = None


@dataclasses.dataclass(frozen=True)
class SleepStage:
    stage: str
    start_utc: datetime.datetime
    end_utc: datetime.datetime


@dataclasses.dataclass
class SleepSession:
    """One night. Any field the source did not state stays None (never 0)."""

    start_utc: datetime.datetime | None = None
    end_utc: datetime.datetime | None = None
    date: str | None = None
    stages: list[SleepStage] = dataclasses.field(default_factory=list)
    deep_s: int | None = None
    light_s: int | None = None
    rem_s: int | None = None
    awake_s: int | None = None
    unmeasurable_s: int | None = None
    overall_score: int | None = None
    quality_score: int | None = None
    duration_score: int | None = None
    recovery_score: int | None = None
    deep_score: int | None = None
    rem_score: int | None = None
    light_score: int | None = None
    awake_time_score: int | None = None
    awakenings_count_score: int | None = None
    combined_awake_score: int | None = None
    restlessness_score: int | None = None
    interruptions_score: int | None = None
    awakenings_count: int | None = None
    avg_stress: float | None = None
    avg_spo2: float | None = None
    lowest_spo2: int | None = None
    avg_hr: float | None = None
    avg_respiration: float | None = None
    lowest_respiration: float | None = None
    highest_respiration: float | None = None
    retro: bool = False


@dataclasses.dataclass(frozen=True)
class Activity:
    start_utc: datetime.datetime
    end_utc: datetime.datetime | None = None
    sport: str | None = None
    sub_sport: str | None = None
    total_timer_s: float | None = None
    total_elapsed_s: float | None = None
    distance_m: float | None = None
    calories_kcal: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_speed_mps: float | None = None
    total_ascent_m: float | None = None
    total_descent_m: float | None = None


@dataclasses.dataclass(frozen=True)
class ClockOffset:
    ts_utc: datetime.datetime
    offset_s: int


@dataclasses.dataclass
class Decoded:
    """Everything one source file or record contributed.

    ``dropped`` counts values the decoder refused (sentinels, off-wrist
    readings, undatable stages) by reason, so data health can report them
    instead of them vanishing silently.
    """

    stream: str
    source_scope: str
    device_id: str | None = None
    start_utc: datetime.datetime | None = None
    end_utc: datetime.datetime | None = None
    samples: list[Sample] = dataclasses.field(default_factory=list)
    daily: list[DailyFact] = dataclasses.field(default_factory=list)
    labels: list[DailyLabel] = dataclasses.field(default_factory=list)
    intervals: list[MonitoringInterval] = dataclasses.field(default_factory=list)
    sleep: SleepSession | None = None
    activities: list[Activity] = dataclasses.field(default_factory=list)
    offsets: list[ClockOffset] = dataclasses.field(default_factory=list)
    message_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    dropped: dict[str, int] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)

    def drop(self, reason: str, count: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + count

    def record_count(self) -> int:
        """Rows this decode will write, for provenance and the import summary."""
        stages = len(self.sleep.stages) if self.sleep else 0
        return (len(self.samples) + len(self.daily) + len(self.labels) + len(self.intervals)
                + (1 if self.sleep else 0) + stages + len(self.activities) + len(self.offsets))
