"""Command line: import files, replay decoding, data health, facts, exports, backups.

Exit codes are a published contract (may be added to, never redefined):
0 ok, 1 failed, 2 usage, 3 not configured (no database yet), 4 busy (another
writer), 6 database error, 7 schema newer than this build, 8 backup or
restore refused (missing, tampered, or newer-schema snapshot), 9 encrypted store
locked or key refused (no unlock path, wrong passphrase, missing key file).

With ``--json`` stdout carries only JSON; everything human goes to stderr, so
a script never has to untangle the two.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import pathlib
import sys

from disconect import __version__, chart, contract, health, identity, insight, serve, storage
from disconect import export as export_module
from disconect.ingest import sources
from disconect.relay import config as relay_config
from disconect.relay import sync as sync_module
from disconect.relay.folder import FolderRelay
from disconect.storage import backup as backup_module
from disconect.storage import home, migrate_home
from disconect.storage import encrypt as encrypt_module
from disconect.storage import keys, sqlite

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_CONFIGURED = 3
EXIT_BUSY = 4
EXIT_DATABASE = 6
EXIT_SCHEMA = 7
EXIT_BACKUP = 8
EXIT_LOCKED = 9


def _emit(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
    else:
        print(text)


def _import_summary(stats) -> str:
    lines = [f"{stats.status()}: {stats.files_imported} imported, {stats.files_duplicate} duplicate, "
             f"{stats.files_failed} failed, {stats.records_written} rows written"
             f"{f', {stats.ignored} files ignored' if stats.ignored else ''}"]
    for stream, counts in sorted(stats.streams.items()):
        lines.append(f"  {stream:18s} {counts.get('files', 0):5d} files  {counts.get('records', 0):8d} rows")
    if stats.derived_days:
        lines.append(f"  derived daily steps/distance for {stats.derived_days} day(s)")
    if stats.dropped:
        dropped = ", ".join(f"{k}={v}" for k, v in sorted(stats.dropped.items()))
        lines.append(f"  dropped (never stored as numbers): {dropped}")
    if stats.dates_assumed_utc:
        lines.append(f"  WARNING: {stats.dates_assumed_utc} daily values dated in UTC -- no watch clock offset known")
    for failure in stats.failures[:10]:
        lines.append(f"  FAILED {failure['file']}: [{failure['kind']}] {failure['error']}")
    if len(stats.failures) > 10:
        lines.append(f"  ... and {len(stats.failures) - 10} more failures")
    return "\n".join(lines)


def cmd_import(args: argparse.Namespace) -> int:
    db_path = pathlib.Path(args.db)
    try:
        with storage.open_for_write(db_path, purpose="import") as conn:
            stats = sources.import_path(pathlib.Path(args.path), conn, transport=args.transport)
    except storage.WriteLockBusy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except storage.SchemaTooNew as exc:
        print(f"schema: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except FileNotFoundError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    payload = dataclasses.asdict(stats)
    payload["status"] = stats.status()
    _emit(payload, args.json, _import_summary(stats))
    return EXIT_OK if stats.status() == "ok" else EXIT_FAILED


def _relay_for(args: argparse.Namespace) -> FolderRelay:
    """The relay folder from --relay or ``relay.json`` in the data folder ({"folder": path}); no secrets live there.
    A LAN relay (``http://host:port``, ``{"lan": url}``) is the Rust core's: this core refuses it."""
    config = home.relay_config_path()
    given = getattr(args, "relay", None)
    if given:
        kind, value = ("lan" if relay_config.is_lan_address(given) else "folder"), given
    else:
        found = relay_config.read(config) if config.exists() else None
        kind, value = found if found else ("folder", None)
    if not value:
        raise FileNotFoundError(f"no relay folder: pass --relay <folder> or write {{\"folder\": ...}} to {config}")
    if given and getattr(args, "remember", False):
        home.ensure_parent_dir(config)
        config.write_text(json.dumps({kind: str(value)}) + "\n")
    return relay_config.open_relay(kind, value)


def cmd_sync(args: argparse.Namespace) -> int:
    """push | pull | status against the blind relay (docs/relay-protocol.md). Needs an encrypted store:
    the relay key and account derive from the master key, so a plaintext store has nothing to sync with."""
    db_path = pathlib.Path(args.db)
    try:
        if args.action == "forget":
            # after a rotation on another device, or a re-pair with new words: start the relay bookkeeping over
            with storage.open_for_write(db_path, purpose="sync") as conn:
                sync_module.forget_relay_state(conn)
            _emit({"forgotten": True}, args.json, "relay bookkeeping cleared: the next 'sync push' publishes everything again")
            return EXIT_OK
        if args.action == "status":
            with storage.open_for_write(db_path, purpose="sync") as conn:
                report = sync_module.status(conn)
            bundles = report["bundles"]
            text = (f"relay: pushed {bundles.get('pushed_applied', 0)}, pulled {bundles.get('pulled_applied', 0)}, "
                    f"rejected {bundles.get('pulled_rejected', 0)}; records unsent {report['records_unsent']}, "
                    f"seen {report['records_seen']}; conflicts {report['conflicts']}; gaps {len(report['gaps'])}; "
                    f"last push {report['last_pushed_at'] or 'never'}, last pull {report['last_pulled_at'] or 'never'}")
            _emit(report, args.json, text)
            return EXIT_OK
        master = storage.master_key_for(db_path, allow_prompt=True)
        if master is None:
            print(f"the relay needs an encrypted store: run '{identity.COMMAND} key init' first", file=sys.stderr)
            return EXIT_LOCKED
        relay = _relay_for(args)
        with storage.open_for_write(db_path, purpose="sync") as conn:
            if args.action == "push":
                result = sync_module.push(conn, master, relay)
                _emit(result.as_dict(), args.json,
                      f"pushed {len(result.bundles)} bundle(s): {result.records} record(s), {result.ranges} range(s)")
                return EXIT_OK
            result = sync_module.pull(conn, master, relay)
            _emit(result.as_dict(), args.json,
                  f"pulled {len(result.applied)} bundle(s): {result.records_new} new, {result.records_duplicate} duplicate, "
                  f"{result.records_invalid} invalid, {result.conflicts} conflict(s), {result.ranges_new} new range(s); "
                  f"rejected {len(result.rejected)}; gaps {len(result.gaps)}; repaired {result.records_repaired}; "
                  f"kept for a later decoder {result.records_kept}")
            return EXIT_OK if result.status == "ok" else EXIT_FAILED
    except storage.WriteLockBusy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except (FileNotFoundError, relay_config.UnsupportedTransport) as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (sqlite.Error, sync_module.RecordWriteFailed) as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE


def cmd_reparse(args: argparse.Namespace) -> int:
    db_path = pathlib.Path(args.db)
    try:
        with storage.open_for_write(db_path, purpose="reparse") as conn:
            stats = sources.reparse_all(conn, streams=args.stream, force=args.force)
    except storage.WriteLockBusy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except storage.SchemaTooNew as exc:
        print(f"schema: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    payload = dataclasses.asdict(stats)
    payload["status"] = stats.status()
    _emit(payload, args.json, _import_summary(stats))
    return EXIT_OK if stats.status() == "ok" else EXIT_FAILED


def cmd_status(args: argparse.Namespace) -> int:
    try:
        conn = storage.open_read_only(pathlib.Path(args.db))
    except storage.NotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except storage.SchemaTooNew as exc:
        print(f"schema: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    try:
        report = health.data_health(conn, args.days)
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    _emit(report, args.json, health.summarize_for_humans(report))
    return EXIT_OK


def cmd_contract(args: argparse.Namespace) -> int:
    data = contract.as_dict()
    lines = [f"contract v{data['contract_version']}", "", "time: " + data["time"], "",
             "missing values: " + data["missing_values"], "", "sources: " + data["sources"], "",
             "privacy: " + data["privacy"], "", "metrics:"]
    lines += [f"  {m['metric']:32s} {m['unit']:12s} {m['cadence']:7s} {m['description']}"
              for m in data["metrics"]]
    lines.append("labels:")
    lines += [f"  {m['metric']:32s} {m['unit']:12s} {m['cadence']:7s} {m['description']}"
              for m in data["labels"]]
    _emit(data, args.json, "\n".join(lines))
    return EXIT_OK


def _open_reader(args: argparse.Namespace):
    """A read-only connection or an exit code (int) explaining why not."""
    try:
        return storage.open_read_only(pathlib.Path(args.db))
    except storage.NotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except storage.SchemaTooNew as exc:
        print(f"schema: {exc}", file=sys.stderr)
        return EXIT_SCHEMA


def _facts_text(report: dict) -> str:
    if report.get("as_of") is None:
        return report.get("reason", "no facts")
    lines = [f"as of {report['as_of']}: last {report['window']['days']} day(s) "
             f"({report['window']['from']}..{report['window']['to']}) vs the {report['baseline']['days']} "
             f"day(s) before ({report['baseline']['from']}..{report['baseline']['to']})"]
    for fact in report["facts"]:
        head = f"  {fact['metric']:32s} [{fact['source_scope']:12s}] "
        if fact["value"] is None:
            lines.append(head + f"-- ({fact['reason_code']})")
            continue
        text = f"{fact['value']:>9} {fact['unit']:12s} "
        comparison = fact["comparison"]
        if comparison:
            z = f", z {comparison['z_score']:+.2f}" if comparison["z_score"] is not None else ""
            text += (f"{comparison['direction']:9s} baseline {comparison['baseline_mean']} "
                     f"(delta {comparison['delta']:+}{z})  {fact['confidence']}")
        else:
            text += f"{fact['confidence']} ({fact['reason_code']})"
        lines.append(head + text)
    if report.get("ignored_metrics"):
        lines.append(f"  ignored (not in the contract): {', '.join(report['ignored_metrics'])}")
    return "\n".join(lines)


def cmd_facts(args: argparse.Namespace) -> int:
    conn = _open_reader(args)
    if isinstance(conn, int):
        return conn
    try:
        report = insight.period_facts(conn, args.days, args.baseline, args.end, args.metric or None,
                                      args.scope, include_points=args.points)
    except ValueError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    _emit(report, args.json, _facts_text(report))
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """CSV always goes to stdout; --json is not meaningful here and is ignored."""
    conn = _open_reader(args)
    if isinstance(conn, int):
        return conn
    try:
        filters = {"metrics": args.metric or None, "start": args.start, "end": args.end,
                   "source_scope": args.scope}
        if args.format == "samples":
            if not args.metric or len(args.metric) != 1:
                print("usage: --format samples needs exactly one --metric", file=sys.stderr)
                return EXIT_USAGE
            text = export_module.samples_csv(conn, args.metric[0], args.start, args.end)
        elif args.format == "long":
            text = export_module.daily_long_csv(conn, **filters)
        else:
            text = export_module.daily_wide_csv(conn, **filters)
    except ValueError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    sys.stdout.write(text)
    return EXIT_OK


def cmd_chart(args: argparse.Namespace) -> int:
    """A PNG of what the read side already answers with; '-' writes the image to stdout.

    Charts are drawn from the same queries the MCP tools use, so a picture can
    never show something the contract would not report.
    """
    conn = _open_reader(args)
    if isinstance(conn, int):
        return conn
    try:
        if args.kind == "sleep":
            canvas = chart.sleep_chart(conn, args.date, args.width, args.height)
        elif not args.metric:
            print(f"usage: chart {args.kind} needs a metric name", file=sys.stderr)
            return EXIT_USAGE
        elif args.kind == "samples":
            canvas = chart.samples_chart(conn, args.metric, args.date, args.scope,
                                         args.width, args.height)
        else:
            canvas = chart.metric_chart(conn, args.metric, args.days, args.end, args.scope,
                                        args.rolling, args.width, args.height)
    except ValueError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    png = canvas.to_png()
    summary = f"{canvas.width}x{canvas.height}, {len(png):,} bytes"
    if args.out == "-":
        # The image owns stdout here, so its one line of prose goes to stderr.
        sys.stdout.buffer.write(png)
        print(f"{args.kind} chart ({summary})", file=sys.stderr)
        return EXIT_OK
    try:
        pathlib.Path(args.out).expanduser().write_bytes(png)
    except OSError as exc:
        print(f"cannot write {args.out}: {exc.strerror}", file=sys.stderr)
        return EXIT_FAILED
    _emit({"chart": args.kind, "metric": args.metric, "path": args.out, "bytes": len(png),
           "width": canvas.width, "height": canvas.height},
          args.json, f"wrote {args.out} ({summary})")
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    try:
        manifest = backup_module.create_backup(pathlib.Path(args.db), args.to, args.note)
    except backup_module.BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return EXIT_BACKUP
    except sqlite.Error as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    _emit(manifest, args.json, f"snapshot {manifest['file']} ({manifest['bytes']:,} bytes, "
                               f"schema v{manifest['schema_version']}, sha256 {manifest['sha256'][:12]}...)")
    return EXIT_OK


def cmd_backups(args: argparse.Namespace) -> int:
    folder = pathlib.Path(args.dir) if args.dir else backup_module.default_backup_dir(pathlib.Path(args.db))
    listed = backup_module.list_backups(folder)
    lines = [f"  {m.get('file')}  {m.get('created_at', '?')}  {m.get('bytes', 0):,} bytes  "
             f"schema v{m.get('schema_version', '?')}" + ("  (no manifest)" if m.get("manifest") == "missing" else "")
             for m in listed] or ["  none"]
    _emit({"dir": str(folder.name), "backups": listed}, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print("restore replaces the current database (a rollback copy is kept); re-run with --yes",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        result = backup_module.restore_backup(pathlib.Path(args.snapshot), pathlib.Path(args.db))
    except backup_module.BackupError as exc:
        print(f"restore: {exc}", file=sys.stderr)
        return EXIT_BACKUP
    except storage.WriteLockBusy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return EXIT_BUSY
    _emit(result, args.json, f"restored from {result['restored_from']}; previous database kept as "
                             f"{result['rollback_copy']}")
    return EXIT_OK



# ---- encryption ----

def _tty() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _ask_passphrase(prompt: str, *, confirm: bool) -> str:
    """A passphrase from the test/CI env var or the terminal. Never from argv."""
    from_env = os.environ.get(keys.PASSPHRASE_ENV)
    if from_env:
        return keys._env_passphrase() or ""
    if not _tty():
        raise keys.Locked(f"a passphrase is needed; run this in a terminal (or set {keys.PASSPHRASE_ENV} in tests)")
    first = getpass.getpass(prompt, stream=sys.stderr)
    if confirm and getpass.getpass("Repeat it: ", stream=sys.stderr) != first:
        raise keys.WeakPassphrase("the two entries differ")
    return first


def _can_show_words() -> bool:
    return sys.stdout.isatty() and _tty()


def _show_words_once(master: bytes) -> bool:
    """Print the recovery phrase only to a terminal and have two words typed back. Returns True if shown."""
    if not _can_show_words():
        print("recovery phrase NOT shown: stdout is not a terminal. Run 'disconect key rotate-recovery' "
              "in a terminal to get one (the current master key has no written phrase).", file=sys.stderr)
        return False
    words = keys.words_for(master).split()
    print("\nWrite these 24 words down, in order. They ARE the key: anyone with them can read the data, "
          "and without them (and your passphrase) the data is gone.\n")
    for row in range(0, 24, 6):
        print("   " + "  ".join(f"{index + 1:2d}.{word:<10s}" for index, word in enumerate(words[row:row + 6], row)))
    import secrets
    for _ in range(2):
        position = secrets.randbelow(24)
        if input(f"\nType word #{position + 1}: ").strip().lower() != words[position]:
            print("that is not the word; check your notes and run 'key rotate-recovery' for a fresh phrase",
                  file=sys.stderr)
    print("\033[2J\033[3J\033[H", end="")  # clear screen and scrollback
    return True


def _master_or_exit(args: argparse.Namespace) -> bytes:
    """The unlocked master key, or a KeyFileMissing that main() turns into exit code 9."""
    master = storage.master_key_for(pathlib.Path(args.db), allow_prompt=True)
    if master is None:
        key_path = keys.key_path_for(pathlib.Path(args.db))
        raise keys.KeyFileMissing(f"no key file at {key_path.name}; run 'disconect key init'")
    return master


def cmd_key_init(args: argparse.Namespace) -> int:
    db_path = pathlib.Path(args.db)
    key_path = keys.key_path_for(db_path)
    if args.generate:
        passphrase = keys.generate_passphrase()
        if not sys.stdout.isatty():
            print("--generate needs a terminal to show the passphrase", file=sys.stderr)
            return EXIT_USAGE
        print(f"Your generated passphrase (write it down too):  {passphrase}\n")
    else:
        passphrase = _ask_passphrase(f"New passphrase (at least {keys.MIN_PASSPHRASE_CHARS} characters): ", confirm=True)
    master = keys.create(key_path, passphrase)
    storage.remember(db_path, master)
    shown = _show_words_once(master)
    encrypted_now = False
    if db_path.exists() and storage.is_encrypted_file(db_path) is False and not args.no_encrypt:
        encrypt_module.encrypt_store(db_path, master)
        encrypted_now = True
    _emit({"key_file": key_path.name, "words_shown": shown, "encrypted": encrypted_now},
          args.json, f"key file written: {key_path.name}" + (" — database encrypted" if encrypted_now else
                                                              "; run 'disconect encrypt' to convert the database")
          + "\nLosing the passphrase AND the recovery phrase means the data is gone. "
            "Run 'disconect key cache' once so daily use needs no passphrase.")
    return EXIT_OK


def cmd_encrypt(args: argparse.Namespace) -> int:
    db_path = pathlib.Path(args.db)
    if args.purge_plaintext:
        result = encrypt_module.purge_plaintext(db_path)
        _emit(result, args.json, f"removed {len(result['removed'])} plaintext file(s): {', '.join(result['removed']) or 'none'}"
                                 f"\nnote: {result['warning']}")
        return EXIT_OK
    master = _master_or_exit(args)
    print("make sure disconect-mcp / Claude Desktop is not running against this database", file=sys.stderr)
    result = encrypt_module.encrypt_store(db_path, master)
    storage.forget(db_path)
    _emit(result, args.json, f"encrypted {result['database']} and {len(result['snapshots'])} snapshot(s). "
                             f"Plaintext kept for rollback: {', '.join(result['plaintext_left'])}. "
                             f"When satisfied run 'disconect encrypt --purge-plaintext'.\nnote: {result['warning']}")
    return EXIT_OK


def cmd_key_change_passphrase(args: argparse.Namespace) -> int:
    master = _master_or_exit(args)
    new = _ask_passphrase("New passphrase: ", confirm=True)
    keys.rewrap(keys.key_path_for(pathlib.Path(args.db)), master, new)
    _emit({"changed": True}, args.json, "passphrase changed (the database key is unchanged; earlier copies of the "
                                        "key file still open with the old passphrase — rotate the recovery phrase to revoke them)")
    return EXIT_OK


def cmd_key_recover(args: argparse.Namespace) -> int:
    """Recovery phrase -> a fresh key file with a new passphrase (the database key is unchanged)."""
    words = keys.env_recovery_words()
    if words is None:
        if not _tty():
            print("recovery needs a terminal", file=sys.stderr)
            return EXIT_LOCKED
        words = getpass.getpass("Recovery phrase (24 words): ", stream=sys.stderr)
    master = keys.master_from_words(words)
    db_path = pathlib.Path(args.db)
    if db_path.exists() and storage.is_encrypted_file(db_path):
        probe = storage.connect(db_path, read_only=True, master=master)
        try:
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except storage.DatabaseError:
            print("that phrase does not open this database", file=sys.stderr)
            return EXIT_LOCKED
        finally:
            probe.close()
    key_path = keys.key_path_for(db_path)
    passphrase = _ask_passphrase("New passphrase: ", confirm=True)
    keys.check_passphrase_strength(passphrase)
    replaced = key_path.with_name(key_path.name + ".replaced")
    if key_path.exists():
        key_path.rename(replaced)
    keys.write_key_file(key_path, keys._document(master, passphrase))
    replaced.unlink(missing_ok=True)   # the old wrap may be what was compromised; do not keep it
    storage.remember(db_path, master)
    _emit({"key_file": key_path.name}, args.json, f"key file rewritten: {key_path.name}")
    return EXIT_OK


def cmd_key_rotate_recovery(args: argparse.Namespace) -> int:
    """New master key: the database and snapshots are re-encrypted, a new phrase is shown, old copies die."""
    db_path = pathlib.Path(args.db)
    if not _can_show_words():
        print("rotate-recovery must show the new phrase: run it in a terminal", file=sys.stderr)
        return EXIT_USAGE
    old_master = _master_or_exit(args)
    passphrase = _ask_passphrase("Current passphrase (kept): ", confirm=False)
    key_path = keys.key_path_for(db_path)
    keys.unlock_with_passphrase(keys.read_key_file(key_path), passphrase)
    import secrets
    new_master = secrets.token_bytes(32)
    document = keys._document(new_master, passphrase)
    # Relay bookkeeping first, under the OLD key: the account and bundle key derive from the master,
    # so everything must be pushed again under the new one (ADR 0005). Idempotent — if the rekey
    # below fails, the next push merely repeats bundles under the old account. Never after the
    # rekey: nothing may stand between a successful rekey and showing the new words.
    relay_cleared = False
    if db_path.exists():
        try:
            with storage.open_for_write(db_path, purpose="sync") as conn:
                sync_module.forget_relay_state(conn)
            relay_cleared = True
        except (storage.WriteLockBusy, storage.DatabaseError, storage.NotEncrypted, sqlite.Error) as exc:
            print(f"relay bookkeeping not cleared ({type(exc).__name__}); run 'disconect sync forget' later", file=sys.stderr)
    if db_path.exists() and storage.is_encrypted_file(db_path):
        result = encrypt_module.rekey_store(db_path, old_master, new_master, document)
    else:
        keys.write_key_file(key_path, document)
        result = {"database": None, "snapshots": [], "copies": []}
    storage.remember(db_path, new_master)
    shown = _show_words_once(new_master)
    keychain_note = keys.keychain_after_rotate(old_master, new_master)  # after the words: a keychain failure is a note
    if keychain_note:
        print(keychain_note, file=sys.stderr)
    _emit({"rotated": True, "words_shown": shown, "relay_cleared": relay_cleared, **result}, args.json,
          f"master key rotated: database, {len(result['snapshots'])} snapshot(s) and {len(result['copies'])} "
          "pre-restore copy(ies) re-encrypted; earlier key-file copies and the old phrase no longer open them. "
          "Snapshots written elsewhere with 'backup --to' stay on the old key. Relay bookkeeping cleared: run "
          "'disconect sync push' to re-publish under the new key and delete the old relay prefix.")
    return EXIT_OK


def cmd_key_cache(args: argparse.Namespace) -> int:
    master = _master_or_exit(args)
    key_id = keys.key_id_for(master)
    if args.remove:
        removed = keys.keychain_delete(key_id)
        removed = keys.keychain_delete_legacy(key_id) or removed
        _emit({"cached": False, "removed": removed}, args.json, "keychain item removed" if removed else "nothing was cached")
        return EXIT_OK
    keys.keychain_set(key_id, master)
    _emit({"cached": True}, args.json, "master key cached in the login keychain (any program you run as you can "
                                       "read it; 'disconect key cache --remove' undoes this)")
    return EXIT_OK


def cmd_key_status(args: argparse.Namespace) -> int:
    status = keys.status_for(pathlib.Path(args.db))
    lines = [f"key file: {'present' if status['key_file'] else 'none'}",
             f"database: {'encrypted' if status['database_encrypted'] else 'plaintext' if status['database_encrypted'] is False else 'absent/empty'}",
             f"unlock paths: env={'yes' if status['env_passphrase_set'] else 'no'} keychain={'yes' if status['keychain'] else 'no'} "
             f"terminal={'yes' if _tty() else 'no'}"]
    if status["kdf"]:
        lines.append(f"kdf: argon2id m={status['kdf']['m_kib'] // 1024} MiB t={status['kdf']['t']} p={status['kdf']['p']}")
    _emit(status, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_migrate_home(args: argparse.Namespace) -> int:
    try:
        report = migrate_home.migrate_home()
    except migrate_home.MigrateRefused as exc:
        print(f"migrate-home: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (storage.Encrypted, storage.NotEncrypted, keys.KeyError_) as exc:
        print(f"locked: {exc}; nothing was moved", file=sys.stderr)
        return EXIT_LOCKED
    lines = [report["message"]]
    if report["moved"]:
        lines += [f"before {report['from']}:", *(f"  {name}" for name in report["before"]),
                  f"after {report['to']}:", *(f"  {name}" for name in report["after"])]
    _emit(report, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    return serve.main(["--db", args.db])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=identity.COMMAND,
        description="Local-first, cloud-free store for Garmin watch health data.",
        epilog=("exit codes: 0 ok, 1 failed, 2 usage, 3 not configured, 4 busy, 6 database, "
                "7 schema newer than this build, 8 backup/restore refused, 9 locked/key refused. " + contract.PRIVACY_NOTE))
    parser.add_argument("--version", action="version", version=f"{identity.COMMAND} {__version__}")
    parser.add_argument("--db", default=str(storage.default_db_path()),
                        help=f"SQLite file (default ${storage.DEFAULT_DB_ENV} or ~/{identity.DATA_DIR}/{identity.DB_FILENAME}; "
                             f"the legacy ~/{identity.LEGACY_HOMES[0]} is read until {identity.COMMAND} migrate-home)")
    parser.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    commands = parser.add_subparsers(dest="command", required=True)

    imp = commands.add_parser("import", help="import a Connect export (zip/folder), a FIT folder, a .fit file, or live-link session files (live-*.jsonl)")
    imp.add_argument("path")
    imp.add_argument("--transport", choices=["connect_export", "usb", "gadgetbridge", "ciq", "drop", "ble"],
                     help="how the files reached this machine (auto-detected for exports)")
    imp.set_defaults(func=cmd_import)

    rep = commands.add_parser(
        "reparse", help="replay decoding for retained raw records, without re-pulling from the watch")
    rep.add_argument("--stream", action="append",
                     help="limit to this raw_records.stream value (repeatable; default: every stream)")
    rep.add_argument("--force", action="store_true",
                     help="rebuild even if some records fail to decode (their rows are lost)")
    rep.set_defaults(func=cmd_reparse)

    status = commands.add_parser("status", help="data health: streams, coverage, failures, recent imports")
    status.add_argument("--days", type=int, default=30, help="coverage window ending today (default 30)")
    status.set_defaults(func=cmd_status)

    facts = commands.add_parser("facts", help="recent window vs your own baseline, as facts with evidence")
    facts.add_argument("metric", nargs="*", help="contract metric names (default: every metric with data)")
    facts.add_argument("--days", type=int, default=insight.DEFAULT_WINDOW_DAYS, help="window length (1-31)")
    facts.add_argument("--baseline", type=int, default=insight.DEFAULT_BASELINE_DAYS,
                       help="baseline length before the window (1-365)")
    facts.add_argument("--end", help="window end YYYY-MM-DD (default: latest stored date)")
    facts.add_argument("--scope", choices=list(contract.SOURCE_SCOPES), help="one source scope only")
    facts.add_argument("--points", action="store_true", help="include the window's per-day values")
    facts.set_defaults(func=cmd_facts)

    exp = commands.add_parser("export", help="CSV of daily values (wide/long) or raw samples, to stdout")
    exp.add_argument("--format", choices=["wide", "long", "samples"], default="wide")
    exp.add_argument("--metric", action="append", help="limit to this metric (repeatable)")
    exp.add_argument("--start", help="first date (YYYY-MM-DD) or, for samples, first UTC timestamp")
    exp.add_argument("--end", help="last date (YYYY-MM-DD) or, for samples, exclusive UTC timestamp")
    exp.add_argument("--scope", choices=list(contract.SOURCE_SCOPES))
    exp.set_defaults(func=cmd_export)

    cha = commands.add_parser("chart", help="draw a PNG: a metric over time, one night's sleep, or one day's samples")
    cha.add_argument("kind", choices=["metric", "sleep", "samples"])
    cha.add_argument("metric", nargs="?", help="contract metric name (needed by metric and samples)")
    cha.add_argument("--out", "-o", required=True, help="PNG path, or - to write the image to stdout")
    cha.add_argument("--days", type=int, default=90, help="window length for kind=metric (default 90)")
    cha.add_argument("--end", help="last day of the window (YYYY-MM-DD, default today)")
    cha.add_argument("--date", help="the day or night to draw (YYYY-MM-DD, default the latest stored)")
    cha.add_argument("--scope", choices=list(contract.SOURCE_SCOPES), help="one source scope only")
    cha.add_argument("--rolling", type=int, default=7, help="trailing-mean window in days; 1 drops the overlay")
    cha.add_argument("--width", type=int, default=1000)
    cha.add_argument("--height", type=int, default=460)
    cha.set_defaults(func=cmd_chart)

    bak = commands.add_parser("backup", help="verified snapshot of the database with a manifest")
    bak.add_argument("--to", help="folder for snapshots (default: <db folder>/backups)")
    bak.add_argument("--note", help="free text stored in the manifest")
    bak.set_defaults(func=cmd_backup)

    baks = commands.add_parser("backups", help="list snapshots")
    baks.add_argument("--dir", help="folder to list (default: <db folder>/backups)")
    baks.set_defaults(func=cmd_backups)

    res = commands.add_parser("restore", help="replace the database with a verified snapshot")
    res.add_argument("snapshot", help="path to a disconect-*.db snapshot (older hearthbeat-*.db ones work too)")
    res.add_argument("--yes", action="store_true", help="confirm; the current database is kept as a rollback copy")
    res.set_defaults(func=cmd_restore)

    con = commands.add_parser("contract", help="print the read contract every outlet follows")
    con.set_defaults(func=cmd_contract)

    enc = commands.add_parser("encrypt", help="convert the database (and its snapshots) to SQLCipher under the key file")
    enc.add_argument("--purge-plaintext", action="store_true", help="delete rollback and plaintext snapshot copies")
    enc.set_defaults(func=cmd_encrypt)

    syn = commands.add_parser("sync", help="push/pull encrypted record bundles through a blind relay folder (docs/relay-protocol.md)")
    syn.add_argument("action", choices=["push", "pull", "status", "forget"])
    syn.add_argument("--relay", help="relay folder (a WebDAV/rsync/Syncthing-carried path); default from relay.json in the data folder")
    syn.add_argument("--remember", action="store_true", help="save --relay to relay.json in the data folder")
    syn.set_defaults(func=cmd_sync)

    mig = commands.add_parser(
        "migrate-home", help=f"move the old data folder (~/{identity.LEGACY_HOMES[0]}) to ~/{identity.DATA_DIR}; "
                             "quit the app and Claude Desktop first")
    mig.set_defaults(func=cmd_migrate_home)

    srv = commands.add_parser("serve", help="JSON Lines sidecar on stdio for the desktop app (see docs/serve-protocol.md)")
    srv.set_defaults(func=cmd_serve)

    key = commands.add_parser("key", help="key file: init, change-passphrase, recover, rotate-recovery, cache, status")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    init = key_commands.add_parser("init", help="create the key file (shows the recovery phrase once) and encrypt")
    init.add_argument("--generate", action="store_true", help="generate a 6-word passphrase instead of prompting")
    init.add_argument("--no-encrypt", action="store_true", help="write the key file only; convert later with 'encrypt'")
    init.set_defaults(func=cmd_key_init)
    key_commands.add_parser("change-passphrase", help="new passphrase, same database key").set_defaults(func=cmd_key_change_passphrase)
    key_commands.add_parser("recover", help="rebuild the key file from the 24-word phrase").set_defaults(func=cmd_key_recover)
    key_commands.add_parser("rotate-recovery", help="new master key + phrase; re-encrypts; revokes old copies").set_defaults(func=cmd_key_rotate_recovery)
    cache = key_commands.add_parser("cache", help="keep the master key in the login keychain for daily use")
    cache.add_argument("--remove", action="store_true")
    cache.set_defaults(func=cmd_key_cache)
    key_commands.add_parser("status", help="which unlock paths exist (never key material)").set_defaults(func=cmd_key_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "migrate-home":
        home.announce_default_resolution(args.db)
        encrypt_module.cleanup_stray(pathlib.Path(args.db))
    try:
        return int(args.func(args))
    except storage.HomeMoved as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except (storage.Encrypted, storage.NotEncrypted, keys.KeyError_) as exc:
        print(f"locked: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except encrypt_module.EncryptError as exc:
        print(f"encrypt: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except storage.DatabaseError as exc:
        print(f"database: {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except Exception as exc:  # noqa: BLE001 - only a keychain backend failure is mapped; anything else propagates
        if not keys.is_keychain_error(exc):
            raise
        print(f"keychain: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
