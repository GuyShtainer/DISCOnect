# DISCOnect

The local-first, cloud-free health platform for Garmin watches: keep the raw files the watch
writes, decode them into your own SQLite store, and let an AI assistant query the history over
MCP, with no vendor cloud in the path.

*Hearth + heartbeat: your body's data, kept at your own hearth.* Not affiliated with, endorsed by,
or supported by Garmin Ltd.

## Status (2026-09-10): v0 core, validated against a real account

Verified on one real Garmin Connect account export (426 FIT files, three weeks of wear, zero
decode failures; import takes about five seconds):

- **Ingest** of wellness FIT files (all-day monitoring, sleep, HRV, skin temperature, training
  metrics, activities) with `fitdecode` (MIT), plus the JSON layer of a Garmin Connect
  *Export Your Data* archive: daily spine, sleep, training readiness, VO2max, training load,
  endurance score, fitness age, weight, hydration, active minutes.
- **Store**: SQLite in WAL mode, append-only migrations, raw bytes retained with every canonical
  row pointing back to its raw record, an OS-held cross-process write lock that readers never
  take, per-stream parse/write provenance, error text redacted before it is stored.
- **Replay**: `disconect reparse` re-decodes the retained bytes with today's decoder. It dry-runs
  first and refuses to touch rows while any record fails; a replay with an unchanged decoder
  reproduces the store exactly (checked on the real export, all nine tables).
- **Contract**: units, time, missing-value and source semantics defined once and quoted by every
  outlet (`disconect contract`).
- **Facts engine**: the last N days versus the person's own baseline, per metric and source
  scope, with delta, z-score, a confidence band that says "insufficient" below three baseline
  days, and evidence dates. No population norms, no diagnosis.
- **CLI**: `import`, `reparse`, `status` (data health incl. cross-source agreement), `facts`,
  `export` (CSV), `backup` / `backups` / `restore` (verified snapshots), `contract`.
- **MCP server** (`disconect-mcp`, stdio, read-only): `get_data_health`, `get_metric_series`,
  `get_sleep_detail`, `list_activities`, `get_period_facts`, `get_contract`. A test walks every
  tool's output for identifiers (serials, paths, emails) so the allowlist is enforced in code.

Cross-checked against Garmin Connect's own figures for the same days (device-decoded vs the
cloud's JSON, stored side by side as `device` vs `vendor_cloud`, reported by `disconect status`):
resting heart rate, sleep score, sleep window, HRV weekly average, VO2max and the day's minimum
and maximum heart rate match exactly on every day; the daily stress average matches within rounding; sleep stage totals match exactly for deep and awake and within 30 seconds for light
and REM; daily steps match on 20 of 21 days (the other differs by under a hundred, most likely a
source Connect merged in); distance within a metre.

Not done yet: the USB/MTP pull off the watch (Phase 0 was deliberately skipped on 2026-09-10; the
same decoder ingests a `GARMIN/` tree once `tools/mtp-pull.sh` exists), body battery and
training readiness from device data (cloud JSON only so far; both are recompute targets), naps,
per-second activity records and routes (not stored, by design for now), a web UI.

## Run it

```sh
cd projects/disconect
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp,dev]'
.venv/bin/disconect import ~/Downloads/<your-export>        # zip, extracted folder, or DI_CONNECT
.venv/bin/disconect status --days 90                         # what the store holds, what failed
.venv/bin/disconect facts sleep_score resting_heart_rate     # last 7 days vs your 28-day baseline
.venv/bin/disconect export > daily.csv                       # one column per metric and scope
.venv/bin/disconect chart metric steps --days 90 -o steps.png   # a PNG, drawn with no dependencies
.venv/bin/disconect chart sleep -o last-night.png            # hypnogram, one row per source scope
.venv/bin/disconect chart samples heart_rate -o hr.png       # a whole day of samples, night shaded
.venv/bin/disconect backup && .venv/bin/disconect backups   # verified snapshot with manifest
.venv/bin/disconect reparse                                  # replay decoding after a decoder fix
.venv/bin/disconect import /path/to/GARMIN --transport usb   # a watch tree, whenever it exists
.venv/bin/python -m pytest                                     # tests, synthetic data only
```

The database defaults to `~/.disconect/disconect.db`; override with `--db` or `DISCONECT_DB`. A folder
from an earlier build (`~/.hearthbeat/hearthbeat.db`) is read as it is, with one hint on stderr, until
you quit the app and Claude Desktop and run `disconect migrate-home`, which moves it.
Re-importing the same files is a no-op (raw bytes are content-addressed), so importing the same
FIT file from a USB pull and from an export never duplicates anything. Exit codes are published in
`disconect --help`.

Claude Desktop / any MCP client, stdio:

```json
{ "mcpServers": { "disconect": {
    "command": "/absolute/path/projects/disconect/.venv/bin/disconect-mcp",
    "env": { "DISCONECT_DB": "/Users/you/.disconect/disconect.db" } } } }
```

The server opens the file read-only, opens no port, and returns no serial numbers, paths,
account ids or coordinates. Import is the CLI's job.

### Encryption at rest (optional, recommended)

```sh
.venv/bin/disconect key init        # in YOUR terminal: choose a passphrase (>=12 chars), write down 24 words
.venv/bin/disconect key cache       # once: keep the master key in the login keychain for daily use
.venv/bin/disconect key status      # which unlock paths exist (never key material)
.venv/bin/disconect encrypt --purge-plaintext   # after a few days: delete the plaintext rollback copies
```

`key init` converts the database and every snapshot in `backups/` to SQLCipher (AES-256 per page)
and writes `<db>.keys.json` beside it: the master key wrapped under your passphrase (Argon2id,
256 MiB). **The 24 words are the master key itself** — recovery needs no file; losing both the
passphrase and the words means the data is gone. Backups stay encrypted (the key file is copied
beside each snapshot). Once a key file exists, a plaintext database in its place is refused, so a
restore or a stray copy can never downgrade you silently.

Unlock paths, in order: the macOS login keychain (`key cache`; any program running as you can read
it — undo with `key cache --remove`), or a passphrase prompt in a terminal. `DISCONECT_PASSPHRASE`
exists for tests and CI only — **never put a passphrase in `claude_desktop_config.json`** or a
launchd plist; the MCP server unlocks from the keychain at startup and never prompts. Other
commands: `key change-passphrase` (same database key), `key recover` (words → new passphrase),
`key rotate-recovery` (new master key: re-encrypts, revokes every earlier copy of the key file).

What encryption does not cover: Time Machine / APFS snapshots and freed SSD blocks keep plaintext
copies made before `encrypt`; FileVault is the protection for those. Backup manifests stay plaintext
metadata (row counts, sizes, your note). Python cannot wipe key material from memory.

## Layout

```
src/disconect/
  contract.py          the read contract (conventions + metric table)
  storage/             open (writer under flock, readers query_only), migrations, backup, time
  ingest/
    fit_wellness.py    FIT -> facts (sentinels dropped, ts16 expanded, stage-end semantics)
    connect_export.py  Connect export walker + JSON decoders + the decoder table reparse shares
    writer.py          raw retention, canonical upserts, provenance, replay, daily-step derivation
    clock.py           watch UTC offsets -> local calendar dates
    sources.py         path detection, two-pass import, order-independent reparse
  insight.py           facts vs own baseline (rules pinned by tests)
  health.py            data health + cross-source agreement
  queries.py, export.py, redact.py
  cli.py, mcp_server.py
tests/                 pytest; fit_builder.py synthesises FIT files via fitdecode's profile
```

Design lineage: the contract module, raw-payload retention, `COALESCE(device_id,'')` unique keys,
per-stream provenance, the OS-held write lock, verified snapshots and the `get_data_health` tool
are adapted from ZeppBridge (MIT), the Amazfit sibling project; see `docs/kb/14-prior-art.md`.

## Energy reserve: read from the watch, checked against the cloud

The watch writes its body-energy gauge once a minute into the monitoring files (`stress_level`,
field 3, not named in public FIT profiles). The importer stores it as `energy_reserve` samples
(`device`) and derives `energy_reserve_high`, `energy_reserve_low`, `energy_reserve_charged` and
`energy_reserve_drained` per local day (`local`, in time order). `disconect status` and
`get_data_health` compare each of those against the vendor cloud's own daily figure under
"source agreement" (`energy_reserve_high vs body_battery_high`, …), so a disagreement is shown,
never hidden. Provenance: black-box correlation of the user's own files with his own export on
one fenix 8; see the handler docstring in `ingest/fit_wellness.py` and `docs/pitches/08-recompute.md`.
Nothing here is an estimate: when the watch files stop, these rows stop.

## Licensing (at publish)

AGPL-3.0-or-later for this server; Apache-2.0 for the future watch app and schemas. Never depends
on Garmin's FIT SDK. Names here avoid Garmin marks; a legal review and a release audit run before
any first push. Third-party marks and the non-affiliation statement: `TRADEMARKS.md` at the
repository root.
