# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which relay a device uses: ``relay.json`` beside the store (``{"folder": path}`` or
``{"lan": "http://host:port"}``), shared with the Rust core. This core speaks folders only: a
``lan`` relay is refused with :class:`UnsupportedTransport` (the serve code ``unsupported_transport``),
the Rust core is the one that serves and reads the LAN relay (docs/relay-protocol.md, "LAN relay")."""

from __future__ import annotations

import json
import pathlib

from disconect.relay.folder import FolderRelay

LAN_TEXT = "a LAN relay needs disconect-core; this core reads folder relays only"


class UnsupportedTransport(Exception):
    """The configured relay needs a transport this core does not have."""


def is_lan_address(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def read(path: pathlib.Path) -> tuple[str, str] | None:
    """``("lan", url)`` or ``("folder", path)`` from ``relay.json``, or None when the file is missing,
    unreadable, not an object or names nothing. A non-empty ``lan`` string wins over ``folder``."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    for key in ("lan", "folder"):
        found = value.get(key)
        if isinstance(found, str) and found:
            return key, found
    return None


def open_relay(kind: str, value: str) -> FolderRelay:
    """The relay a configuration names; a LAN one raises :class:`UnsupportedTransport`."""
    if kind == "lan":
        raise UnsupportedTransport(LAN_TEXT)
    return FolderRelay(pathlib.Path(value).expanduser())
