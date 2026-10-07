# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which relays a device uses: ``relay.json`` beside the store, shared with the Rust core. The list form is
``{"relays": [{id, kind, path | url, label?, serve?}, ...]}``; the legacy forms ``{"folder": path}`` (one entry that
serves) and ``{"lan": "http://host:port"}`` still read. This core speaks folders only: a ``lan`` entry is a site
reported ``unsupported_transport`` (a list of one raises :class:`UnsupportedTransport`, the serve code
``unsupported_transport``); the Rust core is the one that serves and reads the LAN relay (docs/relay-protocol.md,
"LAN relay", "Relay list")."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import pathlib
import re
import secrets

from disconect.relay.folder import FolderRelay

LAN_TEXT = "a LAN relay needs disconect-core; this core reads folder relays only"


class UnsupportedTransport(Exception):
    """The configured relay needs a transport this core does not have."""


def is_lan_address(text: str) -> bool:
    return text.startswith(("http://", "https://"))


@dataclasses.dataclass(frozen=True)
class RelayEntry:
    """One relay of a device's list."""

    id: str            # ``default`` (a legacy file) or 1-32 lowercase hex characters (written: 8, see :func:`new_id`)
    kind: str          # "folder" | "lan"
    value: str         # the folder path or the LAN url
    label: str = ""
    serve: bool = False   # the entry this device serves to others: a folder, at most one per list


_ID = re.compile(r"[0-9a-f]{1,32}")


def valid_id(text: str) -> bool:
    """``default``, or 1-32 characters of ``[0-9a-f]``."""
    return text == "default" or _ID.fullmatch(text) is not None


def new_id() -> str:
    """A fresh entry id: 8 lowercase hex characters."""
    return secrets.token_hex(4)


def _text_of(obj: dict, key: str) -> str | None:
    found = obj.get(key)
    return found if isinstance(found, str) and found else None


def _parse_entry(value: object) -> RelayEntry | None:
    """One ``relays`` element, or None when it is malformed (the whole file is then unreadable)."""
    if not isinstance(value, dict):
        return None
    ident = value.get("id")
    if not isinstance(ident, str) or not valid_id(ident):
        return None
    kind = value.get("kind")
    if kind == "folder":
        place = _text_of(value, "path")
    elif kind == "lan":
        place = _text_of(value, "url")
    else:
        return None
    if place is None:
        return None
    label = value.get("label", "")
    if not isinstance(label, str):
        return None
    serve = value.get("serve", False)
    if not isinstance(serve, bool):
        return None
    if serve and kind == "lan":
        return None
    return RelayEntry(ident, kind, place, label, serve)


def read_list(path: pathlib.Path) -> list[RelayEntry] | None:
    """The list in ``relay.json``, or None when the file is missing, unreadable, not an object, or names nothing.

    ``{"relays": [...]}`` wins when ``relays`` is an array (the legacy keys are then ignored). Any malformed entry,
    a repeated id, more than one ``serve: true`` or an empty array makes the whole file unreadable. A legacy
    ``{"folder": ...}`` is the one entry ``default`` with ``serve: true``; a legacy ``{"lan": ...}`` is the one entry
    ``default`` (a non-empty ``lan`` string wins over ``folder``, as ever)."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    items = value.get("relays")
    if isinstance(items, list):
        entries = []
        for item in items:
            entry = _parse_entry(item)
            if entry is None:
                return None
            entries.append(entry)
        ids = {entry.id for entry in entries}
        serving = sum(1 for entry in entries if entry.serve)
        return entries if entries and len(ids) == len(entries) and serving <= 1 else None
    lan, folder = _text_of(value, "lan"), _text_of(value, "folder")
    if lan is not None:
        return [RelayEntry("default", "lan", lan, "", False)]
    if folder is not None:
        return [RelayEntry("default", "folder", folder, "", True)]
    return None


def read(path: pathlib.Path) -> tuple[str, str] | None:
    """What a legacy single-relay reader sees: ``(kind, place)`` of the first entry."""
    entries = read_list(path)
    return (entries[0].kind, entries[0].value) if entries else None


def write_list(path: pathlib.Path, entries: list[RelayEntry]) -> None:
    """Write ``entries`` as the list form (sorted keys, ``label`` only when set, ``serve`` only when true), creating the
    parent folder. More than one ``serve`` entry is refused (``ValueError``)."""
    if sum(1 for entry in entries if entry.serve) > 1:
        raise ValueError("only one relay can serve")
    items = []
    for entry in entries:
        item = {"id": entry.id, "kind": entry.kind, ("path" if entry.kind == "folder" else "url"): entry.value}
        if entry.label:
            item["label"] = entry.label
        if entry.serve:
            item["serve"] = True
        items.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"relays": items}, sort_keys=True) + "\n")


def serve_entry(entries: list[RelayEntry]) -> RelayEntry | None:
    """The entry this device serves: the one with ``serve: true``, when it is a folder."""
    return next((entry for entry in entries if entry.serve and entry.kind == "folder"), None)


def first_lan_url(entries: list[RelayEntry]) -> str | None:
    """The address of the first ``lan`` entry as configured (``sync.status.relay_url`` shows its base)."""
    return next((entry.value for entry in entries if entry.kind == "lan"), None)


def open_entry(entry: RelayEntry) -> FolderRelay:
    """:func:`open_relay` on an entry."""
    return open_relay(entry.kind, entry.value)


def parse_base_url(text: str) -> str:
    """``http://host[:port]`` (no path, query, user info or TLS), without a trailing slash: the twin of the Rust
    core's ``parse_base_url``. The :class:`ValueError` text names the rule, never the address."""
    rest = text.strip()
    if rest.startswith("https://"):
        raise ValueError("a LAN relay is plain http:// (the bodies are encrypted; https is not supported)")
    if not rest.startswith("http://"):
        raise ValueError("a LAN relay URL looks like http://host:port")
    rest = rest[len("http://"):]
    rest = rest[:-1] if rest.endswith("/") else rest
    if not rest or not all(c.isascii() and (c.isalnum() or c in ".-:[]") for c in rest):
        raise ValueError("a LAN relay URL is http://host[:port] with no path")
    return f"http://{rest}"


def lan_base_url(text: str) -> str | None:
    """:func:`parse_base_url`, or None for a refused address (nothing is shown for it)."""
    try:
        return parse_base_url(text)
    except ValueError:
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
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        raise ValueError(_LISTEN_SHAPE)
    if not (port and len(port) <= 5 and all(c in "0123456789" for c in port) and not port.startswith("0")
            and int(port) <= 65535):
        raise ValueError(_LISTEN_PORT)
    if ip.is_unspecified:
        raise ValueError(_LISTEN_ANY)
    if _is_link_local(ip):
        raise ValueError(_LISTEN_ZONE)
    return ip, int(port)
