"""Charts: stored series drawn to PNG with nothing but the standard library.

Every chart is built from the same read-side queries the MCP server answers
with, so a picture can never claim something the read contract does not.
Three rules keep them honest:

* **Source scopes are never merged.** One line (or one row) per scope, always
  labelled. ``device`` and ``vendor_cloud`` disagreeing is a finding worth
  seeing, not noise worth averaging away.
* **A gap is drawn as a gap.** Days with no data break the line instead of
  being bridged by a straight segment that was never measured.
* **Nothing is invented.** No smoothing beyond an explicitly labelled trailing
  mean, no interpolation, no y-axis that hides zero without saying so.

Charts never raise on absent data: an empty window renders a framed panel that
says so, because a command that prints a picture should not fail differently
from one that prints "no data".
"""

from __future__ import annotations

import dataclasses
import datetime
import math
from collections.abc import Sequence

from disconect import contract, queries
from disconect.ingest.clock import ClockOffsets
from disconect.render import RGB, Canvas
from disconect.storage import parse_iso_utc
from disconect.storage import sqlite

SCOPE_COLORS: dict[str, RGB] = {
    "device": (26, 92, 168),
    "vendor_cloud": (214, 118, 32),
    "local": (38, 142, 92),
}
STAGE_COLORS: dict[str, RGB] = {
    "deep": (26, 62, 130),
    "light": (84, 138, 210),
    "rem": (138, 98, 198),
    "awake": (226, 138, 88),
    "unmeasurable": (172, 176, 182),
}
#: Top-to-bottom lane order of a hypnogram: awake at the surface, deep at the floor.
STAGE_LANES = ("awake", "rem", "light", "deep")

INK: RGB = (38, 42, 50)
MUTED: RGB = (112, 118, 128)
GRID: RGB = (226, 228, 232)
AXIS: RGB = (146, 150, 158)
PAPER: RGB = (255, 255, 255)
NIGHT: RGB = (60, 80, 150)

TITLE_SIZE = 15.0
LABEL_SIZE = 10.0
SMALL_SIZE = 9.0

MARGIN_LEFT = 66.0
MARGIN_RIGHT = 18.0
MARGIN_TOP = 58.0
MARGIN_BOTTOM = 46.0

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SCOPE_LEGEND = ("device = decoded from the watch's own FIT files   "
                "vendor_cloud = Garmin Connect's figures   local = computed here")


@dataclasses.dataclass
class _Frame:
    """A plot rectangle plus the data range it shows; maps data units to pixels."""

    canvas: Canvas
    left: float
    top: float
    right: float
    bottom: float
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def px(self, x: float) -> float:
        """Pixel column for data x (unclamped; the canvas clips)."""
        span = (self.x_max - self.x_min) or 1.0
        return self.left + (x - self.x_min) / span * (self.right - self.left)

    def py(self, y: float) -> float:
        """Pixel row for data y, y increasing upward (unclamped; the canvas clips)."""
        span = (self.y_max - self.y_min) or 1.0
        return self.bottom - (y - self.y_min) / span * (self.bottom - self.top)


def _nice_step(span: float, target: int) -> float:
    """A 1/2/2.5/5 x 10^n step that puts roughly ``target`` ticks across ``span``."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for factor in (1.0, 2.0, 2.5, 5.0):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10.0 * magnitude


def _ticks(low: float, high: float, target: int = 5) -> list[float]:
    """Round tick values inside ``[low, high]``, never more than ``target * 2 + 2`` of them."""
    step = _nice_step(high - low, target)
    first = math.ceil(low / step - 1e-9) * step
    out: list[float] = []
    value = first
    while value <= high + step * 1e-9 and len(out) < target * 2 + 2:
        out.append(round(value, 10))
        value += step
    return out


def _format_number(value: float, step: float) -> str:
    """Tick label with just enough decimals for the tick spacing to read distinctly."""
    if step >= 1:
        return f"{value:.0f}"
    if step >= 0.1:
        return f"{value:.1f}"
    if step >= 0.01:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _y_range(values: Sequence[float], include_zero: bool = False) -> tuple[float, float]:
    """Padded y limits for ``values``; a flat series still gets a readable band."""
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    if include_zero:
        low = min(low, 0.0)
    if high == low:
        pad = abs(high) * 0.05 or 1.0
        return low - pad, high + pad
    pad = (high - low) * 0.08
    return low - pad, high + pad


def _runs(points: Sequence[tuple[float, float]], max_gap: float) -> list[list[tuple[float, float]]]:
    """Split a series wherever consecutive x values are further apart than ``max_gap``."""
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for point in points:
        if current and point[0] - current[-1][0] > max_gap:
            runs.append(current)
            current = []
        current.append(point)
    if current:
        runs.append(current)
    return runs


def _draw_series(frame: _Frame, points: Sequence[tuple[float, float]], color: RGB,
                 max_gap: float, width: float = 1.8, alpha: float = 1.0) -> None:
    """Draw a series as gap-aware polylines; an isolated point becomes a dot."""
    for run in _runs(points, max_gap):
        pixels = [(frame.px(x), frame.py(y)) for x, y in run]
        if len(pixels) == 1:
            frame.canvas.dot(pixels[0][0], pixels[0][1], max(1.2, width * 0.9), color, alpha)
        else:
            frame.canvas.polyline(pixels, color, width, alpha)


def _new_canvas(width: int, height: int, title: str, subtitle: str, footer: str = "") -> Canvas:
    """A titled sheet with the standard header and an optional footer note."""
    canvas = Canvas(width, height, PAPER)
    canvas.text(MARGIN_LEFT - 2, 14, title, INK, TITLE_SIZE)
    if subtitle:
        canvas.text(MARGIN_LEFT - 2, 34, subtitle, MUTED, SMALL_SIZE)
    if footer:
        canvas.text(MARGIN_LEFT - 2, height - 13, footer, MUTED, SMALL_SIZE)
    return canvas


def _frame_for(canvas: Canvas, x_min: float, x_max: float, y_min: float, y_max: float,
               bottom_pad: float = MARGIN_BOTTOM) -> _Frame:
    return _Frame(canvas, MARGIN_LEFT, MARGIN_TOP, canvas.width - MARGIN_RIGHT,
                  canvas.height - bottom_pad, x_min, x_max, y_min, y_max)


def _draw_y_axis(frame: _Frame, unit: str, target: int = 5) -> None:
    """Horizontal gridlines, right-aligned tick labels, and the unit above them."""
    canvas = frame.canvas
    values = _ticks(frame.y_min, frame.y_max, target)
    step = _nice_step(frame.y_max - frame.y_min, target)
    for value in values:
        y = frame.py(value)
        canvas.line(frame.left, y, frame.right, y, GRID, 1.0)
        canvas.text(frame.left - 6, y, _format_number(value, step), MUTED, SMALL_SIZE,
                    anchor="right", baseline="middle")
    canvas.line(frame.left, frame.top, frame.left, frame.bottom, AXIS, 1.0)
    if unit:
        # Sitting right-aligned over the tick labels reads best, but a unit as long
        # as "mL/kg/min" is wider than the margin; those start at the sheet edge.
        if frame.left - 6 - canvas.text_width(unit, SMALL_SIZE) >= 2:
            canvas.text(frame.left - 6, frame.top - 14, unit, MUTED, SMALL_SIZE, anchor="right")
        else:
            canvas.text(2, frame.top - 14, unit, MUTED, SMALL_SIZE)


def _date_tick_days(span_days: int) -> int:
    """Day spacing between x ticks that keeps a date axis under about eight labels."""
    for candidate in (1, 2, 3, 7, 14, 28, 56, 91, 182, 364):
        if span_days / candidate <= 8:
            return candidate
    return max(1, span_days // 8)


def _draw_tick_label(canvas: Canvas, x: float, y: float, label: str) -> None:
    """Draw a centred tick label, nudged inward so it never runs off the sheet.

    The first and last ticks sit on the plot edges, where a wide label would
    otherwise overhang the canvas and lose its outer characters to clipping.
    """
    half = canvas.text_width(label, SMALL_SIZE) / 2.0
    canvas.text(min(max(x, half + 2), canvas.width - half - 2), y, label, MUTED, SMALL_SIZE,
                anchor="center")


def _format_date(date: datetime.date, with_year: bool) -> str:
    """Short date label, carrying the year only when the axis spans more than one.

    The year is written ``'26`` rather than ``26`` because a bare two-digit year
    beside a month name reads as a day of the month: "May 26" looks like the 26th
    of May, not May 2026.
    """
    label = f"{MONTHS[date.month - 1]} {date.day}"
    return f"{label} '{date.year % 100:02d}" if with_year else label


def _draw_date_axis(frame: _Frame, start: datetime.date) -> None:
    """Vertical gridlines and date labels for an x axis measured in days from ``start``."""
    canvas = frame.canvas
    span = int(frame.x_max - frame.x_min)
    spacing = _date_tick_days(max(1, span))
    first = start + datetime.timedelta(days=int(frame.x_min))
    last = first + datetime.timedelta(days=(span // spacing) * spacing)
    with_year = first.year != last.year
    offset = 0
    while offset <= span:
        x = frame.px(frame.x_min + offset)
        canvas.line(x, frame.top, x, frame.bottom, GRID, 1.0)
        label = _format_date(first + datetime.timedelta(days=offset), with_year)
        _draw_tick_label(canvas, x, frame.bottom + 6, label)
        offset += spacing
    canvas.line(frame.left, frame.bottom, frame.right, frame.bottom, AXIS, 1.0)


def _draw_hour_axis(frame: _Frame, spacing: float = 3.0) -> None:
    """Vertical gridlines every ``spacing`` hours, labelled on the 24-hour clock."""
    canvas = frame.canvas
    hour = math.ceil(frame.x_min / spacing) * spacing
    while hour <= frame.x_max + 1e-9:
        x = frame.px(hour)
        canvas.line(x, frame.top, x, frame.bottom, GRID, 1.0)
        _draw_tick_label(canvas, x, frame.bottom + 6, f"{int(hour) % 24:02d}:00")
        hour += spacing
    canvas.line(frame.left, frame.bottom, frame.right, frame.bottom, AXIS, 1.0)


def _draw_legend(canvas: Canvas, frame: _Frame, entries: Sequence[tuple[str, RGB]]) -> None:
    """Swatch-and-name legend along the top right of the plot, right to left."""
    x = frame.right
    for name, color in reversed(entries):
        text_w = canvas.text_width(name, SMALL_SIZE)
        canvas.text(x, frame.top - 13, name, INK, SMALL_SIZE, anchor="right")
        canvas.fill_rect(x - text_w - 13, frame.top - 12, 9, 7, color)
        x -= text_w + 26


def _empty_panel(width: int, height: int, title: str, subtitle: str, reason: str) -> Canvas:
    """A framed sheet that says why there is nothing to plot."""
    canvas = _new_canvas(width, height, title, subtitle, SCOPE_LEGEND)
    frame = _frame_for(canvas, 0, 1, 0, 1)
    canvas.line(frame.left, frame.top, frame.left, frame.bottom, AXIS, 1.0)
    canvas.line(frame.left, frame.bottom, frame.right, frame.bottom, AXIS, 1.0)
    canvas.text((frame.left + frame.right) / 2, (frame.top + frame.bottom) / 2, reason,
                MUTED, LABEL_SIZE, anchor="center", baseline="middle")
    return canvas


def _trailing_mean(points: Sequence[tuple[datetime.date, float]],
                   days: int) -> list[tuple[datetime.date, float]]:
    """Mean over the trailing ``days`` calendar days, emitted only where at least half are present.

    Calendar days, not the last N points: a week with three missing days must
    not be averaged as if it were a full week somewhere else.
    """
    by_date = dict(points)
    window = datetime.timedelta(days=days - 1)
    out: list[tuple[datetime.date, float]] = []
    for date, _ in points:
        present = [by_date[date - datetime.timedelta(days=back)]
                   for back in range(days)
                   if date - datetime.timedelta(days=back) in by_date]
        if len(present) * 2 >= days and date - window >= points[0][0]:
            out.append((date, sum(present) / len(present)))
    return out


def metric_chart(conn: sqlite.Connection, metric: str, days: int = 90,
                 end_date: str | None = None, source_scope: str | None = None,
                 rolling_days: int = 7, width: int = 1000, height: int = 460) -> Canvas:
    """One metric over time, one line per source scope, with a trailing-mean overlay.

    Daily metrics plot their stored value. Sample-cadence metrics plot the
    per-day mean with the day's min-max drawn behind it as a range bar, so a
    quiet mean over a violent day cannot pass for a quiet day. ``rolling_days``
    of 1 or less drops the overlay. Never raises for an empty window.
    """
    report = queries.metric_series(conn, [metric], days, source_scope, end_date)
    if report["ignored_metrics"]:
        raise ValueError(f"{metric!r} is not a contract metric")
    if not report["series"]:
        raise ValueError(f"{metric!r} is a label, not a numeric metric, so it has no line to draw")
    unit = contract.unit_for(metric) or ""
    series = [entry for entry in report["series"] if entry["points"]]
    title = f"{metric}" + (f"  ({unit})" if unit else "")
    span = report["series"][0]
    subtitle = f"{span['from']} to {span['to']}  ({span['cadence']} cadence)"
    if not series:
        return _empty_panel(width, height, title, subtitle,
                            "no data in this window for any source scope")

    start = datetime.date.fromisoformat(span["from"])
    end = datetime.date.fromisoformat(span["to"])
    is_sample = span["cadence"] == contract.CADENCE_SAMPLE
    plotted: list[tuple[str, list[tuple[datetime.date, float]], list[tuple[datetime.date, float, float]]]] = []
    spread: list[float] = []
    for entry in series:
        means = [(datetime.date.fromisoformat(p["date"]),
                  float(p["mean"] if is_sample else p["value"])) for p in entry["points"]]
        ranges = [(datetime.date.fromisoformat(p["date"]), float(p["min"]), float(p["max"]))
                  for p in entry["points"]] if is_sample else []
        plotted.append((entry["source_scope"], means, ranges))
        spread.extend(value for _, value in means)
        spread.extend(low for _, low, _ in ranges)
        spread.extend(high for _, _, high in ranges)

    y_min, y_max = _y_range(spread)
    if rolling_days > 1:
        subtitle += f"   thin = daily, thick = {rolling_days}-day trailing mean"
    canvas = _new_canvas(width, height, title, subtitle, SCOPE_LEGEND)
    frame = _frame_for(canvas, 0, max(1, (end - start).days), y_min, y_max)
    _draw_y_axis(frame, unit)
    _draw_date_axis(frame, start)

    day_width = (frame.right - frame.left) / max(1, (end - start).days + 1)
    bar_width = max(1.0, min(7.0, day_width * 0.65))
    for scope, means, ranges in plotted:
        color = SCOPE_COLORS.get(scope, MUTED)
        for date, low, high in ranges:
            x = frame.px((date - start).days)
            canvas.line(x, frame.py(low), x, frame.py(high), color, bar_width, 0.22)
        raw = [((date - start).days, value) for date, value in means]
        rolling = _trailing_mean(means, rolling_days) if rolling_days > 1 else []
        _draw_series(frame, raw, color, 1.5, 1.6, 0.5 if rolling else 1.0)
        if rolling:
            _draw_series(frame, [((date - start).days, value) for date, value in rolling],
                         color, 1.5, 2.4)
    _draw_legend(canvas, frame, [(scope, SCOPE_COLORS.get(scope, MUTED))
                                 for scope, _, _ in plotted])
    return canvas


def _stage_rows(sessions: Sequence[dict]) -> list[dict]:
    """Sessions that carry a stage series, newest scope order preserved."""
    return [session for session in sessions if session.get("stages")]


def _local_offset(conn: sqlite.Connection, moment: datetime.datetime) -> int:
    """Seconds to add to UTC for the watch's clock at ``moment``; 0 when unknown."""
    return ClockOffsets.load(conn).offset_at(moment) or 0


def _session_summary(session: dict) -> str:
    """One line of totals for a night: score, time asleep, and the stage split."""
    minutes = session.get("stage_minutes", {})
    asleep = sum(minutes.get(stage, 0.0) for stage in ("deep", "light", "rem"))
    parts = [f"{stage} {minutes[stage]:.0f}m" for stage in STAGE_LANES if stage in minutes]
    score = f"score {session['overall_score']:.0f}" if "overall_score" in session else "no score"
    return f"{session['source_scope']}   {score}   asleep {asleep / 60:.0f}h{asleep % 60:02.0f}m   " + "  ".join(parts)


def sleep_chart(conn: sqlite.Connection, date: str | None = None,
                width: int = 1000, height: int = 460) -> Canvas:
    """A hypnogram of one night, one row per source scope, on a shared local clock.

    Rows share an x axis so two sources' accounts of the same night line up
    stage for stage. Sessions that carry no stage series are reported in the
    footnote rather than drawn as an empty row. Never raises for a missing night.
    """
    detail = queries.sleep_detail(conn, date)
    night = detail["date"] or (date or "unknown")
    title = f"sleep  {night}"
    rows = _stage_rows(detail["sessions"])
    if not rows:
        return _empty_panel(width, height, title, "hypnogram, watch local clock",
                            detail.get("reason", "this night has no stage series stored"))

    starts = [parse_iso_utc(row["stages"][0]["start_utc"]) for row in rows]
    ends = [parse_iso_utc(row["stages"][-1]["end_utc"]) for row in rows]
    offset = _local_offset(conn, min(starts))
    origin = min(starts)

    def hours(moment: datetime.datetime) -> float:
        """Hours from the earliest session start, on the watch's clock."""
        return (moment - origin).total_seconds() / 3600

    local_start = (min(starts) + datetime.timedelta(seconds=offset)).strftime("%H:%M")
    local_end = (max(ends) + datetime.timedelta(seconds=offset)).strftime("%H:%M")
    subtitle = f"{local_start} to {local_end} local   {len(rows)} source(s)"
    canvas = _new_canvas(width, height, title, subtitle, SCOPE_LEGEND)
    frame = _frame_for(canvas, 0, max(0.5, hours(max(ends))), 0, 1)

    # The hour axis counts from the first stage, so label it with the wall clock it really was.
    start_clock = (origin + datetime.timedelta(seconds=offset))
    clock_offset_hours = start_clock.hour + start_clock.minute / 60 + start_clock.second / 3600
    hour = math.ceil(clock_offset_hours) - clock_offset_hours
    while hour <= frame.x_max + 1e-9:
        x = frame.px(hour)
        canvas.line(x, frame.top, x, frame.bottom, GRID, 1.0)
        canvas.text(x, frame.bottom + 6, f"{int(round(clock_offset_hours + hour)) % 24:02d}:00",
                    MUTED, SMALL_SIZE, anchor="center")
        hour += 1.0
    canvas.line(frame.left, frame.bottom, frame.right, frame.bottom, AXIS, 1.0)
    canvas.line(frame.left, frame.top, frame.left, frame.bottom, AXIS, 1.0)

    row_height = (frame.bottom - frame.top) / len(rows)
    for index, session in enumerate(rows):
        row_top = frame.top + index * row_height
        lane_height = (row_height - 30) / len(STAGE_LANES)
        for lane, stage in enumerate(STAGE_LANES):
            y = row_top + lane * lane_height
            canvas.text(frame.left - 6, y + lane_height / 2, stage, MUTED, SMALL_SIZE,
                        anchor="right", baseline="middle")
        for stage_span in session["stages"]:
            stage = stage_span["stage"]
            if stage not in STAGE_LANES:
                continue
            x0 = frame.px(hours(parse_iso_utc(stage_span["start_utc"])))
            x1 = frame.px(hours(parse_iso_utc(stage_span["end_utc"])))
            y = row_top + STAGE_LANES.index(stage) * lane_height
            canvas.fill_rect(x0, y + 1, max(1.0, x1 - x0), lane_height - 2,
                             STAGE_COLORS[stage])
        canvas.text(frame.left + 2, row_top + row_height - 22, _session_summary(session),
                    INK, SMALL_SIZE)
    missing = [s["source_scope"] for s in detail["sessions"] if not s.get("stages")]
    if missing:
        canvas.text(frame.right, MARGIN_TOP - 13,
                    f"no stage series from: {', '.join(missing)}", MUTED, SMALL_SIZE,
                    anchor="right")
    return canvas


def _sleep_windows(conn: sqlite.Connection, date: str, offset: int) -> list[tuple[float, float]]:
    """Sleep windows touching local day ``date``, as local-hour spans clipped to 0-24.

    A night is stored under the date it *ends* on, so the night that begins on
    ``date`` is filed under the day after; both are fetched and clipped.
    """
    spans: list[tuple[float, float]] = []
    day = datetime.date.fromisoformat(date)
    for which in (day, day + datetime.timedelta(days=1)):
        for session in queries.sleep_detail(conn, which.isoformat())["sessions"]:
            if "start_utc" not in session or "end_utc" not in session:
                continue
            begin = parse_iso_utc(session["start_utc"]) + datetime.timedelta(seconds=offset)
            finish = parse_iso_utc(session["end_utc"]) + datetime.timedelta(seconds=offset)
            low = (begin - datetime.datetime.combine(day, datetime.time(), begin.tzinfo)).total_seconds() / 3600
            high = (finish - datetime.datetime.combine(day, datetime.time(), finish.tzinfo)).total_seconds() / 3600
            if high > 0 and low < 24:
                spans.append((max(0.0, low), min(24.0, high)))
            break  # one window per night is enough to shade; scopes agree to the second
    return spans


def samples_chart(conn: sqlite.Connection, metric: str, date: str | None = None,
                  source_scope: str | None = None, width: int = 1000,
                  height: int = 420) -> Canvas:
    """Every sample of one metric across a single local day, with the night shaded.

    The x axis is the watch's own clock, 00:00 to 24:00. Sleep windows stored
    for that day are shaded behind the trace, which is what makes an overnight
    heart-rate or stress curve readable. Never raises for a day with no samples.
    """
    report = queries.intraday_samples(conn, metric, date, source_scope)
    unit = report["unit"] or ""
    day = report["date"]
    title = f"{metric}" + (f"  ({unit})" if unit else "")
    subtitle = f"{day}  intraday, watch local clock" if day else "intraday"
    series = [entry for entry in report["series"] if entry["points"]]
    if not series:
        return _empty_panel(width, height, title, subtitle,
                            report.get("reason", "no samples on that day"))

    values = [point["value"] for entry in series for point in entry["points"]]
    y_min, y_max = _y_range(values)
    total = sum(len(entry["points"]) for entry in series)
    offset = _local_offset(conn, parse_iso_utc(series[0]["points"][0]["ts_utc"]))
    windows = _sleep_windows(conn, day, offset)
    heading = f"{subtitle}   {total} samples"
    if windows:
        heading += "   shading = the stored sleep window"
    canvas = _new_canvas(width, height, title, heading, SCOPE_LEGEND)
    frame = _frame_for(canvas, 0, 24, y_min, y_max)

    for low, high in windows:
        canvas.fill_rect(frame.px(low), frame.top, frame.px(high) - frame.px(low),
                         frame.bottom - frame.top, NIGHT, 0.08)
    _draw_y_axis(frame, unit)
    _draw_hour_axis(frame)
    for entry in series:
        color = SCOPE_COLORS.get(entry["source_scope"], MUTED)
        points = [(point["hour"], point["value"]) for point in entry["points"]]
        # A sample cadence of 60 s means a 15-minute hole is a real gap in wear.
        _draw_series(frame, points, color, 0.25, 1.4)
    _draw_legend(canvas, frame, [(entry["source_scope"],
                                  SCOPE_COLORS.get(entry["source_scope"], MUTED))
                                 for entry in series])
    return canvas
