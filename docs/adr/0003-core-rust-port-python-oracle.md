# ADR 0003 — Core language: Rust under Tauri; Python stays the oracle. Timing = Guy's call

- **Status:** ACCEPTED 2026-10-02 (Guy) — **timing = Defer**: the macOS app ships on the Python
  core (Tauri UI + embedded/sidecar Python); the Rust port starts at the phone bet. From the
  day porting starts, new logic is Rust-first. v2 text after the opus challenge ("ACCEPT WITH
  CHANGES") kept below as the record.
- **Context:** the v0 core is ~4,400 lines of Python 3.12+. Correction from the review: it is
  **not** a hand-written FIT decoder — fitdecode 0.11 (profile 21.171) parses bytes; what is
  hand-written is the *meaning layer* (handlers, sentinels, `expand_timestamp_16`,
  sleep-stage-END logic, the midnight step counter in `writer.derive_daily_steps`). The port
  risk is therefore **how a different parser names and represents things**, not byte decoding.

## Options for the core language (facts in kb/19)
| Option | Rating | Why |
|---|---|---|
| **Rust in-process** (fitparser 0.11 / profile 21.202, rusqlite+SQLCipher, btleplug) | chosen eventual core | one core on all five shells; natural under Tauri (ADR 0002); runs headless for MCP and inside short mobile background wakes (startup/memory) |
| Python everywhere (Briefcase, or CPython embedded via PyO3 — no subprocess needed) | viable for desktop; thin for phones | Tier 3 on iOS/Android; mobile wheels for sqlcipher3 very likely missing; background execution from embedded Python unverified. **Narrow claim:** the real phone risk is whether *Tauri mobile* runs in the background at all (ADR 0002 revisit), not Python vs Rust per se |
| Kotlin Multiplatform core | 4/10 | best Android background story, but no permissively licensed FIT parser (Garmin's Java SDK is off-limits), desktop needs a JDK, overturns ADR 0002 |
| TypeScript core in the webview (wa-sqlite/OPFS, WebCrypto) | 3/10 | runs in neither iOS background wakes nor headless for the MCP server → a second runtime anyway |

(The "ZeppBridge proves the shape" argument is withdrawn: it is cloud-fed, no FIT, no BLE.)

## Decision A — what (independent of timing)
1. Rust is the eventual core; Python keeps shipping CLI + MCP and is the **differential-test
   oracle** until parity. **Amended 2026-10-02 (Bet 11 pitch v2):** since Bet 5 the oracle reads
   SQLCipher stores, so the Rust core opens the *same* store with the *same* key file and the
   diff is table-level over two stores fed the same bytes; the canonical-JSON dump below is the
   fallback if SQLCipher parity fails its 2-h time-box. Slice 0 (2026-10-02): parser diff over
   424 real files → one behavioural difference (`docs/kb/21-fitparser-name-map.md`). **Enum text decision (2026-10-03, review finding 2):** the Rust
   core stores fitparser's enum names; where fitdecode 0.11 knows no name (today: sport 63,
   sub_sport 77 — 36 values in all, additions only) the oracle stores the number and a Python
   `reparse` of a Rust-written store rewrites those cells to digits. Accepted for the two-core
   period: the harness tolerates exactly the verified (number → name) pairs, the flip touches
   only `activities.sport/sub_sport` and the `vo2max_sport` label, and it disappears when the
   oracle retires (slice 3). Upgrading fitdecode's profile is upstream's act, not ours. The diff runs on **canonical JSON exports** from both cores (float
   rounding pinned in the contract fixture), not on a shared database (the oracle would
   otherwise need sqlcipher3 to read a Rust-owned encrypted schema).
2. **Slice 1's first deliverable is a fitdecode→fitparser name/representation map** (file
   types `"49"/"68"/"73"/"44"`, `SLEEP_EVENT_CODE "74"`, enum strings like `"off_wrist"`,
   `activity_type` values, unknown-field spelling, subfield resolution `steps` under `cycles`,
   accumulated fields). Revisit trigger: the map needs more than ~10 special cases.
3. **Honest sizing (with an AI pair):** toolchain 2–3 h (done) · name map 4–6 · model/store/
   migrations 8–10 · writer + derived dailies 8–10 · connect_export 6–8 · diff harness 4–6 ·
   mismatch hunting 8–12 → **40–55 h ≈ 6–12 weeks** at 4–8 h/week. Either slice 1 drops
   `connect_export` (diff on FIT-only streams first) or it carries that appetite; slices 2–3
   (facts/insight, MCP, health) go on the roadmap **before** the desktop app can replace the CLI.
4. From the day porting starts, **new logic is written Rust-first** (encryption, relay client,
   recompute), never Python-then-port.
5. **Pre-decision experiment (≤4 h, inside Bet 3):** a ~50-line fitparser dumper vs fitdecode
   over one real file from each of the six streams; diff per message: counts, field names, enum
   strings and the exact values the handlers read (`cycles`/`steps` per activity type,
   `timestamp_16`, `sleep_level`, event 74, SpO2 mode, `file_id.type`). Answers this ADR's own
   revisit trigger in 4 h instead of 30 and sizes the name map.

## Decision B — when (Guy)
| | **Port-first** (start Bet 5 next) | **Defer** (Mac app ships on the Python core; port at the phone bet) |
|---|---|---|
| macOS app with charts + pull | after slice 1–2: ~2–4 months | ~2 months sooner: Tauri UI over the Python core (sidecar or PyO3), 15–25 h of glue that is later thrown away (signing/notarizing bundled Python, sqlcipher3 hookup) |
| Double work | none for new logic (Rust-first rule) | Bets 6 (encryption), 9 (recompute, 12–24 h), 11 (relay client) written in Python then ported |
| Daily use meanwhile | CLI + MCP in Claude Desktop (already works) | same, plus the Mac app earlier |
| Risk | phone bets are 1–1.5 years of appetite away; Rust effort could be sunk if the project stalls before them | a two-language core for a long stretch; Python-on-phone remains unproven if the port is never started |

## Consequences
- PROGRESS/ROADMAP: Bet 5 re-sized per §A.3; slices 2–3 inserted before Bet 8 if Port-first.
