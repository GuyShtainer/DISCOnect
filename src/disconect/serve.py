"""``disconect serve``: the sidecar the desktop app talks to, JSON Lines over stdio.

The protocol is specified in ``docs/serve-protocol.md``; this module is its only implementation.
One long-lived process per app. It is the only process that ever holds the database key, and it
never learns a passphrase it keeps: ``key.unlock`` derives the master key, registers it for this
process and lets the passphrase go.

Boundaries, drawn hard:

* **The protocol owns one private descriptor.** At start the real stdout is duplicated to a
  private fd and fd 1 is pointed at stderr, so a stray ``print`` or a C-level write cannot
  corrupt the stream.
* **No network, no port.** Stdio only.
* **Never primes.** Nothing is unlocked at start; until ``key.unlock`` succeeds every ``data.*``
  and ``import.*`` call answers ``locked``. A store with no key file is plaintext and simply open.
* **Nothing sensitive is logged.** No request parameters and no results are ever written to a
  log; error text goes through :func:`disconect.redact.redact_text`; an unexpected failure
  reports only its exception type.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import json
import os
import pathlib
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TextIO

from disconect import __version__, contract, coverage, health, identity, insight, queries, storage
from disconect.ingest import sources
from disconect.relay import config as relay_config
from disconect.relay import sync as sync_module
from disconect.redact import redact_text
from disconect.storage import home, keys, migrations, sqlite

Id = int | str | None

DEFAULT_HEALTH_DAYS = 90
DEFAULT_METRIC_DAYS = 90
LAST_IMPORTS = 5
#: Protocol transport names -> the names the store records.
TRANSPORTS = {"export": sources.TRANSPORT_CONNECT_EXPORT, "usb": "usb"}
_DEFERRED = object()


class ServeError(Exception):
    """A failure that already knows its protocol error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Channel:
    """The protocol's output: one JSON object per line on a private stream, safe across threads."""

    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        """Write one already-encoded protocol line and flush it."""
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def event(self, payload: dict) -> None:
        """Write an unsolicited event line."""
        self.write(encode(payload))


def encode(payload: dict) -> str:
    """One protocol line (no newline). ASCII-only JSON, so no value can introduce a line break."""
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)


class Session:
    """Everything one serve process holds: which store it serves, where output goes, the import slot."""

    def __init__(self, db_path: pathlib.Path, channel: Channel):
        self.db_path = pathlib.Path(db_path)
        self.channel = channel
        self.import_slot = threading.Lock()
        self.import_thread: threading.Thread | None = None

    def require_unlocked(self) -> None:
        """Raise ``locked`` while an encrypted store has not been unlocked in this process."""
        if not storage.is_unlocked(self.db_path):
            raise ServeError("locked", "the store is locked; unlock it first")

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite.Connection]:
        """A read-only connection for one call. Never prompts; the store must already be unlocked."""
        self.require_unlocked()
        conn = storage.open_read_only(self.db_path, allow_prompt=False)
        try:
            yield conn
        finally:
            conn.close()

    def key_file_exists(self) -> bool:
        return keys.key_path_for(self.db_path).exists()


class Call:
    """One parsed request. ``params`` is a private dict the handler may consume."""

    def __init__(self, request_id: Id, method: str, params: dict):
        self.id = request_id
        self.method = method
        self.params = params


Handler = Callable[[Session, Call], Any]


# ---- errors ----

def _error_for(exc: Exception) -> tuple[str, str]:
    """Map an exception to ``(code, message)``. Unexpected failures report only their type."""
    if isinstance(exc, ServeError):
        return exc.code, str(exc)
    for kind, code in _ERROR_CODES:
        if isinstance(exc, kind):
            return code, str(exc)
    return "internal", f"unexpected {type(exc).__name__}"


#: First match wins, so subclasses come before their bases.
_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (keys.WrongPassphrase, "wrong_passphrase"),
    (relay_config.UnsupportedTransport, "unsupported_transport"),
    (keys.WeakPassphrase, "weak_passphrase"),
    (keys.KeyFileMissing, "not_encrypted"),
    (keys.Locked, "locked"),
    (keys.KeyError_, "database"),
    (storage.Encrypted, "locked"),
    (storage.NotEncrypted, "not_encrypted"),
    (storage.NotConfigured, "not_found"),
    (storage.SchemaTooNew, "database"),
    (storage.WriteLockBusy, "busy"),
    (FileNotFoundError, "not_found"),
    (ValueError, "bad_params"),
    (storage.DatabaseError, "database"),
)


def error_payload(exc: Exception) -> dict:
    """The protocol ``error`` object for ``exc``, its message redacted."""
    code, message = _error_for(exc)
    return {"code": code, "message": redact_text(message)}


def _error_line(request_id: Id, exc: Exception) -> str:
    return encode({"id": request_id, "error": error_payload(exc)})


def _line_for(request_id: Id, produce: Callable[[], Any]) -> str:
    """Encode ``produce()`` as a result line, or any failure (including an unencodable result) as an error line."""
    try:
        return encode({"id": request_id, "result": produce()})
    except Exception as exc:  # noqa: BLE001 - the process must outlive any one request; mapped to a code
        return _error_line(request_id, exc)


# ---- parameter helpers ----

def _text_param(params: dict, name: str, *, required: bool = True) -> str | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ServeError("bad_params", f"{name} must be a non-empty string")
    return value


def _int_param(params: dict, name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServeError("bad_params", f"{name} must be an integer")
    return value


def _bool_param(params: dict, name: str) -> bool:
    value = params.get(name)
    if not isinstance(value, bool):
        raise ServeError("bad_params", f"{name} must be true or false")
    return value


def _unlocked_only(handler: Handler) -> Handler:
    """Make a handler answer ``locked`` first while the store is locked."""
    @functools.wraps(handler)
    def guarded(session: Session, call: Call) -> Any:
        session.require_unlocked()
        return handler(session, call)
    return guarded


# ---- app / key ----

def app_info(session: Session, call: Call) -> dict:
    """Product, versions and which store this process serves."""
    on_disk = storage.is_encrypted_file(session.db_path)
    return {"product": identity.PRODUCT, "core": __version__, "schema": migrations.SCHEMA_VERSION,
            "contract": int(contract.CONTRACT_VERSION), "db": str(session.db_path),
            "encrypted": on_disk if on_disk is not None else session.key_file_exists(),
            "notice": identity.NOTICE}


def key_status(session: Session, call: Call) -> dict:
    """Which unlock paths exist and whether this process has unlocked the store."""
    status = keys.status_for(session.db_path)
    keychain = keys.KEYCHAIN_ABSENT
    if status["key_file"]:
        keychain = keys.keychain_state(keys.read_key_file(keys.key_path_for(session.db_path)).key_id)
    return {"key_file": status["key_file"], "encrypted": bool(status["database_encrypted"]),
            "unlocked": storage.is_unlocked(session.db_path), "keychain": keychain,
            "kdf": status["kdf"]}


def _keychain_master(key_file: keys.KeyFile) -> bytes:
    """The master key from the keychain, or ``locked`` when it holds none that fits this key file."""
    cached = keys.keychain_get(key_file.key_id)
    if cached is None or keys.key_id_for(cached) != key_file.key_id:
        raise ServeError("locked", "no passphrase was given and the keychain holds no key for this store")
    return cached


def key_unlock(session: Session, call: Call) -> dict:
    """Unlock with ``passphrase`` (or the keychain when none is given). The passphrase is not kept."""
    passphrase = call.params.pop("passphrase", None)
    try:
        if passphrase is not None and not isinstance(passphrase, str):
            raise ServeError("bad_params", "passphrase must be a string")
        key_file = keys.read_key_file(keys.key_path_for(session.db_path))
        master = (_keychain_master(key_file) if passphrase is None
                  else keys.unlock_with_passphrase(key_file, passphrase))
    finally:
        del passphrase
    storage.remember(session.db_path, master)
    return {"unlocked": True}


def key_cache(session: Session, call: Call) -> dict:
    """Store or remove the master key in the OS keychain. Needs the store unlocked."""
    enable = _bool_param(call.params, "enable")
    session.require_unlocked()
    master = storage.unlocked_master(session.db_path)
    if master is None:
        raise ServeError("not_encrypted", "this store has no key to cache")
    key_id = keys.read_key_file(keys.key_path_for(session.db_path)).key_id
    if enable:
        keys.keychain_set(key_id, master)
    else:
        keys.keychain_delete(key_id)
    return {"keychain": keys.keychain_state(key_id)}


# ---- data ----

@_unlocked_only
def data_health(session: Session, call: Call) -> dict:
    """Coverage, provenance and recent imports; the core's convention texts are left out."""
    window_days = _int_param(call.params, "window_days", DEFAULT_HEALTH_DAYS)
    with session.reader() as conn:
        report = health.data_health(conn, window_days)
    report.pop("conventions", None)
    return identity.neutral(report)


@_unlocked_only
def data_metric(session: Session, call: Call) -> dict:
    """One metric and scope as a calendar-filled series: a missing day is null with its status."""
    metric = _text_param(call.params, "metric")
    scope = _text_param(call.params, "scope")
    days = _int_param(call.params, "days", DEFAULT_METRIC_DAYS)
    last_day = _text_param(call.params, "last_day", required=False)
    cap = queries.MAX_SAMPLE_DAYS if contract.cadence_for(metric) == contract.CADENCE_SAMPLE else queries.MAX_DAILY_DAYS
    with session.reader() as conn:
        # strict YYYY-MM-DD like the MCP paths (kb/23: the fromisoformat allowance is retired)
        last = queries.parse_day(last_day, "last_day") if last_day is not None else datetime.date.fromisoformat(queries.local_today(conn))
        first = last - datetime.timedelta(days=max(1, min(days, cap)) - 1)
        series = queries.metric_calendar(conn, metric, scope, first.isoformat(), last.isoformat())
    return {"metric": metric, "scope": scope, "unit": contract.unit_for(metric), "days": series}


def _contract_pairs() -> list[tuple[str, str]]:
    """Every (numeric metric, scope) the contract declares a stream for, in contract order."""
    declared = {key for key in contract.STREAMS_FOR if contract.cadence_for(key[0]) is not None}
    return [(item.metric, scope) for item in contract.METRICS for scope in contract.SOURCE_SCOPES
            if (item.metric, scope) in declared]


@_unlocked_only
def data_today(session: Session, call: Call) -> dict:
    """The latest value, with its day, for every metric and scope in the contract; ``day`` is null when none."""
    with session.reader() as conn:
        today = queries.local_today(conn)
        latest = {pair: queries.latest_value(conn, *pair) for pair in _contract_pairs()}
        statuses = coverage.statuses_on(conn, today) if None in latest.values() else {}
    rows = []
    for (metric, scope), found in latest.items():
        day, value = found if found else (None, None)  # no day: a missing value has no date
        status = coverage.PRESENT if found else statuses.get((metric, scope), coverage.NOT_COVERED)
        rows.append({"metric": metric, "scope": scope, "value": value, "unit": contract.unit_for(metric),
                     "day": day, "status": status})
    return {"day": today, "metrics": rows}


@_unlocked_only
def data_facts(session: Session, call: Call) -> dict:
    """The recent window against this person's own baseline, as facts with confidence."""
    days = _int_param(call.params, "days", insight.DEFAULT_WINDOW_DAYS)
    baseline_days = _int_param(call.params, "baseline_days", insight.DEFAULT_BASELINE_DAYS)
    with session.reader() as conn:
        facts = insight.period_facts(conn, days, baseline_days)
    facts.pop("sources", None)
    return identity.neutral(facts)


# ---- import ----

def _import_result(stats: Any, run_id: int | None) -> dict:
    return {"run_id": run_id, "files": stats.files_seen, "ok": stats.files_imported,
            "partial": stats.status() == "partial", "duplicate": stats.files_duplicate,
            "failed": stats.files_failed}


def _run_import(session: Session, path: pathlib.Path, transport: str | None) -> dict:
    """Import under the write lock (never waiting for it), streaming progress events."""
    def progress(done: int, total: int | None, note: str) -> None:
        with contextlib.suppress(OSError):  # the bridge is gone; EOF on stdin ends the process
            session.channel.event({"event": "progress", "op": "import", "done": done, "total": total,
                                   "note": redact_text(note)})
    with storage.open_for_write(session.db_path, purpose="import", timeout_s=0.0) as conn:
        stats = sources.import_path(path, conn, transport=transport, progress=progress)
        run_id = conn.execute("SELECT MAX(id) FROM import_runs").fetchone()[0]
    return _import_result(stats, run_id)


def _import_worker(session: Session, call: Call, path: pathlib.Path, transport: str | None) -> None:
    """The worker thread body: run the import, free the slot, then answer the request."""
    line = _line_for(call.id, lambda: _run_import(session, path, transport))
    session.import_slot.release()
    with contextlib.suppress(OSError):
        session.channel.write(line)


@_unlocked_only
def import_run(session: Session, call: Call) -> Any:
    """Start an import on the worker thread; the answer is sent when it finishes."""
    path = pathlib.Path(_text_param(call.params, "path")).expanduser()
    name = _text_param(call.params, "transport", required=False)
    if name is not None and name not in TRANSPORTS:
        raise ServeError("bad_params", f"transport must be one of {sorted(TRANSPORTS)}")
    if not session.import_slot.acquire(blocking=False):
        raise ServeError("busy", "an import is already running")
    session.import_thread = threading.Thread(
        target=_import_worker, args=(session, call, path, TRANSPORTS.get(name)), name="import")
    session.import_thread.start()
    return _DEFERRED


@_unlocked_only
def import_last(session: Session, call: Call) -> dict:
    """The newest import runs, each with its recorded failure count."""
    with session.reader() as conn:
        has_failures = migrations.has_table(conn, "import_failures")
        rows = conn.execute(
            "SELECT id, started_at, finished_at, transport, status, files_seen, files_imported, "
            "files_duplicate, files_failed, records_written, error FROM import_runs ORDER BY id DESC LIMIT ?",
            (LAST_IMPORTS,)).fetchall()
        runs = []
        for row in rows:
            run = dict(zip(("id", "started_at", "finished_at", "transport", "status", "files_seen",
                            "files_imported", "files_duplicate", "files_failed", "records_written", "error"), row))
            run["error"] = redact_text(run["error"])
            run["failures"] = (conn.execute("SELECT COUNT(*) FROM import_failures WHERE run_id=?",
                                            (run["id"],)).fetchone()[0] if has_failures else 0)
            runs.append(run)
    return {"runs": runs}


# ---- sync ----

@_unlocked_only
def sync_status(session: Session, call: Call) -> dict:
    """The relay counts of the store (``sync status``): read-only, so a store older than the relay
    tables answers what a fresh one would."""
    with session.reader() as conn:
        if migrations.has_table(conn, "relay_bundles"):
            return sync_module.status(conn)
        return {"bundles": {}, "records_unsent": conn.execute("SELECT count(*) FROM raw_records").fetchone()[0],
                "records_seen": 0, "conflicts": 0, "superseded": 0, "gaps": []}


def _sync_event(session: Session, phase: str, state: str, counts: dict | None = None) -> None:
    with contextlib.suppress(OSError):  # the bridge is gone; EOF on stdin ends the process
        session.channel.event({"event": "progress", "op": "sync", "phase": phase, "state": state, **(counts or {})})


def _run_sync(session: Session, master: bytes, relay: Any) -> dict:
    """Push, then pull, over one connection and one hold of the write lock (never waiting for it).
    Counts only: bundle names are random per push and nothing in a UI needs them."""
    with storage.open_for_write(session.db_path, purpose="sync", timeout_s=0.0) as conn:
        _sync_event(session, "push", "start")
        pushed = sync_module.push(conn, master, relay)
        push = {"bundles": len(pushed.bundles), "records": pushed.records, "ranges": pushed.ranges}
        _sync_event(session, "push", "done", push)
        _sync_event(session, "pull", "start")
        pulled = sync_module.pull(conn, master, relay)
        pull = {"applied": len(pulled.applied), "rejected": len(pulled.rejected),
                "records_new": pulled.records_new, "records_duplicate": pulled.records_duplicate,
                "records_invalid": pulled.records_invalid, "conflicts": pulled.conflicts,
                "ranges_new": pulled.ranges_new, "records_repaired": pulled.records_repaired,
                "gaps": len(pulled.gaps), "status": pulled.status}
        _sync_event(session, "pull", "done", pull)
    return {"push": push, "pull": pull}


def _sync_worker(session: Session, call: Call, master: bytes, relay: Any) -> None:
    """The worker thread body: sync, free the slot, then answer the request."""
    line = _line_for(call.id, lambda: _run_sync(session, master, relay))
    session.import_slot.release()
    with contextlib.suppress(OSError):
        session.channel.write(line)


@_unlocked_only
def sync_run(session: Session, call: Call) -> Any:
    """Start a push-then-pull over the relay ``relay.json`` names; the answer is sent when it finishes.
    Checked in this order: unlocked (``locked``), a relay configured (``not_found``), an encrypted store
    (``not_encrypted``), a transport this core has (``unsupported_transport``), the slot shared with
    ``import.run`` (``busy``)."""
    chosen = relay_config.read(session.db_path.parent / home.RELAY_CONFIG_NAME)
    if chosen is None:
        raise ServeError("not_found", "no relay is configured (relay.json in the data folder)")
    master = storage.unlocked_master(session.db_path)
    if master is None:
        raise ServeError("not_encrypted", f"the relay needs an encrypted store: run '{identity.COMMAND} key init' first")
    relay = relay_config.open_relay(*chosen)
    if not session.import_slot.acquire(blocking=False):
        raise ServeError("busy", "an import or a sync is already running")
    session.import_thread = threading.Thread(target=_sync_worker, args=(session, call, master, relay), name="sync")
    session.import_thread.start()
    return _DEFERRED


# ---- tools ----

@_unlocked_only
def tools_call(session: Session, call: Call) -> dict:
    """What the MCP tool ``name`` answers to ``arguments`` (``structuredContent``), read from this session's store.

    The coach loop of the desktop app reaches the six read-only tools through this method, so there is one
    implementation of them per core. Failures: ``invalid_params`` (``name`` is not a non-empty string,
    ``arguments`` is not an object, or the arguments do not fit the tool: the message names the parameters at
    fault, never a value), ``unknown_tool``, and ``tool_error`` (the tool's own error text, or its crash text)."""
    from disconect import mcp_server   # late: the SDK is imported only by a process that serves tools

    name = call.params.get("name")
    if not isinstance(name, str) or not name:
        raise ServeError("invalid_params", "name must be a non-empty string")
    arguments = call.params.get("arguments")
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        raise ServeError("invalid_params", "arguments must be an object")
    try:
        result = mcp_server.run_tool(name, arguments, session.db_path)
    except mcp_server.UnknownTool as exc:
        raise ServeError("unknown_tool", exc.text) from None
    except mcp_server.ArgumentsRejected as exc:
        raise ServeError("invalid_params", str(exc)) from None
    except mcp_server.ToolError as exc:
        raise ServeError("tool_error", str(exc)) from None
    except Exception:  # noqa: BLE001 - a crash: only the tool's name is told, as the MCP does
        raise ServeError("tool_error", mcp_server.CRASH_TEMPLATE.format(name=name)) from None
    return {"name": name, "result": result}


#: The protocol's methods. A method exists exactly when it is a key here.
METHODS: dict[str, Handler] = {
    "app.info": app_info,
    "key.status": key_status,
    "key.unlock": key_unlock,
    "key.cache": key_cache,
    "data.health": data_health,
    "data.metric": data_metric,
    "data.today": data_today,
    "data.facts": data_facts,
    "import.run": import_run,
    "import.last": import_last,
    "sync.status": sync_status,
    "sync.run": sync_run,
    "tools.call": tools_call,
}


# ---- the loop ----

class _Rejected(Exception):
    """A line that is not a usable request; ``request_id`` is None when not even an id could be read."""

    def __init__(self, request_id: Id, message: str):
        super().__init__(message)
        self.request_id = request_id


def _parse(line: str) -> Call:
    """Parse one request line, or raise :class:`_Rejected`."""
    try:
        request = json.loads(line)
    except ValueError:
        raise _Rejected(None, "request is not valid JSON") from None
    if not isinstance(request, dict):
        raise _Rejected(None, "request must be a JSON object")
    request_id = request.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        raise _Rejected(None, "request id must be an integer or a string")
    method, params = request.get("method"), request.get("params", {})
    if not isinstance(method, str) or not isinstance(params, dict):
        raise _Rejected(request_id, "method must be a string and params an object")
    return Call(request_id, method, params)


def _dispatch(session: Session, call: Call) -> str | None:
    """The response line for ``call``, or None when the handler answers later (the import worker)."""
    handler = METHODS.get(call.method)
    if handler is None:
        return _error_line(call.id, ServeError("unknown_method", "no such method"))
    try:
        result = handler(session, call)
        return None if result is _DEFERRED else encode({"id": call.id, "result": result})
    except Exception as exc:  # noqa: BLE001 - the process must outlive any one request; mapped to a code
        return _error_line(call.id, exc)


def handle_line(session: Session, line: str) -> None:
    """Answer one request line on the session's channel."""
    try:
        reply = _dispatch(session, _parse(line))
    except _Rejected as rejected:
        reply = _error_line(rejected.request_id, ServeError("bad_params", str(rejected)))
    if reply is not None:
        session.channel.write(reply)


def serve_lines(session: Session, lines: Iterable[str]) -> int:
    """The request loop: one request per non-blank line until the input ends, then wait for any import."""
    for line in lines:
        if line.strip():
            try:
                handle_line(session, line)
            except OSError:
                break  # the bridge's pipe is gone
    if session.import_thread is not None:
        session.import_thread.join()
    return 0


def isolate_stdout() -> TextIO:
    """Move the protocol to a private fd and point fd 1 at stderr; return the protocol's text stream."""
    sys.stdout.flush()
    private = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(private, "w", encoding="utf-8", newline="\n")


def _ignore_env_secrets(channel: Channel) -> None:
    """Drop the passphrase env vars: serve never unlocks from the environment."""
    was_set = os.environ.pop(keys.PASSPHRASE_ENV, None) is not None
    os.environ.pop(keys.RECOVERY_WORDS_ENV, None)
    if was_set:
        channel.event({"event": "log", "level": "warn",
                       "message": f"{keys.PASSPHRASE_ENV} is ignored by serve and was removed from the environment"})


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``disconect-serve``: serve the protocol on stdio until stdin closes."""
    parser = argparse.ArgumentParser(prog="disconect-serve", description="JSON Lines sidecar on stdio.")
    parser.add_argument("--db", default=str(storage.default_db_path()),
                        help=f"SQLite file (default ${storage.DEFAULT_DB_ENV} or ~/{identity.DATA_DIR}/{identity.DB_FILENAME}; "
                             f"the legacy ~/{identity.LEGACY_HOMES[0]} is read until {identity.COMMAND} migrate-home)")
    args = parser.parse_args(argv)
    storage.home.announce_default_resolution(args.db)
    channel = Channel(isolate_stdout())
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    _ignore_env_secrets(channel)
    return serve_lines(Session(pathlib.Path(args.db), channel), sys.stdin)


if __name__ == "__main__":
    sys.exit(main())
