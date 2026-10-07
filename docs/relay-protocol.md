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
yields no time (the readiness batch, bare labels) the rule is the hash alone. The loser's bytes go to `raw_superseded`, the decision to `sync_conflicts` — per-bundle journals; the reported counts are by content (distinct versions that lost), the only form that converges across devices.
Local import stays first-wins (BACKLOG). FIT never conflicts (its key is its hash).
**Damaged local copies:** before applying anything, a pull verifies every stored record the relay has
carried (`relay_seen`) against its hash and refetches a damaged one from the bundle that carried it
(`records_repaired`); the relay is the copy of last resort, since a pulled record is never pushed back.
**A stream this build cannot decode:** a `json` record whose stream has neither a batch decoder nor a
per-record decoder on the pulling core (a peer on a newer build published it) is **kept**, not dropped:
its bytes become a `raw_records` row with the sender's scope, device and span, no canonical rows, an
`import_failures` row (`unrecognized_payload`, the same trail `reparse` leaves for such bytes) and a
`relay_seen` mark; the pull reports it under `records_kept`. `reparse` leaves such records waiting (a
warning names the streams; they neither fail the replay nor lose their ledger row) until a build with
the decoder replays them. Two versions of one key that neither decodes conflict by the **hash alone**
(the batch-stream rule): the one rule a device without the decoder can apply. Known limitation: a
device that *can* decode both uses the observed-time rule, so a fleet of mixed builds may keep
different winners for that key until the lagging build upgrades and a conflict is re-decided — booked
in kb/22 with the other accepted classes.

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
re-derives every JSON stream in the store, so an interrupted import is healed by running it again (its run row is marked `interrupted` on the next write open).

## Echo prevention and ordering
`relay_seen` marks every record pushed **or received**; push = records not in it (pulled rows keep
their origin transport, so the marks alone stop echoes). After a rotation every device re-pushes
what it holds, received records included: that is intended (the new account must hold everything).
Per-device chains (`device_seq`, `prev`) travel inside the ciphertext; `sync status` reports a
missing link as a gap, and lists the chains it holds (19b: per writer id among the applied bundles, the
bundle and record counts, the highest `device_seq` and whether the id is this store's own). Cost: `sync forget`
(and a re-pair) wipes `relay_device`, so the next push mints a new id and the other devices see a second chain
from the same device; the old chain's row stays in their lists until their own `sync forget`.

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
is started by the app only as the user's switch (see "Serve mode" below), never on its own. It does not do TLS: the bodies are AEAD output, and the
token proves possession of the master key; an eavesdropper on the Wi-Fi sees the (Padmé-rounded)
object sizes and when they move, which a folder carried by Syncthing shows too.

**Serve mode (7b-2, Rust core).** The desktop app's sidecar runs at most one server per session, on an
address the user picks from a fresh list (`relay.addresses`) and passes as `listen` on every call; nothing
about serving is remembered, so the switch is "while the app runs". `relay.serve {"on": true}` starts it and
keeps it until `{"on": false}` or the session ends. A pairing offer (`pair.offer`) starts it if it is not
running. With the switch off the **lifetime rule** is: the server lives until the offer's `exp` (15 minutes)
or the user's "stop serving" once the offer has been delivered, because the joiner's first pull comes from
the same URL right after the payload's `200` and the payload is served on every GET until `exp`; it stops at
once only on abort, cancel or expiry, after a ~2 s linger so the joiner reads `410` rather than a refused
connection. While serving, the core checks every ~10 s that the bound address is still assigned to this
machine; when it is gone the server stops and a `relay` event says so (`reason` `address_gone`; the stop after an
offer ends, once the linger or the expiry is over, sends the same event with `reason` `offer_ended`) (a laptop that changed networks never
serves a foreign network's address). The session's end (the app quitting) aborts the offer and stops the
server before anything else is awaited. The server's logger is a no-op in this mode. The relay folder is
still set by the CLI (`sync … --relay <folder> --remember`); the app does not choose one.

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
over one bundle: `MAX_OBJECT` = 64 MiB + 4096, declared), `408` (a head or body that stopped arriving), `500` (an I/O error).

**Auth.** Every route but health carries
`X-Disconect-Auth: v1.<unix seconds>.<pre>.<tag>`, two hex HMAC-SHA256 values under one key:
`pre = HMAC(token_key, "pre" ‖ "\n" ‖ METHOD ‖ "\n" ‖ path ‖ "\n" ‖ <unix seconds, decimal> ‖ "\n" ‖ <Content-Length, decimal>)`
(`0` when the request declares no length, as GET and DELETE do) and
`tag = HMAC(token_key, METHOD ‖ "\n" ‖ path ‖ "\n" ‖ <unix seconds, decimal> ‖ "\n" ‖ hex(sha256(body)))` (method upper
case, `path` as sent: `/v1/objects/<account>/<name>`, empty body for GET and DELETE), with
`token_key = HKDF-SHA256(ikm = master, salt = the 64 hex characters of the account as ASCII, info =
"disconect/lan/v1/token")`, 32 bytes. The label is frozen from now on (`docs/kb/24-wire-constants.md`,
which holds the known-answer vectors, pre-tag ones included). The server checks, in this order and with a
bare `401` for every failure: header shape, `|now - timestamp| <= 300 s` (a timestamp from the future is as
stale as one from the past), **the pre-tag against the declared `Content-Length`, before it reads a single body
byte**, then the body (exactly the declared length, at most one bundle), then the tag in constant time. A request
without a `Content-Length` has an empty body, which is never read; chunked uploads are refused (`400`). Two
devices that hold the same master (a paired phone and the Mac) therefore derive the same key with no exchange.

**Response tag (12-RA).** Every answer from `handle()` except `/v1/health` and `/v1/pair*`, whatever its status (200, 204 and every refusal), carries
`X-Disconect-Resp: v1.<server unix seconds>.<rtag>` with
`rtag = HMAC(token_key, "resp" ‖ "\n" ‖ METHOD ‖ "\n" ‖ target ‖ "\n" ‖ <the request's X-Disconect-Auth value as the server
received it, SP/HTAB-trimmed, empty when there was none> ‖ "\n" ‖ <server unix seconds, decimal> ‖ "\n" ‖ <status, decimal> ‖ "\n" ‖
hex(sha256(response body)))`, hex-encoded (`target` as received, query included). The tag is computed in `handle()` over
the final status and body, so a `413`, `408` or `500` from the handler is tagged too. `/v1/health` and the pairing routes
carry none, and neither do the two errors raised before a request head exists (`400` for a head that cannot be read,
`408` for a head that stops arriving): the client sees those as unverified. Same key in both directions is fine: a request tag
starts with an upper-case method, the pre-tag with `pre`, the response with `resp`.

**Client rule.** The client keeps the auth value it sent and, before it interprets the status, requires exactly one
`X-Disconect-Resp` of the exact shape (`v1.` + decimal seconds + `.` + 64 lower-case hex), reads the body (capped at one
bundle) and verifies the tag in constant time over its own method, target, sent header, the header's seconds, the status and
the body. Missing, repeated, malformed or wrong is `Unverified`: not the relay this device paired with, the relay was set up
again with a new key (pair again from it), the network altered the answer, or the connection broke; the client takes no
automatic action (it never re-pairs, never books, never lists). A verified `401` whose server seconds differ from the
client's sent timestamp by more than 300 s is `Unauthorized` (the relay holds this device's key but refused the request: the
clocks disagree; the text carries the offset in minutes, "check the date and time on both devices"). A verified `401` within
300 s cannot be a clock problem (the request a middlebox altered, replayed as a re-pair lure) and is `Unverified`. Server and client ship together in one core; a client of this version against a server without the tag sees
`Unverified` on every call (the Mac updates first).

**Limits, stated.**
- *Replay.* A request captured on the LAN can be replayed for 300 s: a replayed `PUT` rewrites the same bytes, a
  replayed `GET` shows ciphertext the sniffer already has, a replayed `DELETE` removes the object again. Nothing
  re-pushes a bundle that was pushed, and nothing deletes anything today (the trait has `delete` for the pairing and
  pruning slices to come), so a replayed `DELETE` has no victim yet. The pairing offer (12-E) is single-use
  but needs no seen-tag cache (12-B review D1): its routes are not token-authenticated at all, a replay of the
  joiner's POST carries the same public key and gets the identical `202` (idempotent), a different key aborts the
  offer, and nothing is read before the declared length is checked ("Pairing routes" below).
- *Slow peers (Bet 12 review F1).* The first version read the body before it could check the tag, so four
  connections with a well-formed forged header, `Content-Length: 1000000` and no body held every worker and
  `/v1/health` timed out. Now the pre-tag refuses a peer without the key on the head alone (`401` at once,
  nothing read), every socket read and write has a time-out (`idle` = 10 s; the head must arrive within it in total,
  the body within `idle + length / 256 KiB/s`, so a 64 MiB bundle gets about 4.5 minutes and a one-byte-per-nine-seconds
  trickle gets nothing; the answer is `408`), and each connection is a thread, at most 32 (one more is closed
  unanswered), so a stalled peer holds only its own. What remains: an unauthenticated peer can occupy connection slots
  with half-sent heads for up to 10 s each (no per-address limit), and a peer holding a captured header for the exact
  length can hold a slot, and make the server buffer what it sends (one bundle at most), until the body budget ends.
  Bind to the LAN only on a network you trust. The HTTP layer is the server's own small subset (one request per
  connection, `Connection: close`; the library first tried, `tiny_http`, has no time-outs), see `lan_server.rs`.
- *A fake or tampering relay (12-RA).* A peer without the key (a fake server on a re-used address, a man in the middle)
  can still refuse the connection, hold it open, or answer nothing; it cannot make the client book a push, trust a
  list or re-pair, because every answer it could forge fails the tag. The body of a pulled bundle was already AEAD; the
  status and the list were the gap. Left open on purpose: an unverified `PUT` may have landed, so the retry pushes the
  same records as a new bundle (a fresh name each time, never reused) and pulls de-duplicate them (`mark_existing`, no false gap);
  a peer that strips tags can only make the relay grow by duplicates. Two requests with identical headers in the same
  second (a repeated `GET` of one path) have identical tags, so a peer may swap their answers: harmless, nothing is booked
  from a list and names are random. A tag proves a holder of the account key, not this particular relay (Bet 14 pins a
  relay id at pairing and binds it into the tag). Nothing may act on health beyond "something answers".
- Time-skewed phones get a tagged `401` (`relay_auth_failed`, "check the date and time on both devices") until their clock is right.
- *An unverified `PUT` that landed (12-RA review).* The retry reuses `(device_id, seq, prev)`, so the relay then holds two
  sibling bundles with the same `seq`. `gaps()` ignores it today; a future fork or tamper check (Bet 14, pruning) must allow
  it. The pusher later pulls its own orphan back as duplicates.

**Client errors** (`RelayError`): unreachable (connect, name lookup, time-out, broken answer) is
`Unreachable`, retryable; a verified `401` outside the skew window is `Unauthorized { skew_s }` (text `relay_auth_failed: ... this device's clock is N
minutes off the relay's; check the date and time on both devices`, reason `relay_auth_failed`); an answer with no valid response tag is `Unverified` (reason and text
`relay_unverified`); `404` on a read or delete is `FileNotFoundError`; `400` `bad object name`; `413` `too_large`; any other
status is `Status(n)` (only the number is kept). A pull stops at the first of the transient ones (unreachable,
unauthorized, unverified, a status) without booking the bundle `rejected`: nothing is marked, the next pull
retries (a bundle whose records had begun to land is `applying`, which the next pull repairs as it
does after a crash). The CLI exits 5 for an unreachable, unverified or clock-refused relay. `relay.json` is `{"folder": path}` or
`{"lan": "http://host:port"}` (a non-empty `lan` wins); `--relay http://host:port` selects LAN, and
`https://` or a URL with a path is a usage error.

## Relay list (Bet 19d, 2026-10-07): several relays, one name per bundle
`relay.json` is a list: `{"relays": [{"id": "<8 hex>", "kind": "folder"|"lan", "path"|"url": "...",
"label"?: "...", "serve"?: true}]}`. The legacy `{"folder": path}` reads as one entry `default` **with
`serve: true`**; the legacy `{"lan": url}` as one entry `default` (a non-empty `lan` still wins over
`folder` in that form). `id` is `default` or 1–32 hex chars; a malformed entry, a duplicate id or two
`serve` entries make the file read as nothing (as `{"folder": 5}` does); an empty list (`{"relays": []}`) reads as "no relay"
everywhere (`sync relay add` appends to it; `sync relay remove` of the last entry deletes the file). Only the CLI
rewrites it (`sync relay add|remove`, `--remember`), always in the list form with sorted keys (`id, kind, label,
path|url, serve`; `label` only when non-empty, `serve` only when true); a folder is stored as typed. At most one entry serves:
`relay.serve`, `pair.offer` and the offerer's push use that folder; no `serve` entry = a joiner.
`relay_url` is the first `lan` entry's base address.

**Push.** A new bundle is packed once and the same bytes are `put` to every site in list order under one
name; it is booked when at least one site took it; when none did the push stops there (the `prev` chain
never skips an unbooked bundle). **Heal:** per site, the names this device pushed (`relay_bundles`
direction `pushed`, status `applied`) that the site's listing lacks are re-packed from the stored header
(`device_id`, `device_seq`, `prev`, `created_utc`) and the rows still linked to the name (`relay_seen`,
`relay_seen_ranges`) and `put` under the same name — at most 16 bundles / 64 MiB per site per run, the
rest reported as `behind`. A re-pack has a fresh nonce and may lack a record retired by a later conflict;
nothing keys on the bytes. No schema change: a site holds what its listing shows.

**Pull.** The union of the listings minus the applied names; each name is tried on every site that lists
it, in list order, until one `get`s and unpacks. A transient error (unreachable, unverified, a clock
refusal, a status) takes that site out of the run; a non-transient error or a failed unpack counts as
`rejected` on that site and the next site is tried; a name is booked `rejected` only when every site
failed it, and stays pending (unbooked) when every site that lists it was transient. `repair_damaged`
fetches the same way.

**Sites.** Before the run every entry is opened: `unavailable` (the folder root is missing, or the
caller passed `"unavailable": true`), `same_relay` (a folder root already opened, by (device, inode); a
LAN base address already opened), `bad_url` (a LAN address `parse_base_url` refuses),
`unsupported_transport` (a `lan` entry on the Python core) — each **reported** on that site and skipped,
never a refusal of the run. A per-call `lan` entry (the phone's list) must also pass the pairing joiner's address
class (an IP literal on a private or on-link network with an explicit port; never a name or a public address) or it
is `bad_url` — the list is the one way an address reaches the sync without the joiner's checks. The core creates
`<root>/<account>` under an existing root, never the root (a root that vanished after the open fails the put as
`missing`; nothing is re-created on the boot disk) — except for the `serve` entry (and so the legacy `{"folder"}`),
this Mac's own folder, which the first put creates as before.
**`not_auto`** (19a): under `sync.run {"auto": true}` every `lan` entry is reported `not_auto` and never opened or
connected to — a `lan` site is never polled on a schedule, only by a click; the run stays `ok` when that is the only
site word (it is the rule, not a failure), and a list with no folder entry is refused `not_folder`.
**Site words during the run** (review 2026-10-07, both cores, the one table the shells map): `too_large`,
`bad_name`, `unreachable` (a LAN relay not answering, or any HTTP status), `refused` (a verified refusal: the
clocks), `unverified`, `missing` (the folder or its account level is gone), `no_permission`, `io_error` (any other
read or write failure, a re-pack that failed included). The stored `rejected` reason of a bundle keeps its own
words (the oracle compares them); a site word is never a path, an address or an OS message. The heal lists with
`list_present`: a folder relay counts an evicted cloud placeholder (`.<name>.icloud`) as present, so a provider
that evicts a file is not fed the same name again with different bytes; the pull keeps `list` (a get of an evicted
copy would fail). The heal re-packs only names under the current account; a stale account's rows (a rotation whose
bookkeeping failed to clear) are ignored. An empty list file (`{"relays": []}`) reads as no relay; `sync relay remove` of the last entry
deletes the file. `--remember` on a list of more than one entry is refused (`sync relay add` keeps the list). The
address class on the Python core allows loopback in every build (it has no debug/release split and no on-link
stage); the Rust core allows loopback in debug and simulator builds only.
**A list of exactly one entry is strict:** that relay's failure is raised as the run's error with the old codes and
texts (`not_found` unreachable, `relay_auth_failed`, `relay_unverified`, `unsupported_transport` on the Python
core); with two or more entries every failure is a site `error` and the run is `partial`.
The result carries `sites: [{id, kind, label, pushed, healed, behind, pulled, rejected, error}]` (`label` the
entry's label, `""` when it has none — user text, never a path; `error` a reason word, never a path, an address or an OS message); the run is `partial` when any site has an error
or the push stopped. The phone's shell owns its list (security-scoped bookmarks are per container) and
passes it per call (`sync.run {"relays": [...]}`); the desktop reads `relay.json`.

## Pairing routes (Bet 12-E): one offer slot on the same server
Only a server started by `disconect-core pair offer` (or a library caller that attaches an offer) carries these
routes; a plain `relay-serve` answers `404` to all of them. The offer's `url` follows the kb/24 offer URL grammar (IPv4, bracketed IPv6 or lowercase hostname, explicit port; shared vectors in `tests/fixtures/pair-offer-urls.json`). The protocol (offer text, key schedule, tags, the six
digits, the sealed payload) is ADR 0011; the constants and vectors are `docs/kb/24-wire-constants.md`. **No
`X-Disconect-Auth` header**: the joiner holds no master yet, and possession of the offer's secret `s` is the proof.
`<id>` is the offer's 32 lowercase hex characters. Checks run top to bottom; the first that fails answers, with an
empty body.

| request | answer |
|---|---|
| any pair route with no offer in the process, a foreign `<id>`, or a path other than the two below | `404` |
| `GET /v1/pair/<id>` or `POST /v1/pair/<id>/payload` (wrong method) | `405` |
| `POST /v1/pair/<id>`, declared `Content-Length` absent or not exactly 64 | `400`, **nothing read** (one status for shorter and longer: no `413`) |
| `POST /v1/pair/<id>`, offer expired (`now > exp`) or aborted | `410` |
| `POST /v1/pair/<id>`, body `joiner_pub (32) ‖ HMAC(K_confirm, "confirm") (32)`, wrong tag or a low-order key | `401`, the offer untouched |
| same, tag valid, offer open | `202`, body = the 64-byte reply `N_o (32) ‖ HMAC(K_offerer, "offerer" ‖ N_o) (32)` (v2, 12-G: the reveal of the nonce the offer committed to in `c`; the joiner checks `SHA-256("disconect/pair/v2/commit" ‖ N_o) == c` and the tag before it shows a code); the offer is bound to `joiner_pub` |
| same, tag valid, offer already bound to the same `joiner_pub` | `202`, the identical body (idempotent) |
| same, tag valid, offer already bound to a different `joiner_pub` | `410` and the offer is aborted: every later request is `410` |
| `GET /v1/pair/<id>/payload`, declared `Content-Length` present and not 0 | `400`, nothing read (a GET with no length is the normal case) |
| same, offer expired or aborted | `410` |
| same, not bound, or bound but the offerer has not released | `202`, empty |
| same, released | `200`, the cached sealed payload (`ChaCha20-Poly1305`, AAD = transcript), on **every** GET until expiry or process exit |

A body that stops arriving after a good declared length is `408`, as everywhere. `202` ("Accepted") and `410`
("Gone") have their own reason phrases. The access log is the server's: method and status only; the offer text, the
code, a public key, a tag and a peer address are never logged. The offerer releases only when the typed digits equal
its code and the offer is neither aborted nor expired at that moment (re-checked under the slot's lock); the payload
is sealed once and cached.
