"""Dependency-free 2D raster canvas that encodes PNG images.

This platform is a local-first, cloud-free health platform: it must run fully offline,
on a machine that may never see a package index, and it must never drag in a
heavyweight or licence-encumbered dependency just to draw a chart. This module
therefore renders 2D shapes and text and encodes them straight to PNG bytes using
only the Python standard library (``zlib`` for DEFLATE/CRC32, ``struct`` for binary
packing, ``math`` for geometry). No matplotlib, no Pillow, no numpy, no third-party
package of any kind.

Anti-aliasing is achieved purely by supersampling: every :class:`Canvas` keeps an
internal RGB buffer at ``scale`` times the requested resolution and box-filters it
down to the final image in :meth:`Canvas.to_png`. There is no analytic (Wu-style)
line or edge anti-aliasing anywhere in this module by design -- supersampling is
simpler to reason about, is trivially correct for every primitive (rectangles,
strokes, circles, text), and needs no per-shape special casing.

Text is rendered from a hand-authored 5x7 monospace bitmap font embedded in this
module (see ``_FONT_5X7``) covering every printable ASCII character (0x20..0x7E).
"""

from __future__ import annotations

import math
import os
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

RGB = tuple[int, int, int]

_PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))

_FONT_GLYPH_WIDTH = 5
_FONT_GLYPH_HEIGHT = 7
_FONT_TRACKING_COLUMNS = 1

_VALID_TEXT_ANCHORS = frozenset({"left", "center", "right"})
_VALID_TEXT_BASELINES = frozenset({"top", "middle", "bottom"})


def _clamp_channel(value: float) -> int:
    """Round ``value`` to the nearest int and clamp it to the 0..255 byte range."""
    return max(0, min(255, round(value)))


def _validate_rgb(color: RGB) -> None:
    """Raise ``ValueError`` unless ``color`` is a 3-tuple of ints in 0..255."""
    if len(color) != 3:
        raise ValueError(f"color must be an (r, g, b) tuple of length 3, got {color!r}")
    for channel in color:
        if not isinstance(channel, int) or not (0 <= channel <= 255):
            raise ValueError(
                f"color channels must be integers in 0..255, got {color!r}"
            )


def _stroke_quad(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    half_width: float,
) -> tuple[tuple[float, float], ...]:
    """Return the four corners of a butt-capped stroke around a non-empty segment.

    The stroke is the rectangle swept perpendicular to ``start``-``end`` by
    ``half_width`` on each side, with flat caps at the two endpoints. Corners come
    back in order, so the result is a convex polygon ready for scanline filling.
    The segment must have non-zero length; a zero-length one is a disc, not a
    rectangle, and its callers route it to :meth:`Canvas._collect_disc` instead.
    """
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length = math.hypot(delta_x, delta_y)
    normal_x = -delta_y / length * half_width
    normal_y = delta_x / length * half_width
    return (
        (start_x + normal_x, start_y + normal_y),
        (end_x + normal_x, end_y + normal_y),
        (end_x - normal_x, end_y - normal_y),
        (start_x - normal_x, start_y - normal_y),
    )


def _convex_row_span(
    corners: Sequence[tuple[float, float]], row_center_y: float
) -> tuple[float, float] | None:
    """Return the x-interval where ``y = row_center_y`` crosses a convex polygon.

    ``None`` means the row misses the polygon entirely. Because the polygon is
    convex the crossings always bound a single interval, so taking the extremes
    of the edge intersections is exact rather than an approximation.
    """
    lowest: float | None = None
    highest: float | None = None
    count = len(corners)
    for index in range(count):
        corner_x, corner_y = corners[index]
        next_x, next_y = corners[(index + 1) % count]
        if corner_y == next_y:
            if corner_y != row_center_y:
                continue
            crossings: tuple[float, ...] = (corner_x, next_x)
        elif min(corner_y, next_y) <= row_center_y <= max(corner_y, next_y):
            along = (row_center_y - corner_y) / (next_y - corner_y)
            crossings = (corner_x + along * (next_x - corner_x),)
        else:
            continue
        for crossing in crossings:
            if lowest is None or crossing < lowest:
                lowest = crossing
            if highest is None or crossing > highest:
                highest = crossing
    if lowest is None or highest is None:
        return None
    return lowest, highest


def _encode_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Encode one PNG chunk: 4-byte length, type, data, 4-byte CRC32 over type+data."""
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _encode_png(width: int, height: int, rgb_pixels: bytes | bytearray) -> bytes:
    """Encode a flat RGB pixel buffer as an 8-bit truecolour, non-interlaced PNG.

    ``rgb_pixels`` must contain exactly ``width * height * 3`` bytes in row-major
    order with no padding. Returns the complete PNG file as bytes (signature,
    IHDR, a single IDAT with zlib level 6, IEND).
    """
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw_scanlines = bytearray()
    for row in range(height):
        raw_scanlines.append(0)  # filter type 0 ("None") for every scanline
        row_start = row * stride
        raw_scanlines.extend(rgb_pixels[row_start : row_start + stride])
    idat_data = zlib.compress(bytes(raw_scanlines), 6)
    return (
        _PNG_SIGNATURE
        + _encode_chunk(b"IHDR", ihdr_data)
        + _encode_chunk(b"IDAT", idat_data)
        + _encode_chunk(b"IEND", b"")
    )


class Canvas:
    """A raster canvas that draws primitives and text, then encodes itself as PNG.

    All public drawing methods take coordinates and sizes in *final* pixels (they
    may be floats). Internally the canvas keeps a supersampled RGB buffer of
    ``(width * scale) x (height * scale)`` pixels; :meth:`to_png` box-filters that
    buffer down to ``width x height`` as the sole anti-aliasing step. Drawing that
    falls partly or wholly outside the canvas is silently clipped and never raises.
    """

    def __init__(
        self,
        width: int,
        height: int,
        background: RGB = (255, 255, 255),
        scale: int = 3,
    ) -> None:
        """Create a blank canvas of ``width x height`` final pixels.

        Args:
            width: Final image width in pixels. Must be positive.
            height: Final image height in pixels. Must be positive.
            background: Fill colour for the whole canvas as an (r, g, b) tuple.
            scale: Supersampling factor; the internal buffer is
                ``width * scale`` by ``height * scale``. Must be positive.

        Raises:
            ValueError: If ``width``, ``height``, or ``scale`` is not positive,
                or if ``background`` is not a valid RGB tuple.
        """
        if width <= 0:
            raise ValueError(f"width must be positive, got {width}")
        if height <= 0:
            raise ValueError(f"height must be positive, got {height}")
        if scale <= 0:
            raise ValueError(f"scale must be positive, got {scale}")
        _validate_rgb(background)

        self.width = width
        self.height = height
        self._scale = scale
        self._buffer_width = width * scale
        self._buffer_height = height * scale
        self._buffer = bytearray(bytes(background) * (self._buffer_width * self._buffer_height))

    def _blend_pixel(self, x: int, y: int, color: RGB, alpha: float) -> None:
        """Composite ``color`` over the supersampled pixel at ``(x, y)``.

        ``(x, y)`` must already be a valid, in-bounds supersampled-buffer index --
        every caller clips its iteration range to the buffer dimensions before
        calling this. ``alpha <= 0`` is unreachable in practice (callers no-op
        earlier) but is treated as a no-op for safety; ``alpha >= 1`` overwrites.
        """
        if alpha <= 0.0:
            return
        index = (y * self._buffer_width + x) * 3
        if alpha >= 1.0:
            self._buffer[index : index + 3] = bytes(color)
            return
        red, green, blue = color
        inverse_alpha = 1.0 - alpha
        self._buffer[index] = _clamp_channel(red * alpha + self._buffer[index] * inverse_alpha)
        self._buffer[index + 1] = _clamp_channel(
            green * alpha + self._buffer[index + 1] * inverse_alpha
        )
        self._buffer[index + 2] = _clamp_channel(
            blue * alpha + self._buffer[index + 2] * inverse_alpha
        )

    def _fill_block(
        self, left: int, top: int, right: int, bottom: int, color: RGB, alpha: float
    ) -> None:
        """Fill the half-open supersampled rectangle ``[left, right) x [top, bottom)``.

        Coordinates are clipped to the buffer bounds; a rectangle that ends up
        empty or fully offscreen after clipping is a silent no-op. Used by both
        :meth:`fill_rect` and the glyph-block rendering inside :meth:`text`.
        """
        left = max(0, left)
        top = max(0, top)
        right = min(self._buffer_width, right)
        bottom = min(self._buffer_height, bottom)
        if left >= right or top >= bottom or alpha <= 0.0:
            return
        if alpha >= 1.0:
            solid_row = bytes(color) * (right - left)
            for y in range(top, bottom):
                row_start = (y * self._buffer_width + left) * 3
                row_end = row_start + len(solid_row)
                self._buffer[row_start:row_end] = solid_row
        else:
            for y in range(top, bottom):
                for x in range(left, right):
                    self._blend_pixel(x, y, color, alpha)

    def _add_span(
        self, rows: dict[int, list[tuple[int, int]]], y: int, x_low: float, x_high: float
    ) -> None:
        """Record the pixels of one shape covered on supersampled row ``y``.

        A pixel counts as covered when its centre lies inside the shape, boundary
        included -- the same rule a point-in-shape test applies, evaluated
        analytically per row instead of once per candidate pixel. Spans are
        clipped to the buffer here, so nothing downstream has to bounds-check.
        """
        if y < 0 or y >= self._buffer_height:
            return
        left = max(0, math.ceil(x_low - 0.5))
        right = min(self._buffer_width, math.floor(x_high - 0.5) + 1)
        if left < right:
            rows.setdefault(y, []).append((left, right))

    def _collect_segment(
        self,
        rows: dict[int, list[tuple[int, int]]],
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        half_width: float,
    ) -> None:
        """Add the spans of one butt-capped stroke segment to ``rows``."""
        corners = _stroke_quad(start_x, start_y, end_x, end_y, half_width)
        corner_ys = [corner[1] for corner in corners]
        first_row = max(0, math.floor(min(corner_ys) - 0.5))
        last_row = min(self._buffer_height, math.ceil(max(corner_ys) + 0.5))
        for y in range(first_row, last_row):
            span = _convex_row_span(corners, y + 0.5)
            if span is not None:
                self._add_span(rows, y, span[0], span[1])

    def _collect_disc(
        self,
        rows: dict[int, list[tuple[int, int]]],
        center_x: float,
        center_y: float,
        radius: float,
    ) -> None:
        """Add the spans of a filled circle to ``rows``."""
        first_row = max(0, math.floor(center_y - radius - 0.5))
        last_row = min(self._buffer_height, math.ceil(center_y + radius + 0.5))
        for y in range(first_row, last_row):
            delta_y = (y + 0.5) - center_y
            if abs(delta_y) > radius:
                continue
            half_span = math.sqrt(radius * radius - delta_y * delta_y)
            self._add_span(rows, y, center_x - half_span, center_x + half_span)

    def _paint_spans(
        self, rows: dict[int, list[tuple[int, int]]], color: RGB, alpha: float
    ) -> None:
        """Composite every recorded span, merging overlaps so no pixel blends twice."""
        solid = alpha >= 1.0
        color_bytes = bytes(color)
        for y, spans in rows.items():
            spans.sort()
            run_left, run_right = spans[0]
            for left, right in spans[1:]:
                if left <= run_right:
                    if right > run_right:
                        run_right = right
                else:
                    self._blend_run(y, run_left, run_right, color_bytes, alpha, solid)
                    run_left, run_right = left, right
            self._blend_run(y, run_left, run_right, color_bytes, alpha, solid)

    def _blend_run(
        self, y: int, left: int, right: int, color_bytes: bytes, alpha: float, solid: bool
    ) -> None:
        """Composite ``color_bytes`` over ``[left, right)`` of supersampled row ``y``.

        The range is already clipped by :meth:`_add_span`. An opaque run is a
        single slice assignment; a translucent one walks the three channels.
        """
        start = (y * self._buffer_width + left) * 3
        length = (right - left) * 3
        if solid:
            self._buffer[start : start + length] = color_bytes * (right - left)
            return
        red, green, blue = color_bytes
        inverse_alpha = 1.0 - alpha
        buffer = self._buffer
        for index in range(start, start + length, 3):
            buffer[index] = _clamp_channel(red * alpha + buffer[index] * inverse_alpha)
            buffer[index + 1] = _clamp_channel(green * alpha + buffer[index + 1] * inverse_alpha)
            buffer[index + 2] = _clamp_channel(blue * alpha + buffer[index + 2] * inverse_alpha)

    def fill_rect(
        self, x: float, y: float, w: float, h: float, color: RGB, alpha: float = 1.0
    ) -> None:
        """Fill an axis-aligned rectangle at final-pixel coordinates ``(x, y)``.

        The rectangle spans ``[x, x + w) x [y, y + h)`` in final pixels. Clipped
        to the canvas; ``w <= 0``, ``h <= 0``, or ``alpha <= 0`` is a no-op.

        Raises:
            ValueError: If ``color`` is not a valid RGB tuple.
        """
        _validate_rgb(color)
        if w <= 0 or h <= 0 or alpha <= 0.0:
            return
        scale = self._scale
        left = round(x * scale)
        top = round(y * scale)
        right = round((x + w) * scale)
        bottom = round((y + h) * scale)
        self._fill_block(left, top, right, bottom, color, alpha)

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        color: RGB,
        width: float = 1.0,
        alpha: float = 1.0,
    ) -> None:
        """Draw a straight, butt-capped stroke from ``(x0, y0)`` to ``(x1, y1)``.

        ``width`` is the stroke thickness in final pixels, centred on the
        segment. A zero-length segment (``x0 == x1 and y0 == y1``) draws a
        filled dot of radius ``width / 2`` instead. Clipped to the canvas;
        ``alpha <= 0`` is a no-op.

        Raises:
            ValueError: If ``color`` is invalid or ``width`` is not positive.
        """
        _validate_rgb(color)
        if width <= 0:
            raise ValueError(f"line width must be positive, got {width}")
        if alpha <= 0.0:
            return
        if x0 == x1 and y0 == y1:
            self.dot(x0, y0, width / 2.0, color, alpha)
            return

        scale = self._scale
        half_width = (width * scale) / 2.0
        rows: dict[int, list[tuple[int, int]]] = {}
        self._collect_segment(
            rows, x0 * scale, y0 * scale, x1 * scale, y1 * scale, half_width
        )
        self._paint_spans(rows, color, alpha)

    def polyline(
        self,
        points: Sequence[tuple[float, float]],
        color: RGB,
        width: float = 1.0,
        alpha: float = 1.0,
    ) -> None:
        """Draw connected straight segments through ``points`` with round joins.

        Interior vertices get a round join (a filled circle of radius
        ``width / 2``) so consecutive segments meet with no gaps or double
        blending; every pixel of the whole polyline is composited at most once
        regardless of how many segments or joints cover it. An empty ``points``
        is a no-op; a single point draws a dot of radius ``width / 2``.

        Raises:
            ValueError: If ``color`` is invalid or ``width`` is not positive.
        """
        _validate_rgb(color)
        if width <= 0:
            raise ValueError(f"polyline width must be positive, got {width}")
        if alpha <= 0.0 or len(points) == 0:
            return
        if len(points) == 1:
            only_x, only_y = points[0]
            self.dot(only_x, only_y, width / 2.0, color, alpha)
            return

        scale = self._scale
        half_width = (width * scale) / 2.0
        supersampled_points = [(px * scale, py * scale) for px, py in points]
        rows: dict[int, list[tuple[int, int]]] = {}
        for start, end in zip(supersampled_points, supersampled_points[1:]):
            if start == end:
                self._collect_disc(rows, start[0], start[1], half_width)
            else:
                self._collect_segment(rows, start[0], start[1], end[0], end[1], half_width)
        for joint_x, joint_y in supersampled_points[1:-1]:
            self._collect_disc(rows, joint_x, joint_y, half_width)
        self._paint_spans(rows, color, alpha)

    def dot(self, x: float, y: float, radius: float, color: RGB, alpha: float = 1.0) -> None:
        """Draw a filled circle of ``radius`` final pixels centred at ``(x, y)``.

        Clipped to the canvas; ``alpha <= 0`` is a no-op.

        Raises:
            ValueError: If ``color`` is invalid or ``radius`` is not positive.
        """
        _validate_rgb(color)
        if radius <= 0:
            raise ValueError(f"dot radius must be positive, got {radius}")
        if alpha <= 0.0:
            return

        scale = self._scale
        rows: dict[int, list[tuple[int, int]]] = {}
        self._collect_disc(rows, x * scale, y * scale, radius * scale)
        self._paint_spans(rows, color, alpha)

    def _font_pixel_size(self, size: float) -> int:
        """Return the integer supersampled pixel size for one font-grid cell.

        Raises:
            ValueError: If ``size`` is not positive.
        """
        if size <= 0:
            raise ValueError(f"text size must be positive, got {size}")
        return max(1, round(size * self._scale / _FONT_GLYPH_HEIGHT))

    def text_width(self, s: str, size: float = 9.0) -> float:
        """Return the advance width, in final pixels, of ``s`` rendered at ``size``.

        Matches exactly what :meth:`text` draws, so callers can centre text
        reliably. Empty string has width 0.

        Raises:
            ValueError: If ``s`` contains a newline or ``size`` is not positive.
        """
        if "\n" in s:
            raise ValueError("text_width does not support newlines")
        pixel_size = self._font_pixel_size(size)
        if len(s) == 0:
            return 0.0
        font_columns = _FONT_GLYPH_WIDTH * len(s) + (len(s) - 1) * _FONT_TRACKING_COLUMNS
        return font_columns * pixel_size / self._scale

    def text_height(self, size: float = 9.0) -> float:
        """Return the rendered glyph cell height, in final pixels, for ``size``.

        Raises:
            ValueError: If ``size`` is not positive.
        """
        pixel_size = self._font_pixel_size(size)
        return _FONT_GLYPH_HEIGHT * pixel_size / self._scale

    def text(
        self,
        x: float,
        y: float,
        s: str,
        color: RGB,
        size: float = 9.0,
        anchor: str = "left",
        baseline: str = "top",
        alpha: float = 1.0,
    ) -> None:
        """Draw ``s`` left-to-right in the embedded 5x7 bitmap font.

        ``size`` is the glyph cell height in final pixels; glyph columns are
        separated by 1 blank column of tracking (a space advances one full
        cell). ``anchor`` controls horizontal placement of ``x`` relative to the
        text box (``"left"``, ``"center"``, or ``"right"``); ``baseline``
        controls vertical placement of ``y`` (``"top"``, ``"middle"``, or
        ``"bottom"``). Clipped to the canvas; ``alpha <= 0`` or an empty string
        is a no-op (after validation).

        Raises:
            ValueError: If ``s`` contains a newline, ``anchor``/``baseline`` is
                not a recognised value, ``size`` is not positive, ``color`` is
                invalid, or ``s`` contains a character outside the embedded font.
        """
        _validate_rgb(color)
        if "\n" in s:
            raise ValueError(r"text() does not support newlines, got a string containing '\n'")
        if anchor not in _VALID_TEXT_ANCHORS:
            raise ValueError(f"anchor must be one of {sorted(_VALID_TEXT_ANCHORS)}, got {anchor!r}")
        if baseline not in _VALID_TEXT_BASELINES:
            raise ValueError(
                f"baseline must be one of {sorted(_VALID_TEXT_BASELINES)}, got {baseline!r}"
            )
        pixel_size = self._font_pixel_size(size)
        if len(s) == 0 or alpha <= 0.0:
            return

        total_width = self.text_width(s, size)
        total_height = self.text_height(size)

        if anchor == "left":
            start_x = x
        elif anchor == "center":
            start_x = x - total_width / 2.0
        else:
            start_x = x - total_width

        if baseline == "top":
            start_y = y
        elif baseline == "middle":
            start_y = y - total_height / 2.0
        else:
            start_y = y - total_height

        scale = self._scale
        origin_x = round(start_x * scale)
        origin_y = round(start_y * scale)
        cell_stride = (_FONT_GLYPH_WIDTH + _FONT_TRACKING_COLUMNS) * pixel_size

        for char_index, character in enumerate(s):
            glyph = _FONT_5X7.get(character)
            if glyph is None:
                raise ValueError(
                    f"no glyph defined for character {character!r} (U+{ord(character):04X})"
                )
            rows = glyph.split("/")
            cell_left = origin_x + char_index * cell_stride
            for row_index, row in enumerate(rows):
                cell_top = origin_y + row_index * pixel_size
                for col_index, pixel in enumerate(row):
                    if pixel != "#":
                        continue
                    block_left = cell_left + col_index * pixel_size
                    self._fill_block(
                        block_left,
                        cell_top,
                        block_left + pixel_size,
                        cell_top + pixel_size,
                        color,
                        alpha,
                    )

    def _downsample(self) -> bytearray:
        """Box-filter the supersampled buffer down to ``width x height`` RGB bytes."""
        scale = self._scale
        width, height = self.width, self.height
        buffer_width = self._buffer_width
        block_area = scale * scale
        result = bytearray(width * height * 3)

        for out_y in range(height):
            base_y = out_y * scale
            for out_x in range(width):
                base_x = out_x * scale
                red_total = green_total = blue_total = 0
                for dy in range(scale):
                    row_start = ((base_y + dy) * buffer_width + base_x) * 3
                    for dx in range(scale):
                        index = row_start + dx * 3
                        red_total += self._buffer[index]
                        green_total += self._buffer[index + 1]
                        blue_total += self._buffer[index + 2]
                out_index = (out_y * width + out_x) * 3
                result[out_index] = round(red_total / block_area)
                result[out_index + 1] = round(green_total / block_area)
                result[out_index + 2] = round(blue_total / block_area)

        return result

    def to_png(self) -> bytes:
        """Box-filter down to final resolution and encode as PNG bytes.

        Returns:
            A complete, valid 8-bit truecolour (colour type 2, no interlace)
            PNG file: signature, IHDR, one zlib-level-6 IDAT, IEND, with a
            correct CRC32 on every chunk.
        """
        final_pixels = self._downsample()
        return _encode_png(self.width, self.height, final_pixels)

    def write_png(self, path: str | os.PathLike[str]) -> None:
        """Encode this canvas as PNG and write it to ``path``, overwriting it."""
        Path(path).write_bytes(self.to_png())


# Hand-authored 5x7 bitmap font covering every printable ASCII character
# (0x20 .. 0x7E). Each glyph is 7 row-strings of exactly 5 characters ('#' =
# ink, '.' = blank) joined by '/'. Rows are numbered 0 (top) .. 6 (bottom).
# Caps and digits occupy rows 0..5 with row 6 left blank (baseline at row 5).
# Lowercase x-height letters occupy rows 1..5 (row 0 and row 6 blank), so
# their baseline lines up with caps/digits. Lowercase ascenders (b d f h k l
# t) occupy rows 0..5 like caps. Lowercase descenders (g j p q y) occupy rows
# 1..6, dropping below the shared baseline. This table is written by hand,
# not generated; use _validate_font() (called from the proof script, not at
# import time) to check its shape.
_FONT_5X7: dict[str, str] = {
    " ": ".....​/.....​/.....​/.....​/.....​/.....​/.....".replace("​", ""),
    "!": ".#.../.#.../.#.../.#.../...../.#.../.....",
    '"': ".#.#./.#.#./...../...../...../...../.....",
    "#": ".#.#./#####/.#.#./#####/.#.#./...../.....",
    "$": "..#../.####/#.#../.###./..#.#/####./..#..",
    "%": "##.../##..#/...#./..#../.#.../#..##/...##",
    "&": ".##../#..#./.##../#..#./#..#./.##.#/.....",
    "'": ".#.../.#.../...../...../...../...../.....",
    "(": "..#../.#.../.#.../.#.../.#.../.#.../..#..",
    ")": "..#../...#./...#./...#./...#./...#./..#..",
    "*": "..#../#.#.#/.###./#####/.###./#.#.#/..#..",
    "+": "...../..#../..#../#####/..#../..#../.....",
    ",": "...../...../...../...../...../..#../.#...",
    "-": "...../...../...../#####/...../...../.....",
    ".": "...../...../...../...../...../.#.../.....",
    "/": "....#/...#./..#../..#../.#.../.#.../#....",
    "0": ".###./#...#/#..##/#.#.#/##..#/.###./.....",
    "1": "..#../.##../..#../..#../..#../.###./.....",
    "2": ".###./#...#/...#./..#../.#.../#####/.....",
    "3": ".###./#...#/..##./...#./#...#/.###./.....",
    "4": "...#./..##./.#.#./#..#./#####/...#./.....",
    "5": "#####/#..../####./....#/#...#/.###./.....",
    "6": "..##./.#.../#..../####./#...#/.###./.....",
    "7": "#####/....#/...#./..#../..#../..#../.....",
    "8": ".###./#...#/.###./#...#/#...#/.###./.....",
    "9": ".###./#...#/#...#/.####/....#/.##../.....",
    ":": "...../.#.../...../.#.../...../...../.....",
    ";": "...../.#.../...../.#.../.#.../#..../.....",
    "<": "...#./..#../.#.../#..../.#.../..#../...#.",
    "=": "...../#####/...../#####/...../...../.....",
    ">": "#..../.#.../..#../...#./..#../.#.../#....",
    "?": ".###./#...#/...#./..#../...../..#../.....",
    "@": ".###./#...#/#.###/#.#.#/#.###/#..../.####",
    "A": "..#../.#.#./#...#/#####/#...#/#...#/.....",
    "B": "####./#...#/####./#...#/#...#/####./.....",
    "C": ".####/#..../#..../#..../#..../.####/.....",
    "D": "####./#...#/#...#/#...#/#...#/####./.....",
    "E": "#####/#..../####./#..../#..../#####/.....",
    "F": "#####/#..../####./#..../#..../#..../.....",
    "G": ".####/#..../#..../#.###/#...#/.####/.....",
    "H": "#...#/#...#/#####/#...#/#...#/#...#/.....",
    "I": "#####/..#../..#../..#../..#../#####/.....",
    "J": "..###/...#./...#./...#./#..#./.##../.....",
    "K": "#...#/#..#./###../#..#./#..#./#...#/.....",
    "L": "#..../#..../#..../#..../#..../#####/.....",
    "M": "#...#/##.##/#.#.#/#...#/#...#/#...#/.....",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/.....",
    "O": ".###./#...#/#...#/#...#/#...#/.###./.....",
    "P": "####./#...#/#...#/####./#..../#..../.....",
    "Q": ".###./#...#/#...#/#.#.#/#..#./.##.#/.....",
    "R": "####./#...#/#...#/####./#..#./#...#/.....",
    "S": ".####/#..../.###./....#/....#/####./.....",
    "T": "#####/..#../..#../..#../..#../..#../.....",
    "U": "#...#/#...#/#...#/#...#/#...#/.###./.....",
    "V": "#...#/#...#/#...#/#...#/.#.#./..#../.....",
    "W": "#...#/#...#/#.#.#/#.#.#/##.##/#...#/.....",
    "X": "#...#/.#.#./..#../..#../.#.#./#...#/.....",
    "Y": "#...#/.#.#./..#../..#../..#../..#../.....",
    "Z": "#####/...#./..#../.#.../#..../#####/.....",
    "[": ".##../.#.../.#.../.#.../.#.../.##../.....",
    "\\": "#..../.#.../.#.../..#../..#../...#./...#.",
    "]": ".##../...#./...#./...#./...#./.##../.....",
    "^": "..#../.#.#./#...#/...../...../...../.....",
    "_": "...../...../...../...../...../...../#####",
    "`": ".#.../..#../...../...../...../...../.....",
    "a": "...../.###./....#/.####/#...#/.####/.....",
    "b": "#..../#..../####./#...#/#...#/####./.....",
    "c": "...../.###./#..../#..../#..../.###./.....",
    "d": "....#/....#/.####/#...#/#...#/.####/.....",
    "e": "...../.###./#...#/#####/#..../.###./.....",
    "f": "..##./.#.../####./.#.../.#.../.#.../.....",
    "g": "...../.####/#...#/#...#/.####/....#/.###.",
    "h": "#..../#..../####./#...#/#...#/#...#/.....",
    "i": ".#.../...../.#.../.#.../.#.../.#.../.....",
    "j": "...#./...../...#./...#./...#./...#./##...",
    "k": "#..../#..../#..#./#.#../##.../#..#./.....",
    "l": ".#.../.#.../.#.../.#.../.#.../..##./.....",
    "m": "...../##.##/#.#.#/#.#.#/#.#.#/#.#.#/.....",
    "n": "...../.###./#...#/#...#/#...#/#...#/.....",
    "o": "...../.###./#...#/#...#/#...#/.###./.....",
    "p": "...../####./#...#/#...#/####./#..../#....",
    "q": "...../.####/#...#/#...#/.####/....#/....#",
    "r": "...../#.##./##.../#..../#..../#..../.....",
    "s": "...../.####/#..../.###./....#/####./.....",
    "t": ".#.../.#.../####./.#.../.#.../..##./.....",
    "u": "...../#...#/#...#/#...#/#...#/.###./.....",
    "v": "...../#...#/#...#/#...#/.#.#./..#../.....",
    "w": "...../#...#/#...#/#.#.#/#.#.#/##.##/.....",
    "x": "...../#...#/.#.#./..#../.#.#./#...#/.....",
    "y": "...../#...#/#...#/#...#/.####/....#/.###.",
    "z": "...../#####/...#./..#../.#.../#####/.....",
    "{": "..##./.#.../.#.../##.../.#.../.#.../..##.",
    "|": "..#../..#../..#../..#../..#../..#../..#..",
    "}": "##.../...#./...#./...##/...#./...#./##...",
    "~": "...../...../...../.##.#/#.##./...../.....",
}


def _validate_font() -> None:
    """Check that ``_FONT_5X7`` has exactly the printable ASCII glyphs, well formed.

    Verifies every character 0x20..0x7E has exactly one entry (no missing, no
    extra), and that every entry is exactly ``_FONT_GLYPH_HEIGHT`` rows of
    exactly ``_FONT_GLYPH_WIDTH`` characters drawn only from ``{'#', '.'}``.
    Not called at import time -- callers (the proof script, tests) call it
    explicitly so a malformed font fails loudly rather than silently at draw
    time.

    Raises:
        AssertionError: If any of the above checks fails.
    """
    expected_characters = {chr(code) for code in range(0x20, 0x7F)}
    actual_characters = set(_FONT_5X7.keys())

    missing = sorted(expected_characters - actual_characters)
    if missing:
        raise AssertionError(f"font is missing glyphs for: {missing!r}")

    extra = sorted(actual_characters - expected_characters)
    if extra:
        raise AssertionError(f"font has unexpected extra glyphs: {extra!r}")

    for character, glyph in _FONT_5X7.items():
        rows = glyph.split("/")
        if len(rows) != _FONT_GLYPH_HEIGHT:
            raise AssertionError(
                f"glyph {character!r} has {len(rows)} rows, expected {_FONT_GLYPH_HEIGHT}"
            )
        for row in rows:
            if len(row) != _FONT_GLYPH_WIDTH:
                raise AssertionError(
                    f"glyph {character!r} row {row!r} has length {len(row)}, "
                    f"expected {_FONT_GLYPH_WIDTH}"
                )
            if any(pixel not in "#." for pixel in row):
                raise AssertionError(
                    f"glyph {character!r} row {row!r} contains characters other than '#'/'.'"
                )
