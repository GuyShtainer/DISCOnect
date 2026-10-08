# ADR 0004 — Encryption at rest and the key model

- **Status:** ACCEPTED 2026-10-02 (the maintainer). The maintainer's requirement: "encrypted as soon as possible
  and be able to decrypt only for the user using a key shared on pc and phone."

## Decision
1. **Whole-database encryption with SQLCipher** (via `rusqlite` `bundled-sqlcipher-vendored-
   openssl`): every page, index and the schema are ciphertext at rest; journals/temp files
   covered by SQLCipher's own handling. Raw FIT payloads already live inside the DB
   (`raw_records`), so they are covered too. Rejected: encrypting payload blobs only (leaves
   queries, indexes and metadata in plaintext).
2. **Key hierarchy (Ente's pattern):** a random 256-bit **master key** encrypts the DB key;
   the master key is wrapped by (a) a passphrase-derived key (Argon2id) and (b) a **recovery
   key** shown once as a BIP39-style word list. Losing both = data gone; stated plainly in the
   UI. On devices with a keychain/Secure Enclave the unwrapped master key is cached there so
   daily use needs no passphrase; OS Data Protection is a complement, never the key itself.
3. **Device pairing PC↔phone:** the already-trusted device shows a QR carrying an ephemeral
   X25519 public key + a short fingerprint phrase; the new device scans, both derive a shared
   secret, the master key is sent wrapped under it, the user confirms the fingerprint on both
   screens (Bitwarden's device-approval shape; 15-minute expiry). No server involvement.
4. **Pre-pull encryption:** files pulled from the watch are written straight into the
   encrypted DB; the plaintext pull folder (a `raw/` folder in the maintainer's data directory) is an interim and is
   deleted by the app once ingested (user-visible toggle "keep raw copies").

## Consequences
- The relay (ADR 0005) only ever sees ChaCha20-Poly1305 bundles under a key derived from the
  master (`age` rejected 2026-10-02 when the relay was shaped: no new dependency, no second format).
- The MCP server needs the key at start (keychain or passphrase prompt); read-only mode stays.
- Performance cost of SQLCipher (PBKDF2 at open, per-page AES) is acceptable at ~1.4 M
  rows/year; measure it.
- Backups: encrypted snapshots via SQLite's backup API remain valid as-is.
- Decided: Argon2id m=256 MiB t=3 p=1. Decided for the relay: change bundles are
  ChaCha20-Poly1305 of canonical raw-record rows, not SQLCipher page diffs.
- Master rotation (`key rotate-recovery`) also rotates the relay account and key: bundles under
  the old master are re-pushed under the new one and the old prefix is deleted or warned about;
  a passphrase change leaves the master, and therefore the relay, unchanged.
