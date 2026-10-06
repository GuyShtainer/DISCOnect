# ADR 0011 — Device pairing: ephemeral X25519 over a QR secret, mutual key confirmation, a short authentication string, a single-use offer

- **Status:** ACCEPTED 2026-10-06 05:55 (Bet 12-E: vectors frozen in `docs/kb/24-wire-constants.md` § Pairing
  vectors, routes frozen in `relay-protocol.md` § Pairing routes, opus review ACCEPT WITH FIXES applied
  `c424280`, two loopback pairings by the release binary). Refines ADR 0004 §3. PROPOSED 2026-10-06 00:40.

## Decision
- **Transport:** the LAN relay's server (`relay/lan_server.rs`, ADR 0005's self-hosted adapter) carries
  the pairing messages under `/v1/pair/<id>` without the token header; the relay's objects stay behind
  the token; the pair routes are `404` when no offer is attached to the server (an ended, expired or aborted, offer answers `410`) and read no body before the declared length
  is checked. `pair offer` is `relay-serve` plus one offer slot: it pushes the store first and keeps
  serving after the offer ends. The Python core keeps refusing `lan`; it twins only the pure functions,
  written from this text.
- **Secrets in the offer (QR / pasted text):** an ephemeral X25519 public key, a 128-bit secret `s`, an
  offer id, an expiry, the relay URL. `s` is what a network-only attacker lacks; the public key is how the
  joiner authenticates the offerer; the expiry is 15 minutes (ADR 0004). The offer text enters the joiner
  by stdin or a scan, never by argv.
- **Key schedule:** `K = HKDF-SHA256(ikm = X25519(dh), salt = s, info = transcript)` with
  `transcript = "disconect/pair/v1" ‖ 0x00 ‖ offer_pub ‖ joiner_pub ‖ id ‖ exp(8 B BE)`; four subkeys by
  HKDF-Expand from `K` under `disconect/pair/v1/{confirm,offerer,sas,payload}`; a non-contributory `dh`
  (low-order or all-zero key) is refused by both sides.
- **Mutual key confirmation:** the joiner's reply carries `HMAC(K_confirm, "confirm")`; the offerer's
  `202` carries `HMAC(K_offerer, "offerer")`, and **the joiner shows no code before that tag verifies**.
  Without it an attacker holding `s` and a MITM position could read the joiner's code and grind a key
  whose SAS matches it (≈10⁶ tries); with it, a code on the joiner's screen means the offerer has bound
  the joiner's own key, and any later key is a second, different one.
- **Human check:** a 6-digit SAS from `K` on both screens; the user types the joiner's digits into the
  offerer (a habitual `y` must not survive a wrong code); the offerer releases only on an exact match,
  re-checked against abort and expiry under the slot's lock.
- **Single-use offer:** the first well-formed reply binds the joiner's key; **the same key again is
  idempotent** (an honest retry or a replay changes nothing); **a different key with a valid tag aborts
  the offer**; a wrong tag never touches it. The QR-photo attacker can therefore deny a pairing, never
  obtain the key; a network-only attacker can do neither.
- **Payload:** `master ‖ the offerer's key file bytes` under `AEAD-ChaCha20-Poly1305(K_payload, nonce 0)`
  with the transcript as AAD, **sealed exactly once and cached**, served on every `GET` until expiry or
  process exit (single delivery protected nothing: the ciphertext opens only with `K`, and the one `200`
  could be stolen or lost). The joiner checks `key_id_for(master)` against the key file, then writes
  `relay.json`, the key file verbatim and atomically (the commit point; the Rust core never generates one),
  creates the store with the in-memory master and pulls in-process; the CLI writes no keychain item (a
  CLI-made item is foreign to the app and `set` would delete the app's) — the library hands the master
  back to its caller.

## Rejected
- **A PAKE over a hand-typed code** (SPAKE2/OPAQUE): stronger against a photographed QR but a new
  dependency and a new primitive family; mutual confirmation + the SAS + the single-use offer cover the
  same attacker for v1.
- **Pairing through the blind relay:** the account name is `HKDF(master)` — the joiner cannot name the
  mailbox before it holds the master (the relay's `delete`, added by 12-B, would have served the cleanup;
  the chicken-and-egg is the reason that stands).
- **TLS on the LAN server:** a self-signed certificate adds a trust dialog on every device and protects
  nothing the AEAD and the token do not; the offer's public key already authenticates the offerer.
- **Sending the master alone:** a device with no key file is plaintext to the cores; the wrapped file is
  what makes the passphrase fallback work on the new device.
- **Single delivery of the payload (`200` once, then `410`):** see Payload.

## Consequences
- Frozen in `kb/24`: the offer prefix, `disconect/pair/v1`, the four subkey labels, the three HMAC
  messages, the transcript layout, the offer JSON key order; vectors (RFC 7748 §6.1 keys, intermediates,
  both tags, SAS with a leading-zero case, the sealed payload) and negative vectors pinned on both cores;
  `x25519-dalek` + `curve25519-dalek` (BSD-3) join the core's dependencies **and the app bundle** (the app
  links the core in-process) — notices booked in BACKLOG 02b.
- The routes and status semantics (`401` wrong tag, `202` bound with the offerer tag, `410` aborted or
  expired, `404` no offer attached, `200` cached payload, exact declared lengths) are frozen in
  `relay-protocol.md`; the phone's pairing (Bet 12 slice E on iOS, Bet 14) is a client of this protocol and
  of those routes: scan → the same join → the same SAS; nothing depends on the device class. Pairing from
  the Mac app means the app runs the LAN server — a decision for the app follow-up.
- Every paired device holds the passphrase-wrapped key file: an offline Argon2id target on each device and
  in its backups; the phone bet excludes the key file from device backups.
- A recovery-word rotation changes the master → the paired device must re-pair (ADR 0004); `join`
  refuses an existing store, so a re-pair needs the booked `--replace` path (push the unsent records
  under the old master, then replace).
