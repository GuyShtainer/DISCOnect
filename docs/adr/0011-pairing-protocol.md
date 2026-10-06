# ADR 0011 — Device pairing: ephemeral X25519 over a QR secret, mutual key confirmation, a short authentication string, a single-use offer

- **Status:** AMENDED 2026-10-07 (12-G: protocol v2, commit-reveal of the SAS nonce; see the amendment at the end; v1 never paired a device, so nothing migrates). ACCEPTED 2026-10-06 05:55 (Bet 12-E: vectors frozen in `docs/kb/24-wire-constants.md` § Pairing
  vectors, routes frozen in `relay-protocol.md` § Pairing routes, opus review ACCEPT WITH FIXES applied
  `c424280`, two loopback pairings by the release binary). Refines ADR 0004 §3. PROPOSED 2026-10-06 00:40.

## Decision
- **Transport:** the LAN relay's server (`relay/lan_server.rs`, ADR 0005's self-hosted adapter) carries
  the pairing messages under `/v1/pair/<id>` without the token header; the relay's objects stay behind
  the token; the pair routes are `404` when no offer is attached to the server (an ended, expired or aborted, offer answers `410`) and read no body before the declared length
  is checked. `pair offer` is `relay-serve` plus one offer slot: it pushes the store first and keeps
  serving after the offer ends. The Python core keeps refusing `lan`; it twins only the pure functions,
  written from this text.
- **Secrets in the offer (QR / pasted text):** an ephemeral X25519 public key, a commitment
  `c = SHA-256("disconect/pair/v2/commit" ‖ N_o)` to a 32-byte nonce `N_o` the offerer draws with the offer
  and reveals only after the join is bound (v2, 12-G), a 128-bit secret `s`, an offer id, an expiry, the
  relay URL. `s` is what a network-only attacker lacks; the public key is how the
  joiner authenticates the offerer; the expiry is 15 minutes (ADR 0004). The offer text enters the joiner
  by stdin or a scan, never by argv.
- **Key schedule:** `K = HKDF-SHA256(ikm = X25519(dh), salt = s, info = transcript)` with
  `transcript = "disconect/pair/v2" ‖ 0x00 ‖ offer_pub ‖ joiner_pub ‖ id ‖ exp(8 B BE) ‖ c` (138 B; `c` last,
  so `K`, both tags and the payload AAD bind the commitment); four subkeys by
  HKDF-Expand from `K` under `disconect/pair/v2/{confirm,offerer,sas,payload}`; a non-contributory `dh`
  (low-order or all-zero key) is refused by both sides.
- **Mutual key confirmation:** the joiner's reply carries `HMAC(K_confirm, "confirm")`; the offerer's
  `202` carries `N_o ‖ HMAC(K_offerer, "offerer" ‖ N_o)` (64 B), and **the joiner shows no code before the
  length, the commitment (`SHA-256("disconect/pair/v2/commit" ‖ N_o) == c`, constant time) and the tag all
  verify, in that order**. The commitment check is the critical one (it is what makes the offerer's nonce
  unchangeable after the joiner's key is known); the tag over `N_o` is defence in depth and never replaces
  it. Without the tag an attacker holding `s` and a MITM position could read the joiner's code and grind a key
  whose SAS matches it (≈10⁶ tries); with it, a code on the joiner's screen means the offerer has bound
  the joiner's own key, and any later key is a second, different one. Without the commitment the offerer's
  code was computable offline by anyone holding the offer text (see the amendment).
- **Human check:** a 6-digit SAS, `first 4 bytes of HMAC(K_sas, "sas" ‖ N_o)` big-endian mod 10⁶, **shown
  on the joiner only**: the offerer never displays it (the app since design decision 6, the CLI since 12-G)
  and only compares the digits the user types into it (a habitual `y` must not survive a wrong code); the
  offerer releases only on an exact match, re-checked against abort and expiry under the slot's lock. There
  are two human steps, not one: (1) the joiner's digits typed into the offerer protect the offerer's master;
  (2) the joiner's landing confirmation ("did the other device say paired?", 12-F) is the only defence
  against a planted offerer that releases its *own* master to the joiner.
- **Single-use offer:** the first well-formed reply binds the joiner's key; **the same key again is
  idempotent** (an honest retry or a replay changes nothing); **a different key with a valid tag aborts
  the offer**; a wrong tag never touches it. **A joiner key, once sent, is never used with any other
  offer** (the client's `Joiner` is pinned to the whole parsed offer, every field; a retry re-sends the
  identical body; a fresh key only on a user action such as a new scan). Bounds: a network-only attacker can
  do nothing; the QR-photo attacker can deny a pairing, never obtain the key; the attacker who holds the
  photo *and* plants an offer in front of the joiner gets one guess at 10⁻⁶ per offer on the offerer's side
  and one per fresh scan on the joiner's side, and no automatic re-offer or re-scan exists to amplify it
  (v1 let that attacker grind the offerer's code offline: 12-F's attack, fixed by 12-G).
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
  dependency and a new primitive family; mutual confirmation + the commit-reveal SAS + the single-use
  offer hold the photo-plus-planted-offer attacker to one guess per offer (v2); a PAKE would also take the
  photo attacker's *deny* away, which is not worth the primitive yet.
- **Pairing through the blind relay:** the account name is `HKDF(master)` — the joiner cannot name the
  mailbox before it holds the master (the relay's `delete`, added by 12-B, would have served the cleanup;
  the chicken-and-egg is the reason that stands).
- **TLS on the LAN server:** a self-signed certificate adds a trust dialog on every device and protects
  nothing the AEAD and the token do not; the offer's public key already authenticates the offerer.
- **Sending the master alone:** a device with no key file is plaintext to the cores; the wrapped file is
  what makes the passphrase fallback work on the new device.
- **Single delivery of the payload (`200` once, then `410`):** see Payload.

## Consequences
- Frozen in `kb/24`: the offer prefix `disconect-pair:v2.`, `disconect/pair/v2`, the four subkey labels,
  the commitment label, the three HMAC messages, the transcript layout, the seven-key offer JSON order; vectors (RFC 7748 §6.1 keys, intermediates,
  both tags, SAS with a leading-zero case, the sealed payload) and negative vectors pinned on both cores;
  `x25519-dalek` + `curve25519-dalek` (BSD-3) join the core's dependencies **and the app bundle** (the app
  links the core in-process) — notices booked in BACKLOG 02b.
- The routes and status semantics (`401` wrong tag, `202` bound with the offerer tag, `410` aborted or
  expired, `404` no offer attached, `200` cached payload, exact declared lengths, the 64-byte `202`) are frozen in
  `relay-protocol.md`; the phone's pairing (Bet 12 slice E on iOS, Bet 14) is a client of this protocol and
  of those routes: scan → the same join → the same SAS; nothing depends on the device class. Pairing from
  the Mac app means the app runs the LAN server — a decision for the app follow-up.
- Every paired device holds the passphrase-wrapped key file: an offline Argon2id target on each device and
  in its backups; the phone bet excludes the key file from device backups.
- A recovery-word rotation changes the master → the paired device must re-pair (ADR 0004); `join`
  refuses an existing store, so a re-pair needs the booked `--replace` path (push the unsent records
  under the old master, then replace).

## Amendment 2026-10-07 (12-G): commit-reveal of the offerer's SAS nonce — protocol v2
**The hole (found by the ios-toolkit session's 12-F attack).** In v1 the code was `HMAC(K_sas, "sas")` and
every offerer-side input to `K` was in the offer text. Whoever held the text (a photographed QR, the Copy
button's text on a shared pasteboard) could compute offline, for any joiner key it might choose, the code the
offerer would show: ≈10⁶ X25519 + HKDF evaluations, seconds. With a planted offer in front of the joiner as
well, the attacker joined the planted offer's victim, read its code `X`, then joined the real offerer with a
key ground so the offerer's code was `X`; the user read `X` on the phone, typed `X` into the Mac, the Mac
released the master to the attacker. The v1 text's "the QR-photo attacker can deny a pairing, never obtain
the key" was true only without the planted offer.

**The fix (as Bluetooth numeric comparison).** The offerer commits to a fresh 32-byte `N_o` in the offer
(`c`, with its own label so the hash is never confused with another use of SHA-256 over 32 bytes) and reveals
it only in the `202`, after the joiner's key is bound and cannot change; the code takes `N_o`. Against the
offerer the attacker must fix its key before it learns `N_o` (one draw per offer; a second key aborts).
Against the joiner `c` is fixed in the planted text before the joiner draws its ephemeral key, the joiner's
code depends on that key through `K`, and the joiner verifies the reveal against `c` before showing a digit
(one draw per fresh scan). The joiner's ephemeral key is its nonce: drawn after the offer is in hand, fresh
per scan, never reused with another offer; a separate joiner nonce would add 32 bytes for nothing.

**Invariants the implementations keep.** `N_o` is drawn with `getrandom` per offer and is a secret until the
bind: held zeroized in the offer slot, wiped with the private key when the offer ends, never in an event, a
log, a status answer or a `Debug` output. The commitment check is never dropped "because the tag covers
`N_o`". The reveal and tag check is one pure function on both cores (`open_offerer_reply`), so the oracle
pins its negatives. The joiner shows no code on any failure, and the user is told to cancel the offer on the
other device and start a new one (new `s`, new `N_o`: the attacker needs a new photo per draw); nothing
re-scans or re-keys on its own. The CLI offerer prints "A device answered." and never its own code. The
bound stated above (10⁻⁶ per offer, per fresh scan) holds per offer × per scan: an attacker bound on the
offerer first and withholding the `202` from the joiner until the joiner's code matches gets `k·10⁻⁶` for `k`
scans the user performs within one offer's 15 minutes, each a visible failure on the phone.
