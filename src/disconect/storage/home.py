"""Where the data folder is: pure resolution, no side effects.

``resolve_default_db`` only looks. It never creates, renames or moves anything, because it runs from
argparse defaults, ``--help``, every read-only MCP call and the app's verify steps. Moving the old
folder is ``disconect migrate-home`` (``storage/migrate_home.py``) and nothing else.

The Rust core (``disconect-core/src/home.rs``) implements the same decision and
prints the same hint texts; ``tests/test_home.py`` and the Rust unit tests pin both.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Mapping

from disconect import identity
from disconect.storage.errors import HomeMoved, NoHome


def expand_user(text: str) -> pathlib.Path:
    """A leading ``~`` (bare or ``~/…``) stands for ``$HOME``, exactly like the Rust core's ``keys::expand_user``:
    ``~name/…`` is left alone (``Path.expanduser`` would look ``name`` up in the password database and the two cores
    would open different folders), and with no ``$HOME`` (or an empty one) the ``~`` stays as written (no
    password-database fallback). The text is tested, not its parts: ``./~/a`` is a folder named ``~``."""
    home = home_dir()
    if (text == "~" or text.startswith("~/")) and home:
        return pathlib.Path(home).joinpath(*pathlib.Path(text).parts[1:])
    return pathlib.Path(text)


def home_dir() -> str | None:
    """``$HOME``, or None when it is unset or empty. Never ``Path.home()``: that falls back to the password database
    when ``$HOME`` is unset, which the Rust core does not, so the two cores would open different stores."""
    return os.environ.get("HOME") or None


DB_ENV = identity.ENV_PREFIX + "DB"
#: Env names of the old builds. They are not aliases: nothing reads them, we only warn.
LEGACY_ENV_SUFFIXES = ("DB", "KEYS", "PASSPHRASE", "RECOVERY_WORDS")
RELAY_CONFIG_NAME = "relay.json"
HOME_NOT_SET = "HOME is not set"
#: The Rust binary's text for "no default store" (``disconect-core.rs``); the CLI prefixes ``error: ``.
NO_STORE_TEXT = f"no store: pass --db <path> or set {identity.ENV_PREFIX}DB (HOME is not set)"


def resolve_default_db() -> tuple[pathlib.Path, str | None]:
    """``(db path, legacy folder name or None)``.

    Raises :class:`~disconect.storage.errors.NoHome` when ``$DISCONECT_DB`` is not set and ``$HOME`` is unset or empty.

    ``$DISCONECT_DB`` if set; else ``~/.disconect/disconect.db`` if that *file* exists; else the same
    file name of an old build's folder (``~/.hearthbeat/hearthbeat.db``, or ``disconect.db`` there when a
    ``migrate-home`` stopped half way) if it exists, reported as the second element so a caller can hint once; else the new path (which may not exist yet).
    """
    override = os.environ.get(DB_ENV)
    if override:
        return expand_user(override), None
    home_text = home_dir()
    if home_text is None:
        raise NoHome(NO_STORE_TEXT)
    home = pathlib.Path(home_text)
    current = home / identity.DATA_DIR / identity.DB_FILENAME
    if not current.is_file():
        for legacy in identity.LEGACY_HOMES:
            # the database under its old name, or under its new one in a half-done migrate-home
            # (siblings renamed, folder not yet)
            for name in (identity.LEGACY_DB_FILENAME, identity.DB_FILENAME):
                candidate = home / legacy / name
                if candidate.is_file():
                    return candidate, legacy
    return current, None


def ensure_parent_dir(path: pathlib.Path) -> None:
    """Create the folder of ``path`` for a writer, except an old data folder that is gone.

    Raises :class:`~disconect.storage.errors.HomeMoved` when the folder is missing and is one of
    ``identity.LEGACY_HOMES``: ``migrate-home`` moved it, and re-creating it would split the data.
    """
    parent = pathlib.Path(path).parent
    if parent.is_dir():
        return
    if parent.name in identity.LEGACY_HOMES:
        raise HomeMoved(f"data folder ~/{parent.name} has moved; restart {identity.PRODUCT}")
    parent.mkdir(parents=True, exist_ok=True)


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


def announce_legacy_env() -> None:
    """Print the old-name warning (stderr) when any ``HEARTHBEAT_*`` variable is set.

    Entry points call it once, before resolving the default store, so the warning survives a no-``$HOME``
    refusal, in the Rust binary's order (``legacy_env_warning_now()`` first, then the store).
    """
    warning = legacy_env_warning()
    if warning:
        print(warning, file=sys.stderr)


def announce_default_resolution(db_path: str | os.PathLike) -> None:
    """Print the legacy hint (stderr) when ``db_path`` is the old folder's default; the old-name warning is
    :func:`announce_legacy_env`, which the entry points print first.

    Call once per process, after argument parsing, so ``--help`` and ``--version`` stay silent.
    """
    try:
        resolved, legacy = resolve_default_db()
    except NoHome:
        return      # an explicit --db needs no home; nothing to hint about
    if legacy and pathlib.Path(db_path) == resolved:
        print(legacy_home_hint(legacy), file=sys.stderr)
