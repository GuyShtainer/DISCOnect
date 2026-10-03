"""Where the data folder is: pure resolution, no side effects (Bet 02a).

``resolve_default_db`` only looks. It never creates, renames or moves anything, because it runs from
argparse defaults, ``--help``, every read-only MCP call and the app's verify steps. Moving the old
folder is ``disconect migrate-home`` (``storage/migrate_home.py``) and nothing else.

The Rust core (``disconect-core/src/serve.rs::resolve_default_db``) implements the same decision and
prints the same hint texts; ``tests/test_home.py`` and the Rust unit tests pin both.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Mapping

from disconect import identity

DB_ENV = identity.ENV_PREFIX + "DB"
#: Env names of the old builds. They are not aliases: nothing reads them, we only warn.
LEGACY_ENV_SUFFIXES = ("DB", "KEYS", "PASSPHRASE", "RECOVERY_WORDS")
RELAY_CONFIG_NAME = "relay.json"


def resolve_default_db() -> tuple[pathlib.Path, str | None]:
    """``(db path, legacy folder name or None)``.

    ``$DISCONECT_DB`` if set; else ``~/.disconect/disconect.db`` if that *file* exists; else the same
    file name of an old build's folder (``~/.hearthbeat/hearthbeat.db``) if it exists, reported as the
    second element so a caller can hint once; else the new path (which may not exist yet).
    """
    override = os.environ.get(DB_ENV)
    if override:
        return pathlib.Path(override).expanduser(), None
    home = pathlib.Path.home()
    current = home / identity.DATA_DIR / identity.DB_FILENAME
    if not current.is_file():
        for legacy in identity.LEGACY_HOMES:
            candidate = home / legacy / identity.LEGACY_DB_FILENAME
            if candidate.is_file():
                return candidate, legacy
    return current, None


def legacy_home_hint(legacy: str) -> str:
    """The one stderr line printed when the old folder is being read through."""
    return f"using legacy data folder ~/{legacy}; run '{identity.COMMAND} migrate-home' to move it"


def relay_config_path() -> pathlib.Path:
    """``relay.json`` lives in the folder of the resolved database file."""
    return resolve_default_db()[0].parent / RELAY_CONFIG_NAME


def legacy_env_warning(environ: Mapping[str, str] | None = None) -> str | None:
    """A one-line warning naming the new variables when any old ``HEARTHBEAT_*`` one is set, else None."""
    environ = os.environ if environ is None else environ
    stale = [suffix for suffix in LEGACY_ENV_SUFFIXES if environ.get(identity.LEGACY_ENV_PREFIX + suffix)]
    if not stale:
        return None
    old = ", ".join(identity.LEGACY_ENV_PREFIX + s for s in stale)
    new = ", ".join(identity.ENV_PREFIX + s for s in stale)
    return f"warning: {old} {'is' if len(stale) == 1 else 'are'} no longer read; set {new} instead"


def announce_default_resolution(db_path: str | os.PathLike) -> None:
    """Print the env warning and, when ``db_path`` is the legacy default, the legacy hint (stderr only).

    Call once per process, after argument parsing, so ``--help`` and ``--version`` stay silent.
    """
    warning = legacy_env_warning()
    if warning:
        print(warning, file=sys.stderr)
    resolved, legacy = resolve_default_db()
    if legacy and pathlib.Path(db_path) == resolved:
        print(legacy_home_hint(legacy), file=sys.stderr)
