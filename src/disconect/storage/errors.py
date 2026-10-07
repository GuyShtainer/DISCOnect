"""Store-level exception classes, in a module with no imports of its own so every storage module can use them."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for store-level failures."""


class NotConfigured(StorageError):
    """No database exists yet: nothing has been imported."""


class HomeMoved(NotConfigured):
    """A writer was pointed into an old data folder that is gone (``migrate-home`` moved it).

    Creating it again would start a second, empty store next to the moved one (split data), so the
    writer stops; restarting the program resolves the new folder.
    """


class NoHome(StorageError):
    """There is no default store: ``$HOME`` is unset or empty and ``$DISCONECT_DB`` is not set.

    The password database is never consulted (the Rust core never does); the CLI prints it as ``error: <text>``, exit 1.
    """
