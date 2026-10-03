"""Scrub identifiers from free text before it is stored or shown.

Error messages are the classic leak: an OSError quotes the full path of a file
whose name embeds the account email, and a decoder failure quotes a serial.
Everything that stores or emits free text runs it through :func:`redact_text`.
Stripping is loud about itself: placeholders stay visible, so a redacted
message is distinguishable from a message that never held anything.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_NUMBER = re.compile(r"(?<![\d.])\d{7,}(?!\d|\.\d)")  # ids, not decimals like 12.3456789
_POSIX_PATH = re.compile(r"(?:(?<=\s)|^|(?<=['\"(:]))/(?:[^\s'\"()]+/)+[^\s'\"()]*")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\(?:[^\s'\"()\\]+\\)*[^\s'\"()\\]*")


def redact_text(text: str | None) -> str | None:
    """Replace emails, long identifiers and absolute paths with placeholders; None stays None."""
    if text is None:
        return None
    text = _EMAIL.sub("{email}", text)
    text = _POSIX_PATH.sub("{path}", text)
    text = _WINDOWS_PATH.sub("{path}", text)
    return _LONG_NUMBER.sub("{id}", text)
