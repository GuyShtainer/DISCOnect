# Status

**v0 core, in progress.** Last updated 2026-10-08. This file says what is validated, on what, and
what is not; the README describes the features.

## Validated on a real account (2026-09-10)

One real Garmin Connect *Export Your Data* archive from the maintainer's own watch: 426 FIT files,
three weeks of wear, zero decode failures, import in about five seconds.

- Ingest of the wellness FIT files and the export's JSON layer; the SQLite store; `reparse`
  (a replay with an unchanged decoder reproduced the store exactly, all tables); the contract;
  the facts engine; the CLI; the MCP server.
- Cross-checked against the vendor's own daily figures for the same days (stored side by side as
  `device` and `vendor_cloud`, reported by `disconect status`): resting heart rate, sleep score,
  sleep window, HRV weekly average, VO2max and the day's minimum and maximum heart rate matched
  exactly on every day; the daily stress average within rounding; sleep stage totals exactly for
  deep and awake and within 30 seconds for light and REM; daily steps on 20 of 21 days (the other
  differed by under a hundred, most likely a source the cloud merged in); distance within a metre.
- The energy-reserve decode (`stress_level` field 3 in the monitoring files) was correlated
  black-box against the export's daily figures on that one watch; the agreement is reported per
  day by `disconect status`, never assumed.

Since then the maintainer has used the CLI, the charts and the MCP server on his own store; that
use is not a test and is not claimed as one.

## Built since, validated on synthetic data only (2026-10-03 … 2026-10-08)

Covered by the test suite in this repository (748 tests on synthetic fixtures) and, in the private
development tree, by differential tests that hold a Rust port to the same answers:

- Encryption at rest (SQLCipher; Argon2id-wrapped master key; 24 recovery words; keychain cache).
  The maintainer's own store was converted on 2026-10-06 and has been in daily use since; that is
  use, not a test.
- The sync relay (encrypted raw-record bundles in a folder; push, pull, status; the relay list;
  convergence: daily rows are a pure function of the converged raw set in any arrival order).
- Device pairing (ephemeral X25519, commit-reveal, short authentication string; pinned by
  known-answer vectors). Exercised once end to end between two of the maintainer's devices on
  2026-10-08 through the unpublished shells; the Python side of that exchange is what ships here.
- The serve protocol (JSON lines over stdio for a shell: data reads, import with progress and
  cancel, key management, pairing, sync, `tools.call`).
- Live-link session files (`live-*.jsonl`) as raw records and their per-minute fold.
- Hardening rows pinned on both cores: calendar bounds (0001-01-01 and 9999-12-31), malformed
  timestamps in hand-edited stores, clock-offset edge cases, interrupted imports.

## Not in this repository

- **The Rust port** (`disconect-core`) and **the desktop and phone shells** (`disconect-app`,
  Tauri 2). The Python core is their oracle; 28 tests here skip because they need them
  (`tests/monorepo.py`). The docs under `docs/` describe the whole design, including those parts.
- **Anything that talks to the watch over Bluetooth.** The live-link client that writes
  `live-*.jsonl` files is a separate, unpublished experiment; this package only ingests the files.
- **The USB/MTP pull.** Copy the watch's `GARMIN/` tree yourself (on macOS the watch does not
  mount; a third-party MTP client works) and point `disconect import` at it.
- **Serving a relay over the local network** (`relay-serve` is a command of the Rust core). The
  Python CLI reads relay folders only.

## Known gaps

- The vendor's energy and readiness figures from device data are recompute targets, not done.
- Naps, per-second activity records and routes are not stored, by design for now.
- `sqlcipher3` must build or ship a wheel for your platform; on an unsupported platform the
  install fails before anything runs.

## How this snapshot was made

The package is developed inside a private monorepo beside the Rust port and the shells. This
repository starts at one snapshot commit of that package directory, with the tests made to run
standalone, the internal planning notes left behind, and the trademark and licensing files added;
the development history stays in the private tree. Commits are authored by the maintainer under
his GitHub no-reply address.
