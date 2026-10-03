# ADR 0005 — Zero-knowledge sync relay ("PC is the brain, relay is blind")

- **Status:** ACCEPTED 2026-10-02 (Guy, Bet 3). Guy: remote server "could also be synced with
  distance server on aws or google … But data there is encrypted! Without the key!"

## Decision
- The relay is a **dumb object store**: PUT/GET/LIST of opaque, client-encrypted blobs under
  a per-user prefix. Nothing on it can decrypt, decode, or compute. *(Amended 2026-10-02, Bet 10
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
  *(Amended 2026-10-03, Bet 10b: that rule makes **raw records** converge; daily rows converge
  because they are a deterministic function of the converged raw set — rebuilt from the raw
  records of every touched JSON stream, in the content order `(start_utc, payload_hash, stream,
  source_key)`, at the end of every import and every pull. No per-write tie-break exists. Known
  exception: a stored record whose bytes no longer decode keeps its rows and is not repaired by the
  relay; pull rejects damaged bytes, so the damage never spreads.)*
- Metadata leakage accepted and documented: blob sizes, timestamps, counts (rclone-crypt
  class of leakage). Padding/batching to daily bundles reduces it; not eliminated.
- No accounts on the relay beyond the bucket/service credentials the user already holds.

## Rejected
- Server-side compute with server-held keys (classic self-host): violates "without the key".
- Syncthing-style peer mesh: no blind intermediary for phone-off-LAN cases; revisit as an
  optional LAN transport later.

## Consequences
- The self-hosted **web app** (Bet 16) runs only on a device that holds the key (the user's
  own box), never on the blind relay.
- Bet 10 implements the folder adapter and the protocol (pitch 10 v2); networked adapters
  (S3/GCS) move to Bet 12's shaping. Evidence must show the relay operator cannot read anything
  (canary + AEAD-only put + wrong-master failure), and that two desktops converge.
