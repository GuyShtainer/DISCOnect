"""``test_serve.py``'s process-level guarantees, run against the Rust binary (no subcommand = serve).

A sibling file rather than a parametrisation: ``test_serve.py``'s children are built around a Python
bootstrap that installs an in-memory keyring and injects a noisy method, neither of which a native
binary has. Here the keychain is switched off with ``DISCONECT_KEYCHAIN=fail`` (the twin of
``PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring``), and the stray-write test drives the
``fd_probe`` example, which calls the same ``isolate_stdout`` the binary does.

Also here: the methods the Rust core answers plus the ones it has deferred are exactly the oracle's.
"""

import base64
import json
import os
import re
import subprocess


from disconect import identity, serve
from disconect.storage import keys
from test_serve import PASS, WRONG, _encrypt, _requests
from test_privacy import _seed

import monorepo  # noqa: E402

CRATE = monorepo.CRATE
BINARY = monorepo.BINARY
PROBE = CRATE / "target" / "debug" / "examples" / "fd_probe"

needs_binary = monorepo.needs_binary


def _env(tmp_path, **extra):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DISCONECT_KEYCHAIN": "fail", **extra}


def _spawn(db_path, tmp_path, **env):
    return subprocess.Popen([str(BINARY), "--db", str(db_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_env(tmp_path, **env), text=True)


@monorepo.needs_monorepo
def test_the_rust_methods_plus_the_deferred_list_are_the_oracles_methods():
    serve_rs = (CRATE / "src" / "serve.rs").read_text()
    table = serve_rs.split("pub const METHODS", 1)[1].split("];", 1)[0]
    ported = set(re.findall(r'\("([a-z]+\.[a-z]+)", ', table))
    deferred = set(re.findall(r'"([a-z]+\.[a-z]+)"', (CRATE / "src" / "read" / "mod.rs").read_text().split(
        "DEFERRED_METHODS", 1)[1].split("];", 1)[0]))
    assert ported & deferred == set()
    assert ported | deferred == set(serve.METHODS), "a method was added to serve.py: port it or defer it"


@needs_binary
def test_eof_ends_the_rust_process_with_exit_zero_within_two_seconds(db_path, tmp_path):
    _seed(db_path)
    proc = _spawn(db_path, tmp_path)
    proc.stdin.write(_requests(("app.info", {})))
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["result"]["product"] == identity.PRODUCT
    proc.stdin.close()
    assert proc.wait(timeout=2) == 0
    proc.stdout.close()
    proc.stderr.close()


@needs_binary
def test_stray_prints_and_raw_fd_writes_never_reach_the_rust_protocol_stream(tmp_path):
    if not PROBE.exists():
        subprocess.run(["cargo", "build", "--example", "fd_probe"], cwd=CRATE, check=True,
                       env={**os.environ, "PATH": "/opt/homebrew/opt/rustup/bin:" + os.environ["PATH"]})
    done = subprocess.run([str(PROBE)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert done.returncode == 0
    lines = [json.loads(line) for line in done.stdout.splitlines()]      # every stdout line parses as protocol
    assert [line["id"] for line in lines] == [1, 2]
    assert "STRAY" not in done.stdout and all(f"STRAY {kind}" in done.stderr for kind in ("PRINT", "RAW WRITE", "STDOUT"))


@needs_binary
def test_passphrase_and_master_key_are_never_printed_by_the_rust_core(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    _encrypt(db_path, monkeypatch, production_kdf=True)
    master = keys.unlock_with_passphrase(keys.read_key_file(keys.key_path_for(db_path)), PASS)
    proc = _spawn(db_path, tmp_path)
    out, err = proc.communicate(_requests(("key.unlock", {"passphrase": WRONG}), ("key.unlock", {"passphrase": PASS}),
                                          ("key.cache", {"enable": True}), ("key.status", {}),
                                          ("key.cache", {"enable": False})), timeout=60)
    assert proc.returncode == 0
    replies = [json.loads(line) for line in out.splitlines()]
    assert [("error" in r) for r in replies] == [True, False, True, False, True]   # no keychain: cache fails
    secrets_ = {"passphrase": PASS, "wrong passphrase": WRONG, "master": master, "db key": keys.db_key(master)}
    for name, secret in secrets_.items():
        raw = secret.encode() if isinstance(secret, str) else secret
        forms = {raw, raw.hex().encode(), base64.b64encode(raw), base64.urlsafe_b64encode(raw)}
        for stream_name, text in (("stdout", out), ("stderr", err)):
            for form in forms:
                assert form.decode("latin-1") not in text, f"{name} leaked on {stream_name}"


@needs_binary
def test_the_env_passphrase_is_ignored_and_reported_by_the_rust_core(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    _encrypt(db_path, monkeypatch, production_kdf=True)
    proc = _spawn(db_path, tmp_path, **{keys.PASSPHRASE_ENV: PASS})
    out, _err = proc.communicate(_requests(("key.status", {}), ("key.cache", {"enable": True})), timeout=60)
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["event"] == "log" and lines[0]["level"] == "warn" and PASS not in lines[0]["message"]
    assert lines[1]["result"]["unlocked"] is False and lines[2]["error"]["code"] == "locked"


@monorepo.needs_monorepo
def test_the_sidecar_env_allowlist_never_passes_the_test_only_switches_on():
    """DISCONECT_NOW (clock pin) and DISCONECT_KEYCHAIN (keychain off) are for the differential harness only."""
    sidecar = (monorepo.APP / "src-tauri" / "src" / "sidecar.rs").read_text()
    allowlist = re.search(r"const ENV_ALLOWLIST: &\[&str\] = &\[(.*?)\];", sidecar, re.S).group(1)
    names = set(re.findall(r'"([A-Z_]+)"', allowlist))
    assert names and "env_clear()" in sidecar, "the allowlist is a whitelist: everything else is dropped"
    assert not names & {"DISCONECT_NOW", "DISCONECT_KEYCHAIN", keys.PASSPHRASE_ENV, keys.RECOVERY_WORDS_ENV}
