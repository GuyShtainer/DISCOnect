# DISCOnect

A local-first, cloud-free store for the health data a Garmin watch records. It keeps the raw
files the watch writes, decodes them into your own SQLite database with full provenance, compares
the last days with your own baseline per metric, and lets an AI assistant query that history over
MCP — with no vendor cloud in the path. This repository is the Python core: a CLI, a read-only MCP
server, a JSON-over-stdio service for a desktop shell, an encrypted store, and an end-to-end
encrypted sync between your own devices through a relay that only ever sees ciphertext.

*Not affiliated with, endorsed by, or supported by Garmin Ltd. or any other company named here;
see [TRADEMARKS.md](TRADEMARKS.md).* What the software's own words may and may not claim about
your data is set out in [CLAIMS-POLICY.md](CLAIMS-POLICY.md), and linted.

**Status: v0, in progress.** The ingest, store, replay, facts, CLI and MCP paths were validated
on one real account export in September 2026; the newer parts (encryption, sync, pairing, the
serve protocol) are validated on synthetic data only. Details and dates in [STATUS.md](STATUS.md).

## Install and first run

Python 3.12 or newer.

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp,dev]'
.venv/bin/disconect import ~/Downloads/<your-connect-export>     # a zip, the extracted folder, or a watch's GARMIN/ tree
.venv/bin/disconect status --days 90                             # what the store holds, what failed, how the sources agree
```

The database defaults to `~/.disconect/disconect.db` (override with `--db` or `DISCONECT_DB`).
Re-importing the same files is a no-op: raw bytes are content-addressed, so the same FIT file
from a USB copy and from an export is stored once. Exit codes are listed in `disconect --help`.

More of the CLI:

```sh
.venv/bin/disconect facts sleep_score resting_heart_rate   # last 7 days vs your own 28-day baseline
.venv/bin/disconect export > daily.csv                     # one column per metric and source scope
.venv/bin/disconect chart metric steps --days 90 -o steps.png   # PNG drawn with no dependencies
.venv/bin/disconect chart sleep -o last-night.png          # hypnogram, one row per source scope
.venv/bin/disconect chart samples heart_rate -o hr.png     # a whole day of samples, night shaded
.venv/bin/disconect backup && .venv/bin/disconect backups  # verified snapshots with a manifest
.venv/bin/disconect reparse                                # replay the decoder over the retained bytes
.venv/bin/disconect contract                               # the read contract every outlet quotes
```

## Tests

```sh
.venv/bin/python -m pytest
```

Every test runs on synthetic data: `tests/fit_builder.py` synthesises FIT files, and the committed
fixture stores under `tests/fixtures/` were built by the generators beside them. No file from a
real watch or account is in this repository. The suite reads `748 passed, 30 skipped` on a fresh
clone (2026-10-08): the skipped tests need the Rust twin, the desktop/phone shell or the two-core
differential harness, which are developed beside this package and are not published here
(`tests/monorepo.py` names them), or a Node toolchain.

## What it does

- **Ingest** wellness FIT files (all-day monitoring, sleep, HRV, skin temperature, training
  metrics, activities) with `fitdecode` (MIT), plus the JSON layer of a Garmin Connect *Export
  Your Data* archive: daily spine, sleep, the vendor's readiness figure, VO2max, training load,
  endurance score, fitness age, weight, hydration, active minutes. Live-link session files
  (`live-*.jsonl`, written by a separate Bluetooth client that is not part of this repository)
  are kept as raw records and folded into per-minute `live`-scope samples.
- **Store**: SQLite in WAL mode, append-only migrations, raw bytes retained with every canonical
  row pointing back to its raw record, an OS-held cross-process write lock that readers never
  take, per-stream parse/write provenance, error text redacted before it is stored. Data from
  the device, the vendor cloud, local derivations and the live link are kept in separate source
  scopes and never merged.
- **Replay**: `disconect reparse` re-decodes the retained bytes with today's decoder. It dry-runs
  first and refuses to touch rows while any record fails; a replay with an unchanged decoder
  reproduces the store exactly.
- **Contract**: units, time, missing-value and source semantics defined once and quoted by every
  outlet (`disconect contract`).
- **Facts**: the last N days versus the person's own baseline, per metric and source scope, with
  delta, z-score, a confidence band that says "insufficient" below three baseline days, and
  evidence dates. No population norms: it describes your own history only.
- **Data health**: coverage per day and metric, what failed, and cross-source agreement (the
  device-decoded figure next to the cloud's own figure for the same day, never silently merged).
- **MCP server** (`disconect-mcp`, stdio, read-only): `get_data_health`, `get_metric_series`,
  `get_sleep_detail`, `list_activities`, `get_period_facts`, `get_contract`. A test walks every
  tool's output for identifiers (serials, paths, emails) so the allowlist is enforced in code.
- **Serve protocol** (`disconect serve`, JSON lines over stdio): the methods a desktop or phone
  shell calls — data reads, import with progress and cancel, key management, pairing, sync —
  specified in [docs/serve-protocol.md](docs/serve-protocol.md).
- **Encryption at rest** (optional): `disconect key init` converts the database and its backups
  to SQLCipher; the master key is wrapped under an Argon2id passphrase and is also the 24
  recovery words. See [docs/adr/0004-encryption-and-keys.md](docs/adr/0004-encryption-and-keys.md).
- **Sync between your devices** (optional, needs a key): `disconect sync push` writes encrypted
  raw-record bundles into a relay folder you carry (for example a folder you already sync);
  `disconect sync pull` applies them on another device. The relay sees only ciphertext, and the
  daily rows are a pure function of the converged raw set, whichever order the bundles arrive.
  Device pairing exchanges the master key over an ephemeral X25519 channel with a short
  authentication string. See [docs/relay-protocol.md](docs/relay-protocol.md) and
  [docs/adr/0011-pairing-protocol.md](docs/adr/0011-pairing-protocol.md).

### An MCP client

Claude Desktop or any MCP client over stdio:

```json
{ "mcpServers": { "disconect": {
    "command": "/absolute/path/to/disconect/.venv/bin/disconect-mcp",
    "env": { "DISCONECT_DB": "/home/you/.disconect/disconect.db" } } } }
```

The server opens the file read-only, opens no port, and returns no serial numbers, paths,
account ids or coordinates. Importing is the CLI's job.

### Encryption at rest

```sh
.venv/bin/disconect key init        # in your terminal: choose a passphrase (>=12 chars), write down 24 words
.venv/bin/disconect key cache       # once: keep the master key in the login keychain for daily use
.venv/bin/disconect key status      # which unlock paths exist (never key material)
.venv/bin/disconect encrypt --purge-plaintext   # later: delete the plaintext rollback copies
```

`key init` converts the database and every snapshot in `backups/` to SQLCipher (AES-256 per
page) and writes `<db>.keys.json` beside it: the master key wrapped under your passphrase
(Argon2id, 256 MiB). **The 24 words are the master key itself**: recovery needs no file, and
losing both the passphrase and the words means the data is gone. Once a key file exists, a
plaintext database in its place is refused, so a restore or a stray copy can never downgrade you
silently. Unlock paths, in order: the OS login keychain (`key cache`; any program running as you
can read it, undo with `key cache --remove`) or a passphrase prompt in a terminal.
`DISCONECT_PASSPHRASE` exists for tests and CI only; never put a passphrase in an MCP client's
configuration. What encryption does not cover: filesystem snapshots and freed blocks keep
plaintext copies made before `encrypt` (full-disk encryption is the protection for those), backup
manifests stay plaintext metadata, and Python cannot wipe key material from memory.

### Sync

```sh
.venv/bin/disconect sync push --relay ~/some-synced-folder/disconect-relay --remember
.venv/bin/disconect sync pull            # on another device holding the same key
.venv/bin/disconect sync status          # counts only
```

The Python CLI reads relay *folders*; serving a relay over the local network is a job of the
Rust core, which is not in this repository, and the CLI answers a LAN address with a usage
error. `sync.status` and `sync.run` are also methods of `disconect serve`.

## Layout

```
src/disconect/
  contract.py          the read contract (conventions + metric table)
  storage/             open (writer under flock, readers query_only), migrations, backup, keys, home
  ingest/
    fit_wellness.py    FIT -> facts (sentinels dropped, ts16 expanded, stage-end semantics)
    connect_export.py  Connect export walker + JSON decoders + the decoder table reparse shares
    writer.py          raw retention, canonical upserts, provenance, replay, daily derivations
    clock.py           watch UTC offsets -> local calendar dates
    live.py            live-link session files and the per-minute fold
    sources.py         path detection, two-pass import, order-independent reparse
  insight.py           facts vs own baseline (rules pinned by tests)
  health.py, coverage.py   data health, coverage ledger, cross-source agreement
  queries.py, export.py, render.py, redact.py
  relay/               encrypted bundles, the folder relay, push/pull, the relay list
  pair.py              device pairing (X25519, commit-reveal, short authentication string)
  serve.py             the JSON-over-stdio service for a shell
  cli.py, mcp_server.py
tests/                 pytest; synthetic data only; monorepo.py names what is developed elsewhere
docs/                  architecture, serve and relay protocols, the ADRs the code implements
```

Design lineage: the contract module, raw-payload retention, `COALESCE(device_id,'')` unique
keys, per-stream provenance, the OS-held write lock, verified snapshots and the
`get_data_health` tool are adapted from ZeppBridge (MIT), the Amazfit sibling project.

## Energy reserve: read from the watch, checked against the cloud

The watch writes its body-energy gauge once a minute into the monitoring files (`stress_level`,
field 3, not named in public FIT profiles). The importer stores it as `energy_reserve` samples
(`device`) and derives `energy_reserve_high`, `energy_reserve_low`, `energy_reserve_charged` and
`energy_reserve_drained` per local day (`local`, in time order). `disconect status` and
`get_data_health` compare each of those against the vendor cloud's own daily figure under
"source agreement", so a disagreement is shown, never hidden. Provenance: black-box correlation
of the maintainer's own watch files with his own account export on one watch; see the handler
docstring in `ingest/fit_wellness.py`. Nothing here is an estimate: when the watch files stop,
these rows stop.

## How it was built

Designed and reviewed by Guy Shtainer; implemented AI-assisted with Claude Code. The Python
core is the oracle a Rust port is held to by differential tests; that port and the desktop and
phone shells are developed beside this package and are not published yet.

## License

AGPL-3.0-or-later ([LICENSE](LICENSE)). The software never depends on Garmin's FIT SDK: FIT files
are parsed with `fitdecode` (MIT). Third-party marks and the non-affiliation statement:
[TRADEMARKS.md](TRADEMARKS.md).
