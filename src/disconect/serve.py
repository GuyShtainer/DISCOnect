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
import time
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
LAST_IMPORTS = health.LAST_IMPORTS
#: Protocol transport names -> the names the store records.
TRANSPORTS = {"export": sources.TRANSPORT_CONNECT_EXPORT, "usb": "usb", "ble": sources.TRANSPORT_BLE}
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
        self.importing = False                   # the slot's holder is an import (not a sync): what import.cancel reaches
        self.import_cancel = threading.Event()   # set by import.cancel; the worker asks it after each file
        self.last_sites: list[dict] | None = None   # the ``sites`` of the last ``sync.run`` (null in ``sync.status.relays`` before the first)
        self.sites_lock = threading.Lock()          # the worker thread writes ``last_sites``, ``sync.status`` reads it

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


def key_lock(session: Session, call: Call) -> dict:
    """Drop this session's master key: the session reads as locked until ``key.unlock`` runs again. Idempotent
    (already locked, or nothing to lock, answers the same). The keychain item, if any, is left alone."""
    storage.forget(session.db_path)
    return {"state": "locked"}


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


def data_contract(session: Session, call: Call) -> dict:
    """The contract's numeric metrics with their unit, cadence and declared scopes, in contract order.
    Reads nothing from the store (so it answers on a locked one) — the Trend screen's metric list."""
    declared = {key for key in contract.STREAMS_FOR if contract.cadence_for(key[0]) is not None}
    declared |= {key for key in contract.SESSION_STREAMS_FOR if contract.cadence_for(key[0]) is not None}
    metrics = []
    for item in contract.METRICS:
        scopes = [scope for scope in contract.SOURCE_SCOPES if (item.metric, scope) in declared]
        if scopes:
            metrics.append({"metric": item.metric, "unit": item.unit, "cadence": item.cadence, "scopes": scopes})
    return {"scopes": list(contract.SOURCE_SCOPES), "metrics": metrics}


@_unlocked_only
def data_live(session: Session, call: Call) -> dict:
    """The live link on one local day (default today): its sessions and, per folded metric, the
    minute count and median — the Today live card's read."""
    day = _text_param(call.params, "day", required=False)
    with session.reader() as conn:
        return queries.live_day(conn, day if day is not None else queries.local_today(conn))


@_unlocked_only
def data_sleep(session: Session, call: Call) -> dict:
    """One night (default the latest stored), every source's record, stages also on the watch's clock."""
    date = _text_param(call.params, "date", required=False)
    with session.reader() as conn:
        return queries.sleep_detail(conn, date, local=True)


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
        stats = sources.import_path(path, conn, transport=transport, progress=progress,
                                    cancel=session.import_cancel.is_set)
        run_id = conn.execute("SELECT MAX(id) FROM import_runs").fetchone()[0]
    if stats.cancelled:
        raise ServeError("cancelled", CANCELLED_IMPORT)
    return _import_result(stats, run_id)


def _import_worker(session: Session, call: Call, path: pathlib.Path, transport: str | None) -> None:
    """The worker thread body: run the import, free the slot, then answer the request."""
    line = _line_for(call.id, lambda: _run_import(session, path, transport))
    session.importing = False
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
    session.import_cancel.clear()
    session.importing = True
    session.import_thread = threading.Thread(
        target=_import_worker, args=(session, call, path, TRANSPORTS.get(name)), name="import")
    try:
        session.import_thread.start()
    except RuntimeError:   # no thread could start: free the slot (the Rust core resets both too)
        session.importing = False
        session.import_slot.release()
        raise
    return _DEFERRED


#: What the cancelled ``import.run`` request answers (code ``cancelled``); ``import.cancel`` itself says only "cancelling".
CANCELLED_IMPORT = "the import was cancelled; the files read so far are kept"


@_unlocked_only
def import_cancel(session: Session, call: Call) -> dict:
    """Ask the running import to stop after the file it is reading (a stop path: only ``locked`` is checked).
    ``{"state": "cancelling"}`` while an import owns the slot, again on a repeat; the ``import.run`` request
    then answers ``cancelled`` — or its result, when the cancel came after the last file's check. No import
    (also a sync or a pair push holding the slot) → ``not_found``."""
    if not session.importing:
        raise ServeError("not_found", "no import is running")
    session.import_cancel.set()
    return {"state": "cancelling"}


@_unlocked_only
def import_last(session: Session, call: Call) -> dict:
    """The newest import runs, each with its recorded failure count."""
    with session.reader() as conn:
        has_failures = migrations.has_table(conn, "import_failures")
        rows = conn.execute(health.RECENT_RUNS_SQL, (LAST_IMPORTS,)).fetchall()
        runs = []
        for row in rows:
            run = dict(zip(health.RUN_COLUMNS, row))
            run["error"] = redact_text(run["error"])
            run["failures"] = (conn.execute("SELECT COUNT(*) FROM import_failures WHERE run_id=?",
                                            (run["id"],)).fetchone()[0] if has_failures else 0)
            runs.append(run)
    return {"runs": runs}


# ---- sync ----

@_unlocked_only
def sync_status(session: Session, call: Call) -> dict:
    """The relay counts of the store (``sync status``): read-only, so a store older than the relay
    tables answers what a fresh one would. ``relay_url`` is the first LAN entry's ``http://host:port`` from
    ``relay.json``, null when it names none (a folder relay has no address); ``relays`` is the ``sites`` of the
    last ``sync.run`` of this session, null before the first."""
    entries = relay_config.read_list(session.db_path.parent / home.RELAY_CONFIG_NAME)
    lan = relay_config.first_lan_url(entries) if entries else None
    relay_url = relay_config.lan_base_url(lan) if lan is not None else None
    with session.sites_lock:
        relays = session.last_sites
    with session.reader() as conn:
        if migrations.has_table(conn, "relay_bundles"):
            return {**sync_module.status(conn), "serving": None, "relay_url": relay_url, "relays": relays}
        return {"bundles": {}, "records_unsent": conn.execute("SELECT count(*) FROM raw_records").fetchone()[0],
                "records_seen": 0, "conflicts": 0, "superseded": 0, "gaps": [], "last_pushed_at": None,
                "last_pulled_at": None, "serving": None, "relay_url": relay_url, "relays": relays}


def _sync_event(session: Session, phase: str, state: str, counts: dict | None = None) -> None:
    with contextlib.suppress(OSError):  # the bridge is gone; EOF on stdin ends the process
        session.channel.event({"event": "progress", "op": "sync", "phase": phase, "state": state, **(counts or {})})


def _run_sync(session: Session, master: bytes, specs: list[sync_module.SiteSpec]) -> dict:
    """Push, then pull, over one connection and one hold of the write lock (never waiting for it), over every relay
    of the list. Counts only: bundle names are random per push and nothing in a UI needs them. A list of one raises
    its relay's failure as the error (the single-relay behaviour); a longer list reports each site's failure in
    ``sites`` and the run is ``partial``."""
    with storage.open_for_write(session.db_path, purpose="sync", timeout_s=0.0) as conn:
        sites, reports = sync_module.open_sites(specs, master)
        strict = len(specs) == 1

        def keep() -> None:
            with session.sites_lock:
                session.last_sites = [report.as_dict() for report in reports]

        _sync_event(session, "push", "start")
        try:
            pushed = (sync_module.push_strict if strict else sync_module.push_all)(conn, master, sites, reports)
        except Exception:
            keep()
            raise
        push = {"bundles": len(pushed.bundles), "records": pushed.records, "ranges": pushed.ranges}
        _sync_event(session, "push", "done", push)
        _sync_event(session, "pull", "start")
        try:
            pulled = (sync_module.pull_strict if strict else sync_module.pull_all)(conn, master, sites, reports)
        except Exception:
            keep()
            raise
        pull = {"applied": len(pulled.applied), "rejected": len(pulled.rejected),
                "records_new": pulled.records_new, "records_duplicate": pulled.records_duplicate,
                "records_invalid": pulled.records_invalid, "conflicts": pulled.conflicts,
                "ranges_new": pulled.ranges_new, "records_repaired": pulled.records_repaired,
                "records_kept": pulled.records_kept, "gaps": len(pulled.gaps), "status": pulled.status}
        _sync_event(session, "pull", "done", pull)
        keep()
    partial = pushed.partial or pulled.status != "ok" or any(report.error is not None for report in reports)
    return {"push": push, "pull": pull, "sites": [report.as_dict() for report in reports],
            "status": "partial" if partial else "ok"}


def _sync_worker(session: Session, call: Call, master: bytes, specs: list[sync_module.SiteSpec]) -> None:
    """The worker thread body: sync, free the slot, then answer the request."""
    line = _line_for(call.id, lambda: _run_sync(session, master, specs))
    session.import_slot.release()
    with contextlib.suppress(OSError):
        session.channel.write(line)


_RELAYS_BAD = "relays: each entry needs an id, a kind and a path or url"


def _relays_param(value: Any) -> list[sync_module.SiteSpec]:
    """``relays`` of ``sync.run``: the list this call uses instead of ``relay.json``'s. Each element is an object
    ``{"id", "kind": "folder" | "lan", "path" | "url", "label"?, "unavailable"?}``; ``path`` may be left out of an
    ``unavailable`` entry (the shell could not reach it). Ids pass ``valid_id`` and are distinct."""
    bad = ServeError("bad_params", _RELAYS_BAD)
    if not isinstance(value, list):
        raise bad
    specs: list[sync_module.SiteSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise bad

        def text(key: str, item: dict = item) -> str | None:
            found = item.get(key)
            return found if isinstance(found, str) and found else None

        ident = text("id")
        if ident is None or not relay_config.valid_id(ident):
            raise bad
        unavailable = item.get("unavailable")
        if unavailable is not None and not isinstance(unavailable, bool):
            raise bad
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise bad
        kind = text("kind")
        if kind not in ("folder", "lan"):
            raise bad
        place = text("path" if kind == "folder" else "url")
        if place is None:
            if not unavailable:
                raise bad
            place = ""
        if any(spec.id == ident for spec in specs):
            raise bad
        specs.append(sync_module.SiteSpec(ident, kind, place, bool(unavailable), False, label or "", True))
    return specs


@_unlocked_only
def sync_run(session: Session, call: Call) -> Any:
    """Start a push-then-pull over the relays of the call's ``relays`` param, else of ``relay.json``; the answer is
    sent when it finishes. Checked in this order: unlocked (``locked``), a relay configured (``not_found``; an empty
    ``relays`` counts as none), an encrypted store (``not_encrypted``), the shape of ``relays`` (``bad_params``), a
    list of one that is a malformed or a ``lan`` address (``bad_params``, ``unsupported_transport``: this core has no
    LAN transport), the slot shared with ``import.run`` (``busy``)."""
    not_found = ServeError("not_found", "no relay is configured (relay.json in the data folder)")
    per_call = "relays" in call.params
    given = call.params.get("relays")
    entries = None
    if per_call:
        if isinstance(given, list) and not given:
            raise not_found
    else:
        entries = relay_config.read_list(session.db_path.parent / home.RELAY_CONFIG_NAME)
        if not entries:
            raise not_found
    master = storage.unlocked_master(session.db_path)
    if master is None:
        raise ServeError("not_encrypted", f"the relay needs an encrypted store: run '{identity.COMMAND} key init' first")
    specs = _relays_param(given) if per_call else [sync_module.SiteSpec.from_entry(entry) for entry in entries or []]
    strict_lan = len(specs) == 1 and not specs[0].unavailable and specs[0].kind == "lan"
    if strict_lan:
        # a list of one with a malformed address is refused before anything is opened; a well-formed one is a
        # transport this core does not have (the strict single-relay error, as before)
        try:
            base = relay_config.parse_base_url(specs[0].value)
        except ValueError as rule:
            raise ServeError("bad_params", f"{'relays' if per_call else 'relay.json'}: {rule}") from None
        if per_call and not relay_config.lan_address_class_ok(base):
            raise ServeError("bad_params", "relays: a LAN relay address must be an IP address on a private or local "
                                           "network, not a name")
    held = session.import_slot.acquire(blocking=False)
    if strict_lan:
        # this core's strict single-lan failure. The site row is recorded only while the slot is held, so a call
        # that finds another run in flight never replaces that run's sites
        if held:
            with session.sites_lock:
                session.last_sites = [sync_module.SiteReport(specs[0].id, "lan", label=specs[0].label,
                                                             error="unsupported_transport").as_dict()]
            session.import_slot.release()
        raise relay_config.UnsupportedTransport(relay_config.LAN_TEXT)
    if not held:
        raise ServeError("busy", "an import or a sync is already running")
    session.import_thread = threading.Thread(target=_sync_worker, args=(session, call, master, specs), name="sync")
    session.import_thread.start()
    return _DEFERRED


# ---- relay and pairing (this core runs no LAN server) ----

NO_SERVER = "this core runs no LAN server"


def _relay_prefix(session: Session) -> None:
    """The checks the five relay/pair methods share, in the Rust core's order: unlocked (``locked``, the
    decorator); a relay configured (``not_found``); a list with no ``serve`` folder entry serves nothing
    (``bad_params``); an encrypted store (``not_encrypted``); no ``<keys>.next`` rotation file (``busy``).
    Parameter shapes follow."""
    entries = relay_config.read_list(session.db_path.parent / home.RELAY_CONFIG_NAME)
    if not entries:
        raise ServeError("not_found", "no relay is configured (relay.json in the data folder)")
    if relay_config.serve_entry(entries) is None:
        raise ServeError("bad_params", "this device is a joiner; it serves nothing")
    if storage.unlocked_master(session.db_path) is None:
        raise ServeError("not_encrypted", f"the relay needs an encrypted store: run '{identity.COMMAND} key init' first")
    key_path = keys.key_path_for(session.db_path)
    if key_path.with_name(key_path.name + keys.NEXT_SUFFIX).exists():
        raise ServeError("busy", "a key rotation is in progress; finish it first")


def _listen_param(params: dict) -> tuple | None:
    """``listen``: absent or null is None; anything else must be a string ``parse_listen`` accepts."""
    value = params.get("listen")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ServeError("bad_params", "listen must be a non-empty string")
    try:
        return relay_config.parse_listen(value)
    except ValueError as exc:
        raise ServeError("bad_params", str(exc)) from None


def _required_listen(params: dict) -> tuple:
    found = _listen_param(params)
    if found is None:
        raise ServeError("bad_params", "listen must be a non-empty string")
    return found


@_unlocked_only
def relay_addresses(session: Session, call: Call) -> Any:
    """The Mac's addresses (Rust only): after the shared prefix, ``unsupported_transport``."""
    _relay_prefix(session)
    raise relay_config.UnsupportedTransport(NO_SERVER)


@_unlocked_only
def relay_serve(session: Session, call: Call) -> Any:
    """The serve switch (Rust only): prefix, ``on`` (bool), ``listen`` (needed when ``on``), then ``unsupported_transport``."""
    # the stop path checks only ``locked`` (the decorator) and the parameter shapes, as on the Rust core
    if call.params.get("on") is False:
        _listen_param(call.params)
        return {"serving": False, "url": None}   # this core serves nothing, so there is nothing to stop
    _relay_prefix(session)
    on = _bool_param(call.params, "on")
    listen = _listen_param(call.params)
    if on and listen is None:
        raise ServeError("bad_params", "listen must be a non-empty string")
    raise relay_config.UnsupportedTransport(NO_SERVER)


@_unlocked_only
def pair_offer(session: Session, call: Call) -> Any:
    """Open a pairing offer (Rust only): prefix, ``listen``, then ``unsupported_transport``."""
    _relay_prefix(session)
    _required_listen(call.params)
    raise relay_config.UnsupportedTransport(NO_SERVER)


@_unlocked_only
def pair_confirm(session: Session, call: Call) -> Any:
    """Confirm the typed digits (Rust only): prefix, ``digits`` (exactly six ASCII digits), then ``unsupported_transport``."""
    _relay_prefix(session)
    digits = call.params.get("digits")
    if not (isinstance(digits, str) and len(digits) == 6 and all(c in "0123456789" for c in digits)):
        raise ServeError("bad_params", "digits must be exactly six digits")
    raise relay_config.UnsupportedTransport(NO_SERVER)


@_unlocked_only
def pair_cancel(session: Session, call: Call) -> Any:
    """End the open offer: this core never has one, so ``not_found`` (only ``locked`` is checked before)."""
    raise ServeError("not_found", "there is no offer to cancel")


# ---- the phone's own method ----

def pair_forget(session: Session, call: Call) -> Any:
    """``pair.forget`` is the phone app's method (Rust only): this core is never a phone, so it always refuses, with
    no ``locked`` check first and whatever the params are."""
    raise ServeError("unsupported_transport", "this core is not a phone; there is nothing to forget")


# ---- the phone as the pairing joiner (12-F row 3) ----

MAX_OFFER_TEXT = 1024
_EXP_AHEAD_MAX = 900 + 300     # an offerer sets exp = now + 900; the relay's own clock window is the slack
NOT_A_PHONE = "this core is not a phone; it does not join a pairing"
#: Rust's ``str::trim`` strips exactly the Unicode White_Space characters; ``str.strip()`` also strips U+001C..U+001F.
_RUST_WHITE_SPACE = "\t\n\x0b\x0c\r \x85\xa0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B)) + "\u2028\u2029\u202f\u205f\u3000"
#: The phone's ``forget.pending`` marker (``pair_forget.rs`` ``MARKER_NAME``), beside the store.
_FORGET_MARKER = "forget.pending"


def _never_a_pairing_address(host: str) -> bool:
    """Stage 1 of the address check, shared with the Rust core (``relay_config.never_a_pairing_address``)."""
    return relay_config.never_a_pairing_address(host)


def _folder_holds_a_key_file(folder) -> bool:
    """Any ``*.keys.json`` or ``*.keys.json.next`` in ``folder``: some store's pairing lives there (the Rust core's
    ``folder_holds_a_key_file``). An unreadable folder counts as none."""
    try:
        names = [item.name for item in folder.iterdir()]
    except OSError:
        return False
    rotation = keys.KEY_FILE_SUFFIX + keys.NEXT_SUFFIX
    return any(name.endswith(keys.KEY_FILE_SUFFIX) or name.endswith(rotation) for name in names)


def _join_prefix(text: str, db_path) -> None:
    """The prefix of ``pair.join``, in the Rust core's order and with its words: the shape (a string within the
    bound that parses as an offer), an IP-literal host, an address that is not unspecified, broadcast, multicast,
    reserved or link-local, ``exp`` at most 1200 s ahead, the expiry by this machine's clock (no tolerance past
    ``exp``), the landing site (a store, key file or rotation file here means "already paired"; so does a
    ``relay.json`` that is not a LAN relay, or a LAN relay of another address beside any store's key file in this
    folder: a LAN relay file alone is a failed landing's leftover, and one naming the offer's address is this
    pairing's, 12-H (d)) and an unfinished forget. Nothing here touches the network and no message echoes the
    offer."""
    from disconect import pair as pair_module   # late: the module name is also a method family here

    text = text.strip(_RUST_WHITE_SPACE)
    if len(text.encode("utf-8", "surrogatepass")) > MAX_OFFER_TEXT:      # bytes, as in Rust
        raise ServeError("bad_params", "that is not a pairing offer")
    try:
        offer = pair_module.parse_offer(text)
    except pair_module.PairError:
        raise ServeError("bad_params", "that is not a pairing offer") from None
    host = offer.url[len("http://"):].rpartition(":")[0]
    if not (host.startswith("[") or (host and all(c in "0123456789." for c in host))):
        raise ServeError("bad_params", "the offer's address must be an IP address, not a name")
    if _never_a_pairing_address(host):
        raise ServeError("bad_params", "the offer's address cannot be a pairing address")
    now = int(time.time())
    if offer.exp > now + _EXP_AHEAD_MAX:
        raise ServeError("pair_failed", "This offer is too far ahead of this phone's clock. Check the date and time.")
    if now > offer.exp:
        raise ServeError("pair_failed", "This offer expired by this phone's clock. Check the date and time.")
    paired = ServeError("pair_failed", "This phone is already paired")
    if os.environ.get(keys.KEYS_ENV):
        raise paired
    key_path = keys.key_path_for(db_path)
    if db_path.exists() or key_path.exists() or key_path.with_name(key_path.name + keys.NEXT_SUFFIX).exists():
        raise paired
    relay_file = db_path.parent / home.RELAY_CONFIG_NAME
    if relay_file.exists():
        named = relay_config.read(relay_file)
        if named is None or named[0] != "lan":
            raise paired
        if named[1] != offer.url and _folder_holds_a_key_file(db_path.parent):
            raise paired
    if (db_path.parent / _FORGET_MARKER).exists():
        raise ServeError("pair_failed", "This phone has not finished forgetting its last pairing. "
                                        "Close and reopen the app, then try again.")


def pair_join(session: Session, call: Call) -> Any:
    """``pair.join`` is the phone app's method (Rust only): the offer's shape and the phone's stricter rules are
    checked as on the phone (no lock check first), then this core, which is never a phone, refuses."""
    offer = call.params.get("offer")
    if not isinstance(offer, str) or not offer:
        raise ServeError("bad_params", "offer must be a non-empty string")
    _join_prefix(offer, session.db_path)
    raise ServeError("unsupported_transport", NOT_A_PHONE)


def pair_land(session: Session, call: Call) -> Any:
    """``pair.land`` is the phone's answer to the join's check (Rust only): the shape of ``confirm``, then refuse."""
    if not isinstance(call.params.get("confirm"), bool):
        raise ServeError("bad_params", "confirm must be true or false")
    raise ServeError("unsupported_transport", NOT_A_PHONE)


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
    "key.lock": key_lock,
    "data.health": data_health,
    "data.metric": data_metric,
    "data.today": data_today,
    "data.live": data_live,
    "data.sleep": data_sleep,
    "data.contract": data_contract,
    "data.facts": data_facts,
    "import.run": import_run,
    "import.last": import_last,
    "import.cancel": import_cancel,
    "sync.status": sync_status,
    "sync.run": sync_run,
    "relay.addresses": relay_addresses,
    "relay.serve": relay_serve,
    "pair.offer": pair_offer,
    "pair.confirm": pair_confirm,
    "pair.cancel": pair_cancel,
    "tools.call": tools_call,
    "pair.forget": pair_forget,
    "pair.join": pair_join,
    "pair.land": pair_land,
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
