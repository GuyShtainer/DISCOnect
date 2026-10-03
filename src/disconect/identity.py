"""Product identity strings: the only place the product name and the non-affiliation notice live.

Every surface that shows either (the serve protocol, the app, later the CLI and MCP) imports them
from here, so a rename or a wording change is one edit.

The names below the notice (data folder, db file, env prefix, keychain service, MCP server name,
command, legacy homes and backup prefixes) are mirrored in ``disconect-app/src/identity.ts`` and
``disconect-core/src/identity.rs``; ``tests/test_identity_drift.py`` fails when the three disagree.
The frozen wire constants (``docs/kb/24-wire-constants.md``) are NOT here: they never change.
"""

PRODUCT = "DISCOnect"
NOTICE = "Not affiliated with or endorsed by any watch manufacturer."

DATA_DIR = ".disconect"
DB_FILENAME = "disconect.db"
ENV_PREFIX = "DISCONECT_"
#: The Python core's keychain service. Distinct from the Rust core's ``KEYCHAIN_SERVICE`` on purpose
#: (docs/kb/23, class 5: macOS binds an item to the program that made it).
CLI_KEYCHAIN_SERVICE = "disconect-cli"
MCP_SERVER_NAME = "disconect"
COMMAND = "disconect"
#: Folders and backup prefixes of earlier builds, read through (never written) until Bet 2 publishes.
LEGACY_HOMES = [".hearthbeat"]
BACKUP_PREFIX = "disconect"
LEGACY_BACKUP_PREFIXES = ["hearthbeat"]
