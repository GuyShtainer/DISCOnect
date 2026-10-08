# ADR 0005 — Zero-knowledge sync relay ("PC is the brain, relay is blind")

- **Status:** ACCEPTED 2026-10-02 (the maintainer). The maintainer: remote server "could also be synced with
  distance server on aws or google … But data there is encrypted! Without the key!"

## Decision
- The relay is a **dumb object store**: PUT/GET/LIST of opaque, client-encrypted blobs under
  a per-user prefix. Nothing on it can decrypt, decode, or compute. *(Amended 2026-10-02, relay
  shaping: no sequence number — S3/GCS assign none, client-assigned ones collide between
  desktops, and folder transports deliver out of order. Pull is a set difference: every object
  name not yet applied. Ordering per device travels inside the ciphertext.)* Two adapters from day one: **self-hosted** (a tiny AGPL
  service or plain WebDAV) and **S3/GCS-compatible bucket** (user's own credentials).
- Design = **Joplin's dumb-target model + Ente's key hierarchy**: the desktop (brain) ingests,
  decodes, computes and publishes encrypted **change bundles** (ChaCha20-Poly1305 under a key
  derived from the shared master — `age` rejected 2026-10-02: one symmetric master, the
  primitives already in `storage/keys.py`, no second format; chunked);
  phones pull bundles and apply them to their local SQLCipher copy. Phones may also publish
  bundles (BLE-collected data later); the desktop merges by `(stream, source_key)` identity —
  the same content-addressed keys the store already uses, so merge conflicts reduce to
  "same bytes" or "newer observation wins" (`observed_utc` of the decoded facts, ties by the
  larger payload hash — a pure function of the two rows, so every device converges), never
  field-level merges. Coverage claims (`export_ranges`) travel too and merge as a union.
  *(Amended 2026-10-03: that rule makes **raw records** converge; daily rows converge
  because they are a deterministic function of the converged raw set — rebuilt from the raw
  records of every touched JSON stream, in the content order `(start_utc, payload_hash, stream,
  source_key)`, at the end of every import and every pull. No per-write tie-break exists. Known
  exception: a stored record whose bytes no longer decode keeps its rows and is not repaired by the
  relay; pull rejects damaged bytes, so the damage never spreads.)*
- Metadata leakage accepted and documented: blob sizes, timestamps, counts (rclone-crypt
  class of leakage). Padding/batching to daily bundles reduces it; not eliminated.
- No accounts on the relay beyond the bucket/service credentials the user already holds.
  The relay account (the first path segment, `HKDF(master)`) is a visible, non-secret per-user prefix: it
  names a user's objects to anyone who can list the relay, and proves nothing about the master.

## Rejected
- Server-side compute with server-held keys (classic self-host): violates "without the key".
- Syncthing-style peer mesh: no blind intermediary for phone-off-LAN cases; revisit as an
  optional LAN transport later.

## Consequences
- The self-hosted **web app** (planned) runs only on a device that holds the key (the user's
  own box), never on the blind relay.
- The first version implements the folder adapter and the protocol; networked adapters
  (S3/GCS) came later. Evidence must show the relay operator cannot read anything
  (canary + AEAD-only put + wrong-master failure), and that two desktops converge.

## Amendment 2026-10-07 (after review) — a list of blind stores
The maintainer's condition on this amendment: more than one cloud service **and** the desktop at once, no
restrictions. The relay becomes a **list** (`relay.json {"relays": [...]}`; the legacy `{"folder"}` and
`{"lan"}` forms read as a one-entry list). Every bundle goes to every relay under the **same name** and
the pull is the union of the listings minus what is applied, each name tried on every site that lists
it until one unpacks. **A site holds what its listing shows:** no per-site table — per site, the names
this device pushed minus the listing are re-packed and put again, within a budget (16 bundles /
64 MiB per site per run). What the attack weighed and this ADR accepts:
- The account prefix already links a user's objects across relays, so the same name and size on every
  relay add nothing an observer of several relays did not have; per-relay names would break the `prev`
  chain and the union by name; per-relay padding buys nothing while the prefix is shared.
- The LAN token is per master, not per host: a captured request works at any relay of the account for
  300 s — PUT writes the same ciphertext, GET returns ciphertext, DELETE is 405. Accepted.
- A rotation leaves the old account's objects on every relay in the list (as it left them on the one).
- A re-packed bundle carries a fresh nonce and only the rows still linked to its name (a version
  retired by a later conflict is absent; the winner travels in its own bundle). Every consumer keys by
  name, `seq`/`prev` or record identity, so the two versions are interchangeable. Stated plainly
  (review 2026-10-07): a site written only by this device therefore never holds a retired version at
  all after a heal — a fresh reader of that site alone sees the winners only, which is the converged
  state, not a loss.
- A served folder that is also a cloud folder: the client writes `.tmp-` files readers ignore and a
  bundle is packed once per run. A heal, however, puts *different bytes under a name the listing lacks*
  (fresh nonce), so two routes to one cloud folder (the phone's and the Mac's) or a provider that evicts
  a file from the listing (iCloud's `.name.icloud` placeholder) can make a provider conflict copy and a
  re-upload of up to the heal budget per site per sync. Corrected 2026-10-07 (review): the heal counts
  an evicted placeholder as present; two routes to one folder remain a known cost (open: dedupe by
  listing the sibling route's names before healing).
- One compromised cloud client can keep every pull `partial` with a planted object (re-fetched on every
  run, times N sites); a bad copy on one site never shadows a good one on another. Backing off a
  rejected name is open work.

