"""Where the Rust twin lives, when it is there at all.

The Python core in this repository is the oracle. Its Rust twin (the ``disconect-core`` crate), the desktop
and phone shell (``disconect-app``) and the two-core differential harness (``core_diff``, ``serve_diff`` and
``mcp_diff``, script files under the monorepo's ``tools/``) are developed beside it in one monorepo and are
not part of this repository. The cross-language pins the crate replays (``contract.json``, ``mcp.json``,
``read_paths.json``, ``py_numerics_read.json``, the shared key vectors) are committed with the crate.

A test that needs any of these calls ``require()`` first, or carries ``needs_monorepo``, and skips with one
reason in a standalone checkout. Nothing here reads a file: the paths are only resolved.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import types

import pytest

PROJECT = pathlib.Path(__file__).resolve().parents[1]
CRATE = PROJECT.parent / "disconect-core"
APP = PROJECT.parent / "disconect-app"
TOOLS = PROJECT.parents[1] / "tools"
BINARY = CRATE / "target" / "debug" / "disconect-core"

PRESENT = CRATE.is_dir() and APP.is_dir() and TOOLS.is_dir()
REASON = ("needs the Rust twin (disconect-core), the app shell or the two-core harness, which are developed "
          "in the monorepo and are not in this repository")

needs_monorepo = pytest.mark.skipif(not PRESENT, reason=REASON)
needs_binary = pytest.mark.skipif(not BINARY.exists(), reason="build disconect-core first (cargo build)")


def require() -> None:
    """Skip the calling module (or test) in a standalone checkout."""
    if not PRESENT:
        pytest.skip(REASON, allow_module_level=True)


def harness(name: str) -> types.ModuleType:
    """Import one of the differential-harness scripts (``tools/`` is a script directory, not a package)."""
    require()
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return importlib.import_module(name)
