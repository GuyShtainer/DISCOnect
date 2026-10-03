"""Your watch's health data, kept at your own hearth.

Local-first, cloud-free store for Garmin watch data. FIT files (from a USB pull,
a Garmin Connect account export, or Gadgetbridge) are decoded with fitdecode
(MIT) into SQLite; the raw bytes are retained so any decoder fix is a replay,
never a re-pull. Every outlet (CLI, MCP) reads the same store under the same
contract (see `disconect.contract`).

Never depends on Garmin's FIT SDK -- its license forbids it.
"""

__version__ = "0.1.0.dev0"
