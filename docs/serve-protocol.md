# `disconect serve` — the sidecar protocol (Bet 7, v1)

One long-lived process per app. JSON Lines over stdio. Written by the Rust bridge; the Python
side is the only process that ever holds the DB key.

## Transport
- Request: one line `{"id": <int|string>, "method": "<name>", "params": {...}}`.
- Response: `{"id": ..., "result": <any>}` or `{"id": ..., "error": {"code": "<string>", "message": "<redacted text>"}}`.
- Event (no id): `{"event": "progress", "op": "import", "done": <int>, "total": <int|null>, "note": "<redacted>"}`;
  `{"event": "log", "level": "info"|"warn", "message": "..."}`;
  `{"event": "progress", "op": "sync"|"pair", "phase": "push"|"pull", "state": "start"|"done", …counts on "done"}`
  (`op` `pair` has the `push` phase only); and, Rust core only (serve mode, 2026-10-06):
  `{"event": "pair", "state": "bound"|"delivered"|"aborted"|"expired", "reason"?: "another_device"|"codes_differ"|"cancelled"}`
  (`reason` on `aborted` only; each transition exactly once per offer; `bound`: a device answered, the digit input
  may be enabled; `delivered`: the device fetched the payload; never the six digits, a key or a peer address) and
  `{"event": "relay", "state": "stopped", "reason": "address_gone"}` (the address the server is bound to left the
  machine; the server is stopped, `sync.status.serving` is null, and an offer that was open ends with this event
  only — no `pair` event).
- Start-up: the process dup()s fd 1 to a private fd for the protocol and dup2()s fd 1 onto
  fd 2, so any stray print or C-level write goes to stderr. Nothing but protocol lines reach
  the private fd. Exits 0 on stdin EOF. Exit codes are for fatal start-up errors only.
- Never primes a key at start. Until `key.unlock` succeeds, every `data.*` and `import.*`
  call answers `{"error": {"code": "locked"}}`. A plaintext DB (no key file) is simply open.
- Reads run on the main loop. `import.run` runs in one worker thread that holds the write lock
  and streams `progress` events; a second `import.run` while one runs → `{"error":{"code":"busy"}}`;
  the CLI importing at the same time → `busy` too (WriteLockBusy).
- Error codes (strings): `locked`, `wrong_passphrase`, `weak_passphrase`, `not_encrypted`,
  `busy`, `not_found`, `bad_params`, `unknown_method`, `database`, `internal`, plus (Bet 12 slice B)
  `unsupported_transport` (`sync.run` with a `lan` relay on the Python core) and `relay_auth_failed`
  (a LAN relay refused the token; Rust only), plus (Bet 15 slice 2) `invalid_params`, `unknown_tool` and `tool_error`
  (all three only from `tools.call`), plus (7b-2) `pair_failed` (`pair.confirm`, and `pair.offer` after a cancel during its push; Rust core). Messages go
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
| `data.live` | `{"day": "YYYY-MM-DD"?}` (default: local today) | `{"day", "sessions": [{"start_utc","end_utc","start_local","end_local","minutes"}], "metrics": [{"metric","unit","minutes","median"}]}` — the live link on one local day (the Today live card, added 2026-10-06): a session is a run of `json:live` records whose spans overlap or touch (a partial file stored before the full one is one session), kept when its start or end falls on `day` on the watch's clock, `*_local` as `YYYY-MM-DDTHH:MM` on that clock (UTC assumed where no offset is known), `minutes` the distinct folded minutes inside it; `metrics` lists every metric the fold keeps, in contract order, with the minutes whose local day is `day` and their lower median (null when none). A malformed `day` is `bad_params` ("day must be YYYY-MM-DD" / "day must be a non-empty string"); needs the store unlocked |
| `data.facts` | `{"days": int=7, "baseline_days": int=28}` | `insight.period_facts(...)` as dict (comparisons carry scope + confidence) |
| `import.run` | `{"path": str, "transport": "export"|"usb"|"ble"}` | `{"run_id", "files", "ok", "partial", "duplicate", "failed"}` after completion; progress events meanwhile |
| `sync.status` | – | `{"bundles": {"<direction>_<status>": n}, "records_unsent": n, "records_seen": n, "conflicts": n, "superseded": n, "gaps": [{"chain","device_seq","missing"}], "last_pushed_at": "<UTC ISO>"|null, "last_pulled_at": "<UTC ISO>"|null}`: the counts of `sync status` (`conflicts`/`superseded` count distinct versions that lost, by content, so converged devices agree) plus the `noted_at` of the newest **applied** bundle per direction — "last data sent / received", not "last attempt": a run that moved nothing books no bundle (added 2026-10-06 for the Sync screen), read-only (a store older than the relay tables answers what a fresh one would); needs the store unlocked. **(7b-2, both cores; the Python core always answers `null`)** also `"serving"`: `null` when the relay serves nothing, else `{"url": "http://ip:port", "keep": bool, "offer": null\|"open"\|"bound"\|"released"\|"delivered"}` — a state, so a reloaded webview that missed the `bound` event can enable the digit input, and one that missed `delivered` still knows the pairing is done (`delivered`: the other device fetched the key; the server only waits for its first pull) |
| `sync.run` | – (params ignored) | push, then pull, over the relay `relay.json` beside the store names (`{"folder": ...}`, or `{"lan": "http://host:port"}` on the Rust core), as a worker like `import.run`: `{"push": {"bundles","records","ranges"}, "pull": {"applied","rejected","records_new","records_duplicate","records_invalid","conflicts","ranges_new","records_repaired","records_kept","gaps","status": "ok"|"partial"}}`, **counts only** (bundle names are random per push and nothing in a UI needs them; the CLI's JSON lists them). Four `progress` events `{"event":"progress","op":"sync","phase":"push"|"pull","state":"start"|"done", …the phase's counts on "done"}` precede the response. Checked in this order: `locked`; no relay configured (`not_found`, "no relay is configured (relay.json in the data folder)"); a plaintext store (`not_encrypted`: the relay account derives from the master); a `lan` relay on the Python core (`unsupported_transport`); a malformed LAN address (`bad_params`); a second `import.run`/`sync.run` while one runs (`busy`, shared slot); another process holding the write lock (`busy`). Failures during the run: an unreachable LAN relay is `not_found` ("the relay is unreachable (retry later)"), a refused token `relay_auth_failed`, any other HTTP status `internal`; database errors `database`. |
| `relay.addresses` | – | **Rust core only.** `{"addresses": [{"ip": str, "label": str}], "port": n}`: the addresses currently assigned to this Mac (up interfaces, never link-local), the network ones first labelled "this network", loopback last labelled "this Mac only, for testing"; `ip` is the bare address (an IPv6 one is bracketed by the caller in a `listen` or URL). `port` is the running server's, else the last one this session served, else `24816` (`DEFAULT_RELAY_PORT`, kb/24). Under the shared prefix below. |
| `relay.serve` | `{"on": bool, "listen"?: "ip:port"}` | **Rust core only.** The user's switch ("serve the relay on this network"). `{"serving": bool, "url": "http://ip:port"\|null}`. `on: true` needs `listen` on every call, starts the server (or keeps the running one) and sets `keep`; `on: false` is a **stop path**: it checks only `locked` and the parameter shapes (not relay.json, `lan`, plaintext or `.next`), sets `keep` off and stops the server unless an offer still needs it (open, bound, or released and not yet fetched) — then the server follows the offer's lifetime rule (relay-protocol.md, "Serve mode"); with nothing running it answers `{"serving": false, "url": null}` (the Python core always does: it serves nothing). A running listener must always be stoppable, even after `sync forget`, a rewritten relay.json or a CLI key rotation. `on: true` runs the full prefix. `listen`: an IPv4 literal or a bracketed IPv6 literal, an explicit port 1..65535 without a leading zero, not unspecified, not link-local, no zone id, not an IPv4-mapped IPv6 literal such as `[::ffff:192.168.1.20]:24816` (`bad_params`, the text names the rule and never the input; a mapped literal gets the shape text "listen must be an IP address and a port, like 192.168.1.20:24816"); a `listen` that differs from the running server's address → `bad_params` "the relay already serves another address"; a bind failure → `internal` "cannot listen on that address". Nothing about serving is persisted. |
| `pair.offer` | `{"listen": "ip:port"}` | **Rust core only; a worker like `sync.run`, unbounded in the app's `timeout_for`.** Pushes the store to the relay folder (two `progress` events, `op` `pair`, `phase` `push`, counts on `done`; the import slot is held for the push only), starts the server if none runs, attaches a pairing offer and answers `{"offer": "<offer text>", "url": "http://ip:port", "expires_at": <unix s>}`. A second offer while one is open (or being pushed) → `busy` "an offer is open"; an import or sync running → `busy` "an import or a sync is already running"; a `listen` that differs from the running server's address or from the address of a push still in flight → `bad_params` "the relay already serves another address"; the server being stopped → `busy` "the relay is stopping; try again in a moment"; a bind failure → `internal` "cannot listen on that address"; a `pair.cancel` during the push → `pair_failed` "the offer ended" (no server is started). After a delivery whose server still runs, a new offer reuses the same address (a new offer text). Then `pair` events follow (see Transport). The offer text is worth something only while the offer is live and crosses the channel once. |
| `pair.confirm` | `{"digits": str}` | **Rust core only.** `{"state": "released"}`. Exactly six ASCII digits or `bad_params` "digits must be exactly six digits" (a malformed entry does not abort the offer). Wrong digits abort the offer (`aborted` / `codes_differ`) and answer `pair_failed` "the codes differ"; no device answered yet → `pair_failed` "no device has answered"; the offer ended → `pair_failed` "the offer ended"; no offer at all → `not_found` "there is no offer". The core compares the digits itself: the six digits of the Mac side never cross this channel. |
| `pair.cancel` | – | **Rust core only.** `{"state": "aborted"}`; idempotent; ends the open offer (`aborted` / `cancelled`). A **stop path**: it checks only `locked` (no relay.json, `lan`, plaintext or `.next` check), so an offer can always be cancelled. No offer → `not_found` "there is no offer to cancel" — also after the address watcher or `relay.serve` off took the offer (the watcher ends an open offer with no `pair` event; `pair.confirm` then answers `not_found` "there is no offer"); the Python core, which never has an offer, always answers it. A cancel after delivery stops the server after the linger (unless `keep`) with no `pair` event. |
| `tools.call` | `{"name": str, "arguments": object?}` (absent or null `arguments` = `{}`) | `{"name": "<tool>", "result": <the tool's result>}`: exactly what the MCP tool of that name puts in `structuredContent` (scrubbed of the manufacturer's name; key order as the tool builds it), read from this session's store with its master key, so the app's coach loop reaches the six read-only tools (`get_data_health`, `get_metric_series`, `get_sleep_detail`, `list_activities`, `get_period_facts`, `get_contract`) without a third implementation. Arguments are coerced exactly as the MCP coerces them (`"7"`, `7.0` and `true` are accepted for an integer, a JSON-text list for a list; `mcp.json`'s table is the oracle). Needs the store unlocked (`locked` first, before anything else is checked). Failures, in order: `invalid_params` (`name` is not a non-empty string: "name must be a non-empty string"; `arguments` is neither absent, null nor an object: "arguments must be an object"), `unknown_tool` ("Unknown tool: <name>", the MCP's text), `invalid_params` (the arguments do not fit the tool: "arguments rejected: " + the sorted, comma-separated parameter names, never a value or a reason; kb/23), `tool_error` (the tool's own error text, as the MCP puts it after "Error executing tool <name>: ": a `no_metrics`, `value_error` such as "date must be YYYY-MM-DD", `database_error`, `not_configured`, `schema_too_new`, `open_failed` text; or, for a crash, "Error executing tool <name>"). `get_contract` reads no store. |
| `import.last` | – | `{"runs": [...]}` — the newest ≤5 `import_runs` rows of every transport but `ble`, plus the newest `ble` row (a live link's end sweep; listed like any run the sweeps crowded the USB and export runs out), newest first; same list as `data.health.recent_imports` (`id, started_at, finished_at, transport, status, files_seen, files_imported, files_duplicate, files_failed, records_written, error` — error redacted) each with a `failures` count from `import_failures` (0 on a schema without that table) |

**Shared prefix of the five relay/pair methods** (Rust core; the Python core answers `unsupported_transport` "this core runs no LAN server" after the same checks), in this order: (1) `locked` (the only check of the stop paths, `relay.serve` `{"on": false}` and `pair.cancel`, besides the `on`/`listen` shapes of the first); (2) no relay → `not_found` "no relay is configured (relay.json in the data folder)"; (3) a `lan` relay → `bad_params` "this device is a joiner; it serves nothing"; (4) a plaintext store → `not_encrypted` "the relay needs an encrypted store: run '<command> key init' first"; (5) a `<keys>.next` rotation file → `busy` "a key rotation is in progress; finish it first"; (6) the parameter shapes (`on`, `listen`, `digits`). No message ever echoes an address or a folder, and no serve line carries a six-digit code, a joiner key, a tag or a peer address (the allow-listed leaves: `pair.offer.result.offer` / `.url`, `relay.serve.result.url`, `sync.status.result.serving.url`).

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
