"""A tiny PNG reader for the tests: decodes what ``render.py`` writes, nothing more.

The renderer deliberately has no image dependency, so its tests must not gain one
either. This handles exactly the shape ``render.py`` emits -- 8-bit truecolour, no
interlace, filter byte 0 on every scanline -- and asserts loudly on anything else,
which is how the tests pin the file format itself.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WHITE = (255, 255, 255)

Pixel = tuple[int, int, int]
Rows = list[list[Pixel]]


def chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Return ``[(chunk_type, payload), ...]``, verifying the CRC32 of every chunk."""
    assert data[:8] == PNG_SIGNATURE, "not a PNG: bad signature"
    found = []
    position = 8
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        (stored,) = struct.unpack(">I", data[position + 8 + length:position + 12 + length])
        assert stored == zlib.crc32(kind + payload), f"bad CRC32 on the {kind!r} chunk"
        found.append((kind, payload))
        position += 12 + length
    assert position == len(data), "trailing bytes after the last chunk"
    return found


def decode(data: bytes) -> tuple[int, int, Rows]:
    """Decode to ``(width, height, rows)`` where ``rows[y][x]`` is an ``(r, g, b)`` tuple."""
    header = None
    compressed = bytearray()
    kinds = []
    for kind, payload in chunks(data):
        kinds.append(kind)
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed += payload
    assert kinds[0] == b"IHDR", f"first chunk is {kinds[0]!r}, not IHDR"
    assert kinds[-1] == b"IEND", f"last chunk is {kinds[-1]!r}, not IEND"
    assert header is not None

    width, height, depth, colour, compression, filtering, interlace = header
    assert depth == 8, f"bit depth {depth}, expected 8"
    assert colour == 2, f"colour type {colour}, expected 2 (truecolour)"
    assert (compression, filtering, interlace) == (0, 0, 0), header

    raw = zlib.decompress(bytes(compressed))
    stride = width * 3
    assert len(raw) == height * (stride + 1), (len(raw), height, stride)

    rows: Rows = []
    for y in range(height):
        start = y * (stride + 1)
        assert raw[start] == 0, f"row {y} uses filter {raw[start]}, expected 0 (None)"
        line = raw[start + 1:start + 1 + stride]
        rows.append([(line[x * 3], line[x * 3 + 1], line[x * 3 + 2]) for x in range(width)])
    return width, height, rows


def ink_bounds(rows: Rows, background: Pixel = WHITE) -> tuple[int, int, int, int] | None:
    """Inclusive ``(left, top, right, bottom)`` of the pixels that differ from ``background``.

    ``None`` when nothing was drawn.
    """
    left = top = right = bottom = None
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == background:
                continue
            if left is None or x < left:
                left = x
            if right is None or x > right:
                right = x
            if top is None:
                top = y
            bottom = y
    if left is None:
        return None
    return left, top, right, bottom


def is_blank(rows: Rows, background: Pixel = WHITE) -> bool:
    """True when every pixel still holds ``background``."""
    return ink_bounds(rows, background) is None
