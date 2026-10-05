# ADR 0011 — Device pairing: ephemeral X25519 over a QR secret, a short authentication string, a single-use offer

- **Status:** PROPOSED 2026-10-06 (Bet 12-E pitch v1; ACCEPTED when the opus attack passes and the
  Rust build's vectors are frozen in `docs/kb/24-wire-constants.md`). Refines ADR 0004 §3.

## Decision
- **Transport:** the LAN relay's server (`relay/lan_server.rs`, ADR 0005's self-hosted adapter) carries
  the three pairing messages under `/v1/pair/<id>` without the token header; the relay's objects stay
  behind the token. The Python core keeps refusing `lan`; it twins only the pure functions.
- **Secrets in the offer (QR / pasted text):** an ephemeral X25519 public key, a 128-bit secret `s`, an
  offer id, an expiry, the relay URL. `s` is what a network-only attacker lacks; the public key is how the
  joiner authenticates the offerer; the expiry is 15 minutes (ADR 0004).
- **Key schedule:** `K = HKDF-SHA256(ikm = X25519(dh), salt = s, info = transcript)`; subkeys for the
  confirm tag, the SAS and the payload by HKDF-expand with distinct labels; all-zero `dh` refused.
- **Human check:** a 6-digit SAS from `K` on both screens; the offerer releases only on the user's
  confirmation; **the first well-formed reply takes the offer, a second one aborts it** — the QR-photo
  attacker either loses the race (the user sees no matching code and declines) or kills the offer.
- **Payload:** `master ‖ the offerer's key file bytes` under `AEAD(K_payload)` with the transcript as
  AAD; the joiner writes the key file verbatim (the Rust core never generates one), keeps the master in
  its keychain, then pulls the store through the relay (12-B's bootstrap).

## Rejected
- **A PAKE over a hand-typed code** (SPAKE2/OPAQUE): stronger against a photographed QR but a new
  dependency and a new primitive family; the SAS + single-use offer cover the same attacker for v1.
- **Pairing through the blind relay:** the account name is `HKDF(master)` — the joiner cannot name the
  mailbox before it holds the master; and the relay has no `delete` for the offer's cleanup.
- **TLS on the LAN server:** a self-signed certificate adds a trust dialog on every device and protects
  nothing the AEAD and the token do not; the offer's public key already authenticates the offerer.
- **Sending the master alone:** a device with no key file is plaintext to the cores; the wrapped file is
  what makes the passphrase fallback work on the new device.

## Consequences
- Four new frozen labels in `kb/24` (`disconect/pair/v1` and its three subkey infos), vectors pinned on
  both cores; `x25519-dalek` (BSD-3) joins the core's dependencies.
- The phone's pairing (Bet 12 slice E on iOS, Bet 14) is a client of this protocol: scan → the same join
  → the same SAS; nothing in the protocol depends on the device class.
- A recovery-word rotation changes the master → the paired device must re-pair (ADR 0004).
