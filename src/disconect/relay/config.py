# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which relay a device uses: ``relay.json`` beside the store (``{"folder": path}`` or
``{"lan": "http://host:port"}``), shared with the Rust core. This core speaks folders only: a
``lan`` relay is refused with :class:`UnsupportedTransport` (the serve code ``unsupported_transport``),
the Rust core is the one that serves and reads the LAN relay (docs/relay-protocol.md, "LAN relay")."""

from __future__ import annotations

import ipaddress
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


_LISTEN_SHAPE = "listen must be an IP address and a port, like 192.168.1.20:24816"
_LISTEN_ZONE = "listen cannot carry a zone id or a link-local address"
_LISTEN_PORT = "listen needs an explicit port from 1 to 65535"
_LISTEN_ANY = "listen needs a concrete address, not 0.0.0.0"


def _is_link_local(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return ip.packed[0] == 169 and ip.packed[1] == 254
    return (int(ip) >> 112) & 0xFFC0 == 0xFE80


def parse_listen(text: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    """``(ip, port)`` of a ``listen`` value: an IPv4 literal or a bracketed IPv6 literal, an explicit port
    1..65535 without a leading zero, never unspecified, link-local or carrying a zone id. The twin of the
    Rust core's ``parse_listen``; a refusal is a :class:`ValueError` whose text names the rule and never
    the value."""
    bracketed = text.startswith("[")
    if bracketed:
        host, found, port = text[1:].partition("]:")
    else:
        host, found, port = text.rpartition(":")
        if not found:
            raise ValueError(_LISTEN_SHAPE)
    if bracketed and not found:
        raise ValueError(_LISTEN_SHAPE)
    if "%" in host:
        raise ValueError(_LISTEN_ZONE)
    try:
        ip = ipaddress.IPv6Address(host) if bracketed else ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError(_LISTEN_SHAPE) from None
    if not (port and len(port) <= 5 and all(c in "0123456789" for c in port) and not port.startswith("0")
            and int(port) <= 65535):
        raise ValueError(_LISTEN_PORT)
    if ip.is_unspecified:
        raise ValueError(_LISTEN_ANY)
    if _is_link_local(ip):
        raise ValueError(_LISTEN_ZONE)
    return ip, int(port)
