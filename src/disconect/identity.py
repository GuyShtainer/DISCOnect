"""Product identity strings: the only place the product name and the non-affiliation notice live.

Every surface that shows either (the serve protocol, the app, later the CLI and MCP) imports them
from here, so a rename or a wording change is one edit.

The names below the notice (data folder, db file, env prefix, keychain service, MCP server name,
command, legacy homes and backup prefixes) are mirrored in ``disconect-app/src/identity.ts`` and
``disconect-core/src/identity.rs``; ``tests/test_identity_drift.py`` fails when the three disagree.
The frozen wire constants (``docs/kb/24-wire-constants.md``) are NOT here: they never change.
"""

import re
from typing import Any

PRODUCT = "DISCOnect"
NOTICE = "Not affiliated with or endorsed by any watch manufacturer."

DATA_DIR = ".disconect"
DB_FILENAME = "disconect.db"
ENV_PREFIX = "DISCONECT_"
#: The Python core's keychain service. Distinct from the Rust core's ``KEYCHAIN_SERVICE`` on purpose
#: (docs/kb/23, class 5: macOS binds an item to the program that made it).
CLI_KEYCHAIN_SERVICE = "disconect-cli"
#: Service names an earlier build's Python core stored the master key under; ``key cache --remove`` and
#: ``key rotate-recovery`` delete these items too so no orphan keeps a copy of the key.
LEGACY_CLI_KEYCHAIN_SERVICES = ["hearthbeat"]
MCP_SERVER_NAME = "disconect"
COMMAND = "disconect"
#: Folders and backup prefixes of earlier builds, read through (never written) until Bet 2 publishes.
LEGACY_HOMES = [".hearthbeat"]
LEGACY_DB_FILENAME = "hearthbeat.db"
LEGACY_ENV_PREFIX = "HEARTHBEAT_"
BACKUP_PREFIX = "disconect"
LEGACY_BACKUP_PREFIXES = ["hearthbeat"]
#: Reported as ``serverInfo.version`` by the MCP server; the Rust crate's ``version`` (``Cargo.toml``) is the
#: same string, pinned by ``tests/test_identity_drift.py``. (The package's ``0.1.0.dev0`` is the Python wheel's.)
VERSION = "0.1.0"

#: The manufacturer's name (mirrored as ``MANUFACTURER_PATTERN`` in ``identity.rs``): no surface shows it.
MANUFACTURER = re.compile(r"garmin(?: connect)?", re.IGNORECASE)
VENDOR_PLACEHOLDER = "{vendor}"


def neutral(node: Any) -> Any:
    """Replace the manufacturer's name in every string of ``node`` (dicts, lists, strings); keys are kept."""
    if isinstance(node, dict):
        return {key: neutral(value) for key, value in node.items()}
    if isinstance(node, list):
        return [neutral(item) for item in node]
    if isinstance(node, str):
        return MANUFACTURER.sub(VENDOR_PLACEHOLDER, node)
    return node
