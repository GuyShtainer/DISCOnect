# `disconect serve` — the sidecar protocol (Bet 7, v1)

One long-lived process per app. JSON Lines over stdio. Written by the Rust bridge; the Python
side is the only process that ever holds the DB key.

## Transport
- Request: one line `{"id": <int|string>, "method": "<name>", "params": {...}}`.
- Response: `{"id": ..., "result": <any>}` or `{"id": ..., "error": {"code": "<string>", "message": "<redacted text>"}}`.
- Event (no id): `{"event": "progress", "op": "import", "done": <int>, "total": <int|null>, "note": "<redacted>"}`;
  `{"event": "log", "level": "info"|"warn", "message": "..."}`.
- Start-up: the process dup()s fd 1 to a private fd for the protocol and dup2()s fd 1 onto
  fd 2, so any stray print or C-level write goes to stderr. Nothing but protocol lines reach
  the private fd. Exits 0 on stdin EOF. Exit codes are for fatal start-up errors only.
- Never primes a key at start. Until `key.unlock` succeeds, every `data.*` and `import.*`
  call answers `{"error": {"code": "locked"}}`. A plaintext DB (no key file) is simply open.
- Reads run on the main loop. `import.run` runs in one worker thread that holds the write lock
  and streams `progress` events; a second `import.run` while one runs → `{"error":{"code":"busy"}}`;
  the CLI importing at the same time → `busy` too (WriteLockBusy).
- Error codes (strings): `locked`, `wrong_passphrase`, `weak_passphrase`, `not_encrypted`,
  `busy`, `not_found`, `bad_params`, `unknown_method`, `database`, `internal`. Messages go
  through `redact.redact_text`; never a path the user did not pass in this request.
- Env: honours `DISCONECT_DB` / `--db PATH` and `DISCONECT_KEYS`. The env passphrase path
  (`DISCONECT_PASSPHRASE`) is **ignored** by `serve` (the bridge strips it anyway).
- Stdin framing: a request ends at `\n` only (CPython opens stdin with `newline="\n"`, so a `\r` is
  JSON whitespace between tokens and an error anywhere else); invalid UTF-8 becomes U+FFFD; a line
  that is blank by Python's `str.strip()` is skipped. Every line written is ASCII-only JSON.
- **Test-only switches (never set by the app; the sidecar's env allowlist drops them):**
  `DISCONECT_NOW=YYYY-MM-DDTHH:MM:SSZ` pins the one clock both cores read (`storage/_time.now_utc`,
  `time::now_unix`), and `DISCONECT_KEYCHAIN=fail` makes the Rust core's keychain behave like
  `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` (reads find nothing, writes fail with
  `internal`). Both exist so `tools/serve_diff.py` is deterministic and never touches the real keychain.
- Two implementations: `disconect-serve` (Python, the oracle) and `disconect-core` with no subcommand
  (Rust; `app.info.core` is `disconect-core/<version>`). Since 2026-10-03 the Rust core answers every method in this table
  (`tools/serve_diff.py` reports 0 differences and 0 "not yet ported" on six stores; kb/23 lists the
  accepted divergences). One booked difference: Python 3.11+ reads `last_day` in compact (`20261003`) and ISO-week
  (`2026-W40-6`) forms and passes them raw into SQL; the Rust core accepts `YYYY-MM-DD` only and
  answers `bad_params` (the harness names those requests `booked`).

## Methods

*The oracle is `serve.py` + `tests/test_serve.py`; this table was corrected to the code on 2026-10-03 (keychain tri-state, `data.today` null day, `import.last` shape) before the Rust port of the read layer (pitch 11c).*

| method | params | result |
|---|---|---|
| `app.info` | – | `{"product": "DISCOnect", "core": "<version>", "schema": <int>, "contract": <int>, "db": "<path>", "encrypted": bool, "notice": "<non-affiliation text>"}` |
| `key.status` | – | `{"key_file": bool, "encrypted": bool, "unlocked": bool, "keychain": "cached"|"absent"|"stale", "kdf": {...}}` |
| `key.unlock` | `{"passphrase": str}` | `{"unlocked": true}`; errors `wrong_passphrase`, `not_encrypted`. Uses `keys.unlock_with_passphrase` + `storage.remember`; never sets the session-passphrase cache; the param string is dropped right after. Also tries the keychain first when `params.passphrase` is absent. |
| `key.cache` | `{"enable": bool}` | `{"keychain": "cached"|"absent"|"stale"}` (store/remove the master key in the OS keychain; requires unlocked) |
| `data.health` | `{"window_days": int=90}` | `health.data_health(...)` as dict (ledger included), minus core convention strings |
| `data.metric` | `{"metric": str, "scope": str, "days": int=90, "last_day": "YYYY-MM-DD"?}` | `{"metric","scope","unit","days":[{"day":"YYYY-MM-DD","value":<num|null>,"status":"present|failed|source_empty|not_covered"}]}` — **calendar-filled**: every local day from first to last; missing = `null` + status from `coverage.day_statuses` |
| `data.today` | – | `{"day": "<local today>", "metrics": [{"metric","scope","value","unit","day","status"}]}` — the latest value per (metric, scope) in the contract; when none is stored, `value` **and `day`** are null and `status` comes from `coverage.statuses_on(today)` (`failed`, `source_empty`, `not_covered`) |
| `data.facts` | `{"days": int=7, "baseline_days": int=28}` | `insight.period_facts(...)` as dict (comparisons carry scope + confidence) |
| `import.run` | `{"path": str, "transport": "export"|"usb"}` | `{"run_id", "files", "ok", "partial", "duplicate", "failed"}` after completion; progress events meanwhile |
| `import.last` | – | `{"runs": [...]}` — the newest ≤5 `import_runs` rows (`id, started_at, finished_at, transport, status, files_seen, files_imported, files_duplicate, files_failed, records_written, error` — error redacted) each with a `failures` count from `import_failures` (0 on a schema without that table) |

## Core helpers added for serve (also usable by CLI/MCP)
- `disconect.identity`: `PRODUCT = "DISCOnect"`, `NOTICE = "Not affiliated with or endorsed by any watch manufacturer."` (the only place these strings live; MCP/CLI may import them later).
- `queries.local_today(conn)` — one definition of "today" (health's max(UTC, local) rule).
- `coverage.day_statuses(conn, metric, scope, first_day, last_day) -> dict[day, status]`.
- `queries.metric_calendar(conn, metric, scope, first_day, last_day)` — calendar-filled series.
- `keys.unlock_with_passphrase(key_path, passphrase) -> master` without touching `_session_passphrase` (exists; `serve` must not go through `keys.unlock`).
- `sources.import_path(..., progress=callable|None)`.
- `cli` key init/status logic extracted to `keys.status_for(db_path)` so TTY code stays in cli.

## Tests (all in `tests/test_serve.py` unless noted)
- Registry-complete privacy walk: every method in `serve.METHODS` is exercised (guard like
  `test_privacy.py`), every result/error/progress line walked for `FORBIDDEN_KEYS`,
  `FORBIDDEN_TEXT` and the manufacturer name.
- Locked-before-unlock, wrong passphrase, unlock, then data; `busy` on concurrent import.
- Stdout isolation: a handler that `print()`s must not corrupt the stream.
- EOF exit: closing stdin ends the process within 2 s.
- Never-printed: subprocess run of `key.unlock` + `key.cache` with a scratch passphrase; stdout
  and stderr scanned for the passphrase raw/hex/base64 and for the master key.
- No-socket fixture: `socket.socket.connect` and `socket.getaddrinfo` raise during every
  method; also under `disconect-mcp` (test_mcp_stdio).
- `test_lint.py`: no `print(` outside cli.py; `DISCONECT_PASSPHRASE` not read in serve.py.
