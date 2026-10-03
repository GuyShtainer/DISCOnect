# DISCOnect — target architecture (2026-10-02)

*This repository holds the Python core only (CLI, MCP server, serve protocol, relay client, pairing). The Rust core and the desktop/phone shells this document also describes are developed separately and are not published here.*

One core, one UI, five shells; the user's PC is the brain; any remote box is blind.

```
                    ┌──────────────────────── user's devices (hold the key) ───────────────────────┐
                    │                                                                             │
 fenix 8 ──USB/MTP──▶  DESKTOP app (macOS first; Win10, Linux)      PHONE app (iOS 18.1+, Android) │
 (FIT files;        │  ┌───────────────────────────────────┐        ┌──────────────────────────┐   │
  stable layer)     │  │ Tauri 2 shell · web UI (ECharts)  │        │ Tauri 2 shell · same UI  │   │
                    │  ├───────────────────────────────────┤        ├──────────────────────────┤   │
                    │  │ Rust core: MTP pull · FIT decode  │◀─sync──▶│ Rust core: decode · store│                
                    │  │ (fitparser) · SQLCipher store ·   │ bundles │ · facts ·                │           
                    │  │ contract · facts · MCP server     │ (age)   │                          │            
                    │  │ · bundled Python oracle? NO —     │         │ no cable pull on iOS     │        
                    │  │   Python stays a dev-time oracle  │         │ USB-OTG pull on Android  │
                    │  └──────────────┬────────────────────┘        └──────────────────────────┘   │
                    │                 │ stdio MCP (read-only)         QR pairing: ephemeral X25519  │
                    │                 ▼                              + fingerprint phrase; master   │
                    │   Claude / ChatGPT desktop (AI coaching today)   key wrapped device→device    │
                    │   in-app coaching later (user's keys)                                        │
                    └───────────────────────────────┬─────────────────────────────────────────────┘
                                                    │ opaque encrypted blobs only
                                                    ▼
                               ┌────────────────────────────────────────────┐
                               │ BLIND RELAY (optional): self-hosted blob    │
                               │ service or user's S3/GCS bucket. Stores      │
                               │ ciphertext + sequence numbers. Cannot decode, │
                               │ decrypt or compute. Leaks sizes/times only.   │
                               └────────────────────────────────────────────┘
```

**Key hierarchy (ADR 0004):** random master key → wraps the SQLCipher DB key; master key wrapped
by (a) Argon2id(passphrase) and (b) a recovery word list; cached unwrapped in the OS keychain
for daily use. Losing passphrase + recovery words = data gone (stated in UI).

**Data flow:** watch bytes → encrypted DB (`raw_records` retained, content-addressed) → canonical
rows (`device` / `vendor_cloud` / `local` / `live` scopes, never merged) → facts/contract → UI, MCP,
change bundles. Decoder fixes are replays (`reparse`), never re-pulls. Live-link sessions are
`json:live` raw records folded by a writer post-pass into `live`-scope samples, one per
metric and UTC minute (lower median, FIT's sentinel rules, readings de-duplicated across records):
a pure function of the raw set, rebuilt after every import, pull and reparse, never a
daily and never a day of coverage.

**What is stable vs tracked:** FIT files over MTP = stable, additive contract (spine). Anything
else the watch exposes changes with firmware/SDK = tracked, never load-bearing.

**Shells & floors:** macOS (verified Sequoia) → Windows 10 (real box) → Linux
(CI, untested) → iOS 18.1 (iPhone 16 Pro, free provisioning) → Android API 24 (emulator).
Cable pull: libmtp (mac/Linux), WPD via `winmtp` (Windows), USB-OTG (Android), none (iOS).

**Licensing:** this core is AGPL-3.0-or-later (`LICENSE`). How the shipped apps will be licensed is
not decided in this repository.

**Python core today:** ships CLI + MCP and is the differential-test oracle for every Rust slice;
retired only at parity.
