"""The self-hosted relay: a folder. WebDAV, rsync or Syncthing carries it between machines."""

from __future__ import annotations

import os
import pathlib
import re
import secrets
from typing import Protocol

NAME = re.compile(r"^[0-9a-f]{64}/[0-9a-f]{32}$")
#: Largest object a reader will load: the plaintext cap plus the envelope. Anything bigger is the
#: operator's problem, not ours (``TooLarge`` → rejected as ``too_large``).
_EVICTED = re.compile(r"^\.([0-9a-f]{32})\.icloud$")
MAX_OBJECT = 64 * 1024 * 1024 + 4096


class TooLarge(ValueError):
    reason = "too_large"


class Relay(Protocol):
    def put(self, name: str, data: bytes) -> None: ...
    def get(self, name: str) -> bytes: ...
    def list(self, account: str) -> list[str]: ...
    def delete(self, name: str) -> None: ...


class FolderRelay:
    """Objects are files ``<root>/<account>/<32 hex>``. Writes go to a temp name in the same
    directory and are renamed into place, so a reader never sees a partial object; anything that
    does not match the name pattern (temp files, strangers) is ignored."""

    def __init__(self, root: pathlib.Path, create_root: bool = False):
        """``create_root`` is for a folder the first ``put`` may create from nothing (the relay this device serves, the
        legacy single relay, ``--relay``, pairing's push). Without it a ``put`` into a root that is not a directory (an
        unmounted volume) raises ``FileNotFoundError`` and creates nothing; only ``<root>/<account>`` is created."""
        self.root = pathlib.Path(root)
        self.create_root = create_root

    def put(self, name: str, data: bytes) -> None:
        if not NAME.fullmatch(name):
            raise ValueError("bad object name")
        target = self.root / name
        if self.create_root:
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            if not self.root.is_dir():
                raise FileNotFoundError("the relay folder is gone")
            target.parent.mkdir(exist_ok=True)
        temp = target.parent / f".tmp-{secrets.token_hex(8)}"
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)

    def get(self, name: str) -> bytes:
        if not NAME.fullmatch(name):
            raise ValueError("bad object name")
        path = self.root / name
        if path.stat().st_size > MAX_OBJECT:
            raise TooLarge("object larger than any bundle can be")
        return path.read_bytes()

    def delete(self, name: str) -> None:
        """Remove one object (``FileNotFoundError`` when it is not there). Nothing in a push or pull
        deletes; pairing and tests do."""
        if not NAME.fullmatch(name):
            raise ValueError("bad object name")
        (self.root / name).unlink()

    def list(self, account: str) -> list[str]:
        folder = self.root / account
        if not folder.is_dir():
            return []
        names = [f"{account}/{p.name}" for p in folder.iterdir() if p.is_file()]
        return sorted(n for n in names if NAME.fullmatch(n))

    def list_present(self, account: str) -> list[str]:
        """The names a heal may count as present: :meth:`list` plus an evicted cloud placeholder
        (``.<32 hex>.icloud``), which a ``get`` could not read but which is the same object."""
        names = set(self.list(account))
        folder = self.root / account
        if folder.is_dir():
            for item in folder.iterdir():
                found = _EVICTED.fullmatch(item.name)
                if found:
                    names.add(f"{account}/{found.group(1)}")
        return sorted(n for n in names if NAME.fullmatch(n))
