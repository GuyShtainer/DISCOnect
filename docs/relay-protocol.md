# Relay protocol v1 — encrypted raw-record bundles over a blind store

Status: Bet 10 (2026-10-02); LAN relay added by Bet 12 slice B (2026-10-03). Implements ADR 0005 with
ADR 0004's key model. Code: `src/disconect/relay/{bundle,folder,sync,config}.py` and
`disconect-core/src/relay/{bundle,folder,sync,config,lan,lan_server}.rs`.

## What travels
- `raw_records` rows (every column but `id`; `payload` is the stored zlib bytes, base64 in
  JSON). Origin `transport` and `imported_at` are kept, so provenance survives the hop.
- `export_ranges` claims (stream, from_day, to_day), merged as a union under their UNIQUE key.
- Nothing else. `import_runs`, `import_failures`, derived tables, keys, settings never travel. A
  day one device failed to decode shows as "not covered" on another; undecodable FIT bytes are
  never retained, so they are never relayed either.

## Object
```
object  = version(1 byte = 0x01) ‖ salt(32) ‖ nonce(12) ‖ ChaCha20-Poly1305(k, nonce, padded, AAD)
k       = HKDF-SHA256(ikm = master, salt, info = "hearthbeat/relay/v1/bundle")
AAD     = "hearthbeat/relay/v1/" ‖ account ‖ "/" ‖ object name
padded  = u64be(len(z)) ‖ z ‖ 0x00… to the Padmé size (64 KiB floor)
z       = zlib(lines), lines = header line, then one line per record ({"t":"r",…}) and range ({"t":"x",…})
header  = {"t":"h","format":1,"core":…,"device_id":16 hex,"device_seq":n,"prev":name|null,"created_utc":…,"records":n,"ranges":n}
account = hex(HKDF-SHA256(master, no salt, info = "hearthbeat/relay-account"))   (64 hex chars)
name    = account ‖ "/" ‖ 32 random hex
```
Random bundle ids (never a plaintext hash: that would be a confirmation oracle). A fresh salt per
bundle so phones and a future Rust writer can share the master without coordinating nonces. The
AAD binds the object to its name: a renamed or copied-elsewhere object fails authentication.

## Applying (pull = import)
LIST the account prefix → every name not in `relay_bundles` with status `applied` → GET →
authenticate and decode → verify each record (sha256 of the decompressed bytes = `payload_hash`;
for FIT also = `source_key`; `payload_bytes` matches) → the normal `Writer` as one import run
(transport `relay`): FIT first (clock-offset pre-pass; dedup by the bytes' sha256 alone, so the
same bytes under another stream label from another core version are one record), then JSON
records through the export decoders, then the readiness batch re-decode if any arrived, then the
derived dailies. Ranges union. A bundle that fails any check is recorded `rejected` with a
class-name reason and retried on the next pull (a rejection counts as a failed file, so the
`relay` import run ends `partial`); other bundles proceed. A bundle is marked `applying` when its
records start landing and `applied` only after the derived dailies ran; a pull that finds an
`applying` bundle (a crash) applies it again and re-derives its streams in full. Applying is
idempotent: a re-pull changes nothing. Objects larger than any bundle can be are refused before
they are read; a zlib bomb stops at the 64 MB plaintext cap. A renamed copy of an object is
rejected on every pull (`authentication_failed`), so the operator's stray copy keeps a pull
`partial` until it is removed.

## Conflict rule
JSON streams keyed by a date can carry two different records for one key (a mid-day export and a
later one). Same `(stream, source_key)`, different `payload_hash`: the record whose decoded facts
carry the **later observed time** wins (`end_utc`, else the latest daily fact's time); ties go to
the **larger payload_hash**. A pure function of the two rows under one decoder version, so every
device running the same core converges whatever the arrival order; for streams whose decoder
yields no time (the readiness batch, bare labels) the rule is the hash alone. The loser's bytes go to `raw_superseded`, the decision to `sync_conflicts`.
Local import stays first-wins (BACKLOG). FIT never conflicts (its key is its hash).

The conflict rule converges the **raw set** only. Daily rows converge because every import and
every pull ends by re-deriving the touched JSON streams from the raw records now stored, in the
content order `(start_utc, payload_hash, stream, source_key)` — delete the streams' canonical
rows, rebuild record by record, then the readiness batch (its duplicate collapse also walks the
records by hash). The day a device shows therefore depends on which raw records it holds, never
on the order they arrived or on local ids; a superseded record stops contributing and the
runner-up it hid contributes again. Reparse after an import or a pull changes 0 rows (Bet 10b),
with one known exception: a stored record whose bytes no longer decode keeps the rows it has, and
the relay does not repair it (the hash still matches, so a peer's copy counts as a duplicate);
such a device can differ by that record's rows until the bytes are restored (BACKLOG). An import
re-derives every JSON stream in the store, so an interrupted import is healed by running it again.

## Echo prevention and ordering
`relay_seen` marks every record pushed **or received**; push = records not in it (pulled rows keep
their origin transport, so the marks alone stop echoes). After a rotation every device re-pushes
what it holds, received records included: that is intended (the new account must hold everything).
Per-device chains (`device_seq`, `prev`) travel inside the ciphertext; `sync status` reports a
missing link as a gap.

## Claims and non-claims
The relay cannot read or forge bundles. It can delay or drop them: a dropped middle bundle of a
device's chain is detected; withholding the newest is undetectable; replay is harmless.
Leakage, stated: object count and Padmé-rounded sizes, push times (the store's own mtimes),
total volume, device count only through push cadence. No name, date, stream, device id or hash is
outside the ciphertext.

## Rotation
A new master (`key rotate-recovery`) means a new account and key: relay bookkeeping is cleared
first, under the old key (`sync.forget_relay_state`; nothing stands between the rekey and the
new words), everything is re-pushed under the new account, the old prefix is deleted or warned
about, other devices are re-paired (`key recover` with the new words, then `sync forget`, then
`sync push`). A passphrase change leaves the master,
and therefore the relay, unchanged.

## Adapter
`FolderRelay(root)`: objects are files `<root>/<account>/<32 hex>`; writes go to a temp file in
the same directory and are renamed into place; names outside the pattern are ignored. Any folder
that WebDAV, rsync or Syncthing carries is the self-host story. Networked buckets (S3/GCS) are
a later bet (phones hold the credentials, the user pays); the LAN relay below is Bet 12's answer for
a phone at home. Every adapter implements `put`, `get`, `list(account)` and `delete(name)`; nothing
in a push or a pull deletes (pairing and tests do). `delete` of a missing object is
`FileNotFoundError`, as `get` is.


## LAN relay (Bet 12 slice B): the user's own Mac serves its relay folder
ADR 0005's "self-hosted" adapter with a network face. `disconect-core relay-serve --relay <folder>
[--listen <addr:port>]` (default `127.0.0.1:0`, the bound address is printed as `listening
http://<addr>`; `--listen 0.0.0.0:<port>` is an explicit choice) serves the folder of the one
account its master key derives. A `LanRelay` client (Rust core only; the Python core refuses a
`lan` address: `unsupported_transport`) implements the same four operations over HTTP/1.1. No
server is run by the project: it is the user's machine, on the user's network, started by the user.

**What it never does.** It never opens, decrypts, parses or indexes a bundle (it needs the master
key only to derive the account name and the token key). It never serves another account's objects
(403), never lists anything but the account's object names, never logs anything but the method and
the status (no path, no object name, no peer address), never answers a refusal with a reason, and
is never started by the app on its own. It does not do TLS: the bodies are AEAD output, and the
token proves possession of the master key; an eavesdropper on the Wi-Fi sees the (Padmé-rounded)
object sizes and when they move, which a folder carried by Syncthing shows too.

**Routes** (`/v1`; any other path is 404, a query string is 404, a wrong method 405):
| request | answer |
|---|---|
| `GET /v1/health` (no token) | `200 {"product":"DISCOnect","relay":"lan","v":1}`: no host name, no path |
| `GET /v1/objects` | `200` JSON array of the account's object names, sorted |
| `GET /v1/objects/<account>/<32 hex>` | `200` the object's bytes; `404` when absent |
| `PUT /v1/objects/<account>/<32 hex>` | `204`; the folder relay's own atomic write (temp file, rename); an existing object is replaced, as `FolderRelay.put` does |
| `DELETE /v1/objects/<account>/<32 hex>` | `204`; `404` when absent |

Refusals carry an empty body: `401` (no token, malformed, stale, wrong), `400` (a name that is not
`^[0-9a-f]{64}/[0-9a-f]{32}$`), `403` (a well-formed name under another account), `413` (a body
over one bundle: `MAX_OBJECT` = 64 MiB + 4096, declared or not), `500` (an I/O error).

**Auth.** Every route but health carries
`X-Disconect-Auth: v1.<unix seconds>.<hex HMAC-SHA256(token_key, message)>` with
`message = METHOD ‖ "\n" ‖ path ‖ "\n" ‖ <unix seconds, decimal> ‖ "\n" ‖ hex(sha256(body))` (method upper
case, `path` as sent: `/v1/objects/<account>/<name>`, empty body for GET and DELETE) and
`token_key = HKDF-SHA256(ikm = master, salt = the 64 hex characters of the account as ASCII, info =
"disconect/lan/v1/token")`, 32 bytes. The label is frozen from now on (`docs/kb/24-wire-constants.md`,
which holds the known-answer vectors). The server checks, in this order and with a bare `401` for
every failure: header shape, `|now - timestamp| <= 300 s` (a timestamp from the future is as stale as
one from the past), then reads the body (capped) and compares the tag in constant time. Two
devices that hold the same master (a paired phone and the Mac) therefore derive the same key with
no exchange. Limits, stated: a request captured on the LAN can be replayed for 300 s (a replayed
`PUT` rewrites the same bytes, a replayed `DELETE` removes an object the owner can re-push; a
replayed `GET` shows ciphertext the sniffer already has); the body is read before the tag can be
checked (the tag covers its hash), so an unauthenticated peer can make the server read up to one
bundle per connection: bind to the LAN only on a network you trust. Time-skewed phones fail with
`authentication_failed` until their clock is right.

**Client errors** (`RelayError`): unreachable (connect, name lookup, time-out, broken answer) is
`Unreachable`, retryable; `401` is `Unauthorized`, reason and text `authentication_failed`; `404` on
a read or delete is `FileNotFoundError`; `400` `bad object name`; `413` `too_large`; any other status
is `Status(n)` (only the number is kept). A pull stops at the first of the transient ones (unreachable,
unauthorized, a status) without booking the bundle `rejected`: nothing is marked, the next pull
retries (a bundle whose records had begun to land is `applying`, which the next pull repairs as it
does after a crash). The CLI exits 5 for an unreachable relay. `relay.json` is `{"folder": path}` or
`{"lan": "http://host:port"}` (a non-empty `lan` wins); `--relay http://host:port` selects LAN, and
`https://` or a URL with a path is a usage error.
