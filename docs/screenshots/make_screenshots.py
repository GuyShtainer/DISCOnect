"""Rebuild the README screenshots from synthetic data only.

Writes a seeded, made-up 90-day account export (watch FIT files + the export's JSON layer) with
``tests/fit_builder.py``, imports it into a throwaway store under a temporary HOME, and draws the
charts with the CLI's own PNG renderer. No file from a real watch or account is read.

    .venv/bin/python docs/screenshots/make_screenshots.py      # from the package root
"""

from __future__ import annotations

import datetime
import io
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE = HERE.parents[1]
sys.path.insert(0, str(PACKAGE / "tests"))

from fit_builder import FitBuilder  # noqa: E402

UTC = datetime.timezone.utc
OFFSET = datetime.timedelta(hours=2)          # a made-up watch time zone, UTC+2
DAYS = 90
LAST_DAY = datetime.date(2025, 6, 30)
SERIAL = 1234567890                           # fit_builder's synthetic default
RNG = random.Random(20251008)


def _local_midnight_utc(day: datetime.date) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, tzinfo=UTC) - OFFSET


def _heart_rate(minute: int, asleep: bool, active: bool) -> int:
    if asleep:
        return RNG.randint(47, 56)
    if active:
        return RNG.randint(115, 150)
    base = 64 + 6 * (1 if 9 * 60 <= minute <= 18 * 60 else 0)
    return max(50, int(RNG.gauss(base, 5)))


def monitoring_day(day: datetime.date, steps: int, rhr: int, detailed: bool) -> bytes:
    start = _local_midnight_utc(day)
    b = FitBuilder("monitoring_b", serial=SERIAL, created=start)
    b.add("monitoring_info", timestamp=start, local_timestamp=start + OFFSET, resting_metabolic_rate=1650)
    b.add("monitoring", timestamp=start + datetime.timedelta(minutes=1), activity_type="walking",
          steps=20, active_time=60.0, distance=14.0)
    b.add("monitoring", timestamp=start + datetime.timedelta(hours=12), activity_type="walking",
          steps=steps // 2, active_time=2400.0, distance=steps / 2 * 0.75)
    b.add("monitoring", timestamp=start + datetime.timedelta(hours=24), activity_type="walking",
          steps=steps, active_time=5000.0, distance=steps * 0.75)
    b.add("monitoring_hr_data", timestamp=start + datetime.timedelta(hours=20),
          resting_heart_rate=rhr, current_day_resting_heart_rate=rhr + 1)
    if detailed:                                  # per-minute samples for the day chart
        run = range(17 * 60 + 30, 18 * 60 + 15)
        for minute in range(0, 24 * 60, 2):
            hr = _heart_rate(minute, asleep=minute < 6 * 60 + 30 or minute > 23 * 60, active=minute in run)
            b.add("monitoring", timestamp=start + datetime.timedelta(minutes=minute), heart_rate=hr)
    return b.build()


NIGHT = [("light", 25), ("deep", 55), ("light", 40), ("rem", 25), ("light", 35), ("deep", 35),
         ("awake", 5), ("light", 45), ("rem", 35), ("light", 40), ("rem", 40), ("light", 20)]


def sleep_night(wake_day: datetime.date) -> tuple[bytes, dict, int]:
    """One night ending on the morning of ``wake_day``; returns FIT, the export's JSON, the score."""
    stages = [(name, max(5, minutes + RNG.randint(-8, 8))) for name, minutes in NIGHT]
    bed = _local_midnight_utc(wake_day) - datetime.timedelta(minutes=RNG.randint(30, 75))
    total = sum(m for _, m in stages)
    score = max(60, min(92, int(76 + (total - 395) * 0.3 + RNG.randint(-7, 7))))
    b = FitBuilder("49", serial=SERIAL, created=bed + datetime.timedelta(minutes=total))
    b.add("event", timestamp=bed, event=74, event_type="start")
    t = bed
    seconds: dict[str, int] = {"light": 0, "deep": 0, "rem": 0, "awake": 0}
    for name, minutes in stages:
        t += datetime.timedelta(minutes=minutes)
        seconds[name] += minutes * 60
        b.add("sleep_level", timestamp=t, sleep_level=name)
    b.add("event", timestamp=t, event=74, event_type="stop")
    b.add("sleep_assessment", overall_sleep_score=score, awakenings_count=1)
    fmt = "%Y-%m-%dT%H:%M:%S.0"
    js = {"retro": False, "calendarDate": wake_day.isoformat(), "sleepStartTimestampGMT": bed.strftime(fmt),
          "sleepEndTimestampGMT": t.strftime(fmt), "deepSleepSeconds": seconds["deep"],
          "lightSleepSeconds": seconds["light"], "remSleepSeconds": seconds["rem"],
          "awakeSleepSeconds": seconds["awake"], "unmeasurableSeconds": 0, "awakeCount": 1,
          "sleepScores": {"overallScore": score}}
    return b.build(), js, score


def uds(day: datetime.date, steps: int, rhr: int) -> dict:
    d = day.isoformat()
    return {"userProfilePK": 111, "calendarDate": d, "totalSteps": steps, "totalDistanceMeters": int(steps * 0.75),
            "restingHeartRate": rhr, "currentDayRestingHeartRate": rhr + 1, "includesWellnessData": True, "totalKilocalories": 2200.0,
            "activeKilocalories": 450.0, "bmrKilocalories": 1750.0, "moderateIntensityMinutes": 25,
            "vigorousIntensityMinutes": 10, "wellnessStartTimeGmt": f"{d}T22:00:00.0",
            "wellnessEndTimeGmt": f"{d}T22:00:00.0"}


def build_export(root: pathlib.Path) -> None:
    fits: dict[str, bytes] = {}
    days, nights = [], []
    for i in range(DAYS):
        day = LAST_DAY - datetime.timedelta(days=DAYS - 1 - i)
        weekend = day.weekday() >= 5
        steps = max(1500, int(RNG.gauss(11500 if weekend else 8200, 2200)))
        rhr_day = int(round(60 - 4 * i / DAYS + RNG.gauss(0, 0.8)))   # drifts down over the quarter
        fits[f"demo_{i:03d}_m.fit"] = monitoring_day(day, steps, rhr_day, detailed=i >= DAYS - 2)
        fit, js, _ = sleep_night(day)
        fits[f"demo_{i:03d}_s.fit"] = fit
        days.append(uds(day, steps + RNG.randint(-60, 60), rhr_day))
        nights.append(js)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        for name, data in fits.items():
            z.writestr(name, data)
    up = root / "DI_CONNECT" / "DI-Connect-Uploaded-Files"
    up.mkdir(parents=True)
    (up / "UploadedFiles_0-_Part1.zip").write_bytes(inner.getvalue())
    first, last = days[0]["calendarDate"], days[-1]["calendarDate"]
    agg = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    agg.mkdir()
    (agg / f"UDSFile_{first}_{last}.json").write_text(json.dumps(days))
    wellness = root / "DI_CONNECT" / "DI-Connect-Wellness"
    wellness.mkdir()
    (wellness / f"{first}_{last}_111_sleepData.json").write_text(json.dumps(nights))


def main() -> int:
    exe = pathlib.Path(sys.executable).parent / "disconect"
    with tempfile.TemporaryDirectory(prefix="disconect-demo-") as tmp:
        tmp_path = pathlib.Path(tmp)
        export = tmp_path / "export"
        build_export(export)
        env = {**os.environ, "HOME": str(tmp_path), "DISCONECT_DB": str(tmp_path / "demo.db")}
        env.pop("DISCONECT_PASSPHRASE", None)

        def run(*args: str, capture: bool = False) -> str:
            done = subprocess.run([str(exe), *args], env=env, check=True, text=True,
                                  capture_output=capture)
            return done.stdout if capture else ""

        run("import", str(export))
        end = LAST_DAY.isoformat()
        shots = [
            ("steps.png", ["chart", "metric", "steps", "--days", "90", "--end", end]),
            ("resting-heart-rate.png", ["chart", "metric", "resting_heart_rate", "--days", "90", "--end", end]),
            ("sleep-night.png", ["chart", "sleep", "--date", end]),
            ("heart-rate-day.png", ["chart", "samples", "heart_rate", "--date",
                                    (LAST_DAY - datetime.timedelta(days=1)).isoformat()]),
        ]
        for name, args in shots:
            run(*args, "--width", "1280", "--out", str(HERE / name))
            print("wrote", HERE / name)
        (HERE / "facts.txt").write_text(run("facts", "sleep_score", "resting_heart_rate", "steps",
                                            capture=True))
        print("wrote facts.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
