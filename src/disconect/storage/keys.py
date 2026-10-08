"""The key file: who may open an encrypted store, and how the key is recovered.

One **master key** (32 random bytes) per database. The SQLCipher key is derived
from it (HKDF, ``info=hearthbeat/db``) so the passphrase can change without
re-encrypting the database. The master key is stored wrapped under a key the
passphrase derives (Argon2id) with ChaCha20-Poly1305, in ``<db>.keys.json``
beside the database. The **recovery phrase** is the master key itself as 24
BIP39 words, so recovery needs no file at all; rotating the words means a new
master key and a ``PRAGMA rekey`` of the database.

Unlock order: ``DISCONECT_PASSPHRASE`` (tests and CI only: removed from the
process environment after reading and warned about) -> the macOS keychain item
named by the key id (opt-in, holds the master key) -> a passphrase prompt only
when both stdin and stderr are terminals. Nothing here ever prints or logs key
material; the words are shown once by ``key init`` and only to a terminal.

Format pinned for the Rust core and the phone app (both developed separately): standard
base64 with padding, fixed AAD bytes (never re-serialised JSON), NFC-normalised
passphrases, Argon2id version 0x13 with parallelism 1 (libsodium's constraint).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import getpass
import json
import os
import pathlib
import secrets
import sys
import unicodedata

from argon2 import low_level as argon2
from argon2.exceptions import Argon2Error
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mnemonic import Mnemonic

from disconect import identity
from disconect.storage import home

FORMAT_VERSION = 1
PASSPHRASE_ENV = "DISCONECT_PASSPHRASE"
KEYS_ENV = "DISCONECT_KEYS"
KEY_FILE_SUFFIX = ".keys.json"
NEXT_SUFFIX = ".next"          # a rotation in progress: the key the database is being moved to
RECOVERY_WORDS_ENV = "DISCONECT_RECOVERY_WORDS"
KEYCHAIN_SERVICE = identity.CLI_KEYCHAIN_SERVICE   # the Rust core uses its own (macOS binds a keychain item to the program that made it)
MIN_PASSPHRASE_CHARS = 12
PURPOSE_PASSPHRASE = "passphrase"

#: Argon2id cost for the passphrase wrap: ~0.4 s on an M3 Pro. Tests lower these through
#: :func:`set_kdf_params`; production refuses anything below the floor.
KDF_PARAMS = {"m_kib": 256 * 1024, "t": 3, "p": 1, "v": 0x13}
_KDF_FLOOR = {"m_kib": 64 * 1024, "t": 2}
_KDF_CEILING = {"m_kib": 4 * 1024 * 1024, "t": 20}   # a hostile file must not turn unlock into a hang
_kdf_override: dict | None = None
#: The passphrase this process already used successfully (memory only): lets a second key file
#: (a rotation's ``.next``) unlock without asking again. Never written anywhere.
_session_passphrase: str | None = None


class KeyError_(Exception):
    """Base class: the key file is missing, unreadable, or the secret does not unlock it."""


class KeyFileMissing(KeyError_):
    pass


class KeyFileCorrupt(KeyError_):
    pass


class WrongPassphrase(KeyError_):
    pass


class Locked(KeyError_):
    """No unlock path applied (no env passphrase, no keychain item, no terminal)."""


class WeakPassphrase(KeyError_):
    pass


@dataclasses.dataclass(frozen=True)
class KeyFile:
    """The parsed key file. Holds no secrets."""

    key_id: bytes
    created_at: str
    wraps: list[dict]
    sqlcipher: dict
    raw: dict


# ---- derivations ----

def db_key(master: bytes) -> bytes:
    """The 32-byte SQLCipher raw key for ``master``."""
    return HKDF(hashes.SHA256(), 32, None, b"hearthbeat/db").derive(master)


def key_id_for(master: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 16, None, b"hearthbeat/key-id").derive(master)


def db_key_hex(master: bytes) -> str:
    return db_key(master).hex()


def _aad(purpose: str, key_id: bytes) -> bytes:
    return b"hearthbeat/keys/v1/" + purpose.encode() + b"/" + key_id.hex().encode()


def normalise_passphrase(text: str) -> bytes:
    return unicodedata.normalize("NFC", text).encode("utf-8")


def check_passphrase_strength(text: str) -> None:
    """Raise :class:`WeakPassphrase` below the floor; an offline guess against the file is the attack."""
    if len(unicodedata.normalize("NFC", text)) < MIN_PASSPHRASE_CHARS:
        raise WeakPassphrase(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters "
                             "(or use a generated word passphrase)")


def generate_passphrase(words: int = 6) -> str:
    """A memorable passphrase from the BIP39 word list (about 11 bits per word)."""
    wordlist = Mnemonic("english").wordlist
    return " ".join(secrets.choice(wordlist) for _ in range(words))


def kdf_params() -> dict:
    return dict(_kdf_override or KDF_PARAMS)


def set_kdf_params(params: dict | None) -> None:
    """Test hook: lower the Argon2id cost for the duration of a test. ``None`` restores production."""
    global _kdf_override
    _kdf_override = dict(params) if params else None


def _validate_params(params: dict) -> dict:
    """Exact keys, p=1, v=0x13, cost inside [floor, ceiling] (the floor is lifted only by the test hook)."""
    if set(params) != {"m_kib", "t", "p", "v"}:
        raise KeyFileCorrupt("key file KDF params are malformed")
    clean = {k: int(v) for k, v in params.items()}
    if clean["p"] != 1 or clean["v"] != 0x13:
        raise KeyFileCorrupt("key file asks for Argon2 settings this build does not use")
    if clean["m_kib"] > _KDF_CEILING["m_kib"] or clean["t"] > _KDF_CEILING["t"]:
        raise KeyFileCorrupt("key file asks for a KDF cost above the ceiling; refusing")
    if _kdf_override is None and (clean["m_kib"] < _KDF_FLOOR["m_kib"] or clean["t"] < _KDF_FLOOR["t"]):
        raise KeyFileCorrupt("key file asks for a KDF cost below the floor; refusing")
    return clean


def _passphrase_key(passphrase: str, salt: bytes, params: dict) -> bytes:
    params = _validate_params(params)
    try:
        return argon2.hash_secret_raw(normalise_passphrase(passphrase), salt, time_cost=params["t"],
                                      memory_cost=params["m_kib"], parallelism=params["p"], hash_len=32,
                                      type=argon2.Type.ID, version=params["v"])
    except Argon2Error as exc:
        raise KeyFileCorrupt(f"key derivation failed: {type(exc).__name__}") from exc


# ---- words ----

def words_for(master: bytes) -> str:
    """The master key as 24 BIP39 English words. Shown once; never stored."""
    return Mnemonic("english").to_mnemonic(master)


def master_from_words(words: str) -> bytes:
    """Parse a recovery phrase back into the master key; raise :class:`WrongPassphrase` if invalid."""
    mnemonic = Mnemonic("english")
    cleaned = " ".join(words.strip().lower().split())
    if not mnemonic.check(cleaned):
        raise WrongPassphrase("that is not a valid 24-word recovery phrase")
    master = bytes(mnemonic.to_entropy(cleaned))
    if len(master) != 32:
        raise WrongPassphrase("recovery phrase must be 24 words")
    return master


# ---- file ----

def key_path_for(db_path: pathlib.Path) -> pathlib.Path:
    """``$DISCONECT_KEYS`` if set, else ``<db>.keys.json`` beside the database."""
    override = os.environ.get(KEYS_ENV)
    if override:
        return home.expand_user(override)
    db_path = pathlib.Path(db_path)
    return db_path.with_name(db_path.name + KEY_FILE_SUFFIX)


def _wrap(master: bytes, key_id: bytes, passphrase: str) -> dict:
    params = kdf_params()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    sealed = ChaCha20Poly1305(_passphrase_key(passphrase, salt, params)).encrypt(
        nonce, master, _aad(PURPOSE_PASSPHRASE, key_id))
    return {"purpose": PURPOSE_PASSPHRASE, "kdf": "argon2id", "params": params,
            "salt": base64.b64encode(salt).decode(), "aead": "chacha20poly1305",
            "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(sealed).decode()}


def _unwrap(wrap: dict, key_id: bytes, passphrase: str) -> bytes:
    try:
        if not isinstance(wrap, dict) or not isinstance(wrap.get("params"), dict):
            raise KeyFileCorrupt("key file wrap is malformed")
        params = dict(wrap["params"])
        salt, nonce, sealed = (base64.b64decode(wrap[k], validate=True) for k in ("salt", "nonce", "ct"))
        if wrap.get("kdf") != "argon2id" or wrap.get("aead") != "chacha20poly1305":
            raise KeyFileCorrupt("key file uses algorithms this build does not know")
        if len(salt) != 16 or len(nonce) != 12:
            raise KeyFileCorrupt("key file wrap has the wrong salt or nonce size")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise KeyFileCorrupt(f"key file wrap is malformed: {type(exc).__name__}") from exc
    try:
        master = ChaCha20Poly1305(_passphrase_key(passphrase, salt, params)).decrypt(
            nonce, sealed, _aad(wrap["purpose"], key_id))
    except InvalidTag as exc:
        raise WrongPassphrase("wrong passphrase") from exc
    if key_id_for(master) != key_id:
        raise KeyFileCorrupt("key file's key id does not match its master key")
    return master


def _document(master: bytes, passphrase: str, created_at: str | None = None,
              sqlcipher: dict | None = None) -> dict:
    key_id = key_id_for(master)
    return {
        "format": "hearthbeat-keys", "format_version": FORMAT_VERSION, "key_id": key_id.hex(),
        "created_at": created_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "sqlcipher": sqlcipher or {"compat": 4, "page_size": 4096, "key_form": "raw-hex", "db_salt": None},
        "wraps": [_wrap(master, key_id, passphrase)],
    }


def write_key_file(path: pathlib.Path, document: dict) -> None:
    """Atomic 0600 write: exclusive temp file -> fsync -> replace -> fsync directory. Directory becomes 0700."""
    path = pathlib.Path(path)
    home.ensure_parent_dir(path)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(path.name + f".tmp-{secrets.token_hex(4)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_key_file(path: pathlib.Path) -> KeyFile:
    path = pathlib.Path(path)
    if not path.exists():
        raise KeyFileMissing(f"no key file at {path.name}; run '{identity.COMMAND} key init' (or set {KEYS_ENV})")
    try:
        raw = json.loads(path.read_text())
        if raw.get("format") != "hearthbeat-keys" or int(raw.get("format_version", 0)) > FORMAT_VERSION:
            raise KeyFileCorrupt("key file is not a disconect key file this build understands")
        key_id = bytes.fromhex(raw["key_id"])
        wraps = list(raw["wraps"])
        if len(key_id) != 16 or not wraps:
            raise KeyFileCorrupt("key file has no usable key id or wraps")
        return KeyFile(key_id, str(raw.get("created_at", "")), wraps, dict(raw.get("sqlcipher", {})), raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise KeyFileCorrupt(f"key file is unreadable: {type(exc).__name__}") from exc


def create(path: pathlib.Path, passphrase: str) -> bytes:
    """Make a new master key, write its key file at ``path``; return the master key (for words/keychain)."""
    check_passphrase_strength(passphrase)
    if pathlib.Path(path).exists():
        raise KeyError_(f"{pathlib.Path(path).name} already exists; refusing to overwrite a key file")
    master = secrets.token_bytes(32)
    write_key_file(path, _document(master, passphrase))
    return master


def unlock_with_passphrase(key_file: KeyFile, passphrase: str) -> bytes:
    for wrap in key_file.wraps:
        if isinstance(wrap, dict) and wrap.get("purpose") == PURPOSE_PASSPHRASE:
            return _unwrap(wrap, key_file.key_id, passphrase)
    raise KeyFileCorrupt("key file has no passphrase wrap")


def rewrap(path: pathlib.Path, master: bytes, new_passphrase: str) -> None:
    """Replace the passphrase wrap (same master key, so the database key is unchanged)."""
    check_passphrase_strength(new_passphrase)
    existing = read_key_file(path)
    write_key_file(path, _document(master, new_passphrase, existing.created_at, existing.sqlcipher))


# ---- unlock paths ----

#: TEST-ONLY switch, the twin of `keychain.rs`'s `BACKEND_ENV`: ``fail`` makes every keychain call behave like
#: ``PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`` (reads find nothing, writes raise
#: ``NoKeyringError``, the item probe never spawns), so a differential run never touches the login keychain.
#: The desktop sidecar's env allowlist never passes it on.
KEYCHAIN_BACKEND_ENV = "DISCONECT_KEYCHAIN"


class _FailKeychain:
    """The ``fail`` switch as a stand-in for the ``keyring`` module: the fail backend behind the same four
    calls, without ``keyring``'s backend detection (which loads plugins and, on Linux, opens D-Bus) and
    without touching its process-wide global, so the switch is never sticky and never races a thread."""

    def __init__(self, keyring) -> None:
        from keyring.backends import fail  # noqa: PLC0415 - only on this path
        self._backend = fail.Keyring()
        self.errors = keyring.errors

    def get_keyring(self):
        return self._backend

    def get_password(self, service: str, username: str):
        return self._backend.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._backend.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self._backend.delete_password(service, username)


def _keychain():
    import keyring  # imported lazily: optional at runtime, and slow to import
    if os.environ.get(KEYCHAIN_BACKEND_ENV) == "fail":
        return _FailKeychain(keyring)
    return keyring


def is_keychain_error(exc: BaseException) -> bool:
    """A failure of the keychain backend itself (no backend, a refused write), as opposed to an absent item."""
    try:
        import keyring.errors  # noqa: PLC0415 - optional at runtime
    except ImportError:
        return False
    return isinstance(exc, keyring.errors.KeyringError)


def _login_keychain_active() -> bool:
    """True only when the live macOS login keychain is the active backend: the one place a ``stale`` item
    can exist. A memory, null or fail backend (tests, the differential harness) holds no items of ours, and
    a macOS backend pointed at another keychain file (``KEYCHAIN_PATH``) is not the login keychain either,
    so the ``security`` probe (which searches the default keychain list) must never run for them."""
    if sys.platform != "darwin":
        return False
    keyring = _keychain()
    from keyring.backends import macOS  # noqa: PLC0415 - only on this path
    backend = keyring.get_keyring()
    return isinstance(backend, macOS.Keyring) and not getattr(backend, "keychain", None)


def keychain_get(key_id: bytes) -> bytes | None:
    try:
        stored = _keychain().get_password(KEYCHAIN_SERVICE, key_id.hex())
    except Exception:  # noqa: BLE001 - any keychain failure means "not cached", never a crash
        return None
    return bytes.fromhex(stored) if stored else None


KEYCHAIN_CACHED, KEYCHAIN_ABSENT, KEYCHAIN_STALE = "cached", "absent", "stale"


def _keychain_item_exists(key_id: bytes) -> bool:
    """macOS only: does an item for this key id exist at all (readable by us or not)? Never reads it, and
    never runs unless the login keychain is the active backend (see ``_login_keychain_active``)."""
    if not _login_keychain_active():
        return False
    import subprocess  # noqa: PLC0415 - only on this path
    try:
        done = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                               "-a", key_id.hex()], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def keychain_state(key_id: bytes) -> str:
    """``cached`` when this process can read the item, ``absent`` when none exists, ``stale`` when an
    item exists that this code identity cannot read (macOS binds items to the binary that made them,
    so a rebuilt app sees its own earlier item as stale)."""
    if keychain_get(key_id) is not None:
        return KEYCHAIN_CACHED
    return KEYCHAIN_STALE if _keychain_item_exists(key_id) else KEYCHAIN_ABSENT


def keychain_set(key_id: bytes, master: bytes) -> None:
    _keychain().set_password(KEYCHAIN_SERVICE, key_id.hex(), master.hex())


def keychain_delete(key_id: bytes) -> bool:
    keyring = _keychain()
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, key_id.hex())
    except keyring.errors.PasswordDeleteError:
        return False
    return True


def keychain_delete_legacy(key_id: bytes) -> bool:
    """Delete the items an earlier build left under its own service name (``identity.LEGACY_CLI_KEYCHAIN_SERVICES``)
    for this key id; absent items and keychain failures are ignored. True if any item was removed."""
    keyring = _keychain()
    removed = False
    for service in identity.LEGACY_CLI_KEYCHAIN_SERVICES:
        try:
            keyring.delete_password(service, key_id.hex())
            removed = True
        except keyring.errors.KeyringError:  # absent (PasswordDeleteError) or no backend at all: ignored, as documented
            pass
    return removed


def keychain_after_rotate(old_master: bytes, new_master: bytes) -> str | None:
    """Keychain housekeeping after a rekey: a cached item moves to the new key, an earlier build's item for
    the old key goes. Runs after the new words were shown, and a keychain failure is a note for stderr, never
    an error: nothing may stand between a successful rekey and the words."""
    old_id, new_id = key_id_for(old_master), key_id_for(new_master)
    try:
        if keychain_get(old_id) is not None:
            keychain_delete(old_id)
            keychain_set(new_id, new_master)
        keychain_delete_legacy(old_id)
    except Exception as exc:  # noqa: BLE001 - a keychain failure must not reach the caller
        if not is_keychain_error(exc):
            raise
        return f"keychain not updated ({type(exc).__name__}: {exc}); run 'disconect key cache' to cache the new key"
    return None


def status_for(db_path: pathlib.Path) -> dict:
    """Which unlock paths exist for ``db_path``, as ``disconect key status`` reports them. Never key material."""
    from disconect import storage  # lazy: storage imports this module
    key_path = key_path_for(db_path)
    status = {"key_file": key_path.exists(), "database_encrypted": storage.is_encrypted_file(db_path),
              "env_passphrase_set": bool(os.environ.get(PASSPHRASE_ENV)), "keychain": None, "kdf": None}
    if status["key_file"]:
        key_file = read_key_file(key_path)
        status["keychain"] = keychain_get(key_file.key_id) is not None
        status["kdf"] = next((w.get("params") for w in key_file.wraps if w.get("purpose") == PURPOSE_PASSPHRASE), None)
        status["created_at"] = key_file.created_at
    return status


def _env_passphrase() -> str | None:
    """Read and REMOVE the test/CI passphrase so child processes do not inherit it."""
    value = os.environ.pop(PASSPHRASE_ENV, None)
    if value:
        print(f"warning: unlocking with {PASSPHRASE_ENV} (tests/CI only; use 'disconect key cache' "
              "for daily use)", file=sys.stderr)
    return value or None


def env_recovery_words() -> str | None:
    """Tests/CI only: a recovery phrase from the environment, removed on read and warned about
    (it IS the master key and is visible to anything that can read the process environment)."""
    value = os.environ.pop(RECOVERY_WORDS_ENV, None)
    if value:
        print(f"warning: recovery phrase taken from {RECOVERY_WORDS_ENV} (tests/CI only)", file=sys.stderr)
    return value or None


def unlock(key_path: pathlib.Path, *, allow_prompt: bool = True, prompt_text: str = "Passphrase: ") -> bytes:
    """Return the master key via env -> keychain -> terminal prompt; raise :class:`Locked` if none applies."""
    global _session_passphrase
    key_file = read_key_file(key_path)
    passphrase = _env_passphrase() or _session_passphrase
    if passphrase is not None:
        master = unlock_with_passphrase(key_file, passphrase)
        _session_passphrase = passphrase
        return master
    cached = keychain_get(key_file.key_id)
    if cached is not None:
        if key_id_for(cached) == key_file.key_id:
            return cached
    if allow_prompt and sys.stdin.isatty() and sys.stderr.isatty():
        passphrase = getpass.getpass(prompt_text, stream=sys.stderr)
        master = unlock_with_passphrase(key_file, passphrase)
        _session_passphrase = passphrase
        return master
    raise Locked("the database is encrypted and no unlock path applies: run 'disconect key cache' once "
                 "in a terminal (keychain), or use the disconect CLI in a terminal to be prompted")


def forget_session() -> None:
    """Drop the passphrase remembered for this process (tests)."""
    global _session_passphrase
    _session_passphrase = None
