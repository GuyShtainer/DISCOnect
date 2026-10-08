"""The Rust MCP server (``disconect-core mcp``) against the Python one and against the stock SDK clients.

A sibling of ``test_mcp_diff.py`` (which tests the harness against Python) and of
``test_serve_rust.py`` (the same idea for ``serve``): the harness itself is the gate, wrapped here so ``pytest``
runs it.

* ``tools/mcp_diff.py`` (the two-core differential harness, not in this repository), Python against the Rust debug binary, at 0 differences (a result's ``text`` compared
  byte for byte) on every store of the oracle set: synthetic, its schema-v1 twin, empty, the privacy seed, the
  wide store (tiny and huge floats, non-ASCII echoes), a store that was never imported, and the
  encrypted store **locked** (both exit 9 before reading stdin), plus the encrypted store **unlocked**: Python
  from ``DISCONECT_PASSPHRASE``, Rust from ``--passphrase-file`` (``{PASSPHRASE_FILE}`` in ``--right-cmd``);
* the stock-client leg (``ClientSession`` and ``Client(mode="auto")``) against the Rust command;
* the methods the Rust server answers are the ones the Python server registers, bar the two the handshake-era
  protocol gates away (``server/discover``, ``subscriptions/listen``).
"""

from __future__ import annotations

import re

import pytest

import monorepo

mcp_diff = monorepo.harness("mcp_diff")
mcp_stock_client = monorepo.harness("mcp_stock_client")
import gen_mcp_fixtures  # noqa: E402

CRATE = monorepo.CRATE
BINARY = monorepo.BINARY

needs_binary = monorepo.needs_binary


@needs_binary
@pytest.mark.parametrize("name", list(gen_mcp_fixtures.STORES))
def test_the_rust_mcp_and_the_python_one_agree_on_every_store(capsys, name):
    store, keys = gen_mcp_fixtures.STORES[name]
    status = mcp_diff.main(["--store", str(store), "--label", name, "--right-cmd", f"{BINARY} mcp",
                            *(["--keys", str(keys)] if keys else [])])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "differing: 0" in report and "RESULT: 0 differences" in report
    assert "left: store copy unchanged: yes" in report and "right: store copy unchanged: yes" in report
    if name == "encrypted-locked":
        assert "identical: 15" in report, "locked: both servers exit 9 and write nothing, 15 sessions"
    else:
        assert "identical: 393" in report and "allowed (kb23-pydantic-validation-text, counted apart): 59" in report
        assert "allowed (kb23-locked-midsession-text, counted apart): 5" in report


@needs_binary
@pytest.mark.parametrize("name", list(gen_mcp_fixtures.HOSTILE))
def test_the_hostile_corpora_run_alike_on_both_servers(capsys, name):
    """Review 11e: conformance corners (batches, float ids, odd params, notifications with ids), lone-surrogate and
    injection payloads, and 385 coercion probes beyond the table; one line at a time, so Python's concurrent answers
    cannot reorder what is compared."""
    store, _ = gen_mcp_fixtures.STORES[gen_mcp_fixtures.HOSTILE_STORE]
    script = gen_mcp_fixtures.hostile_script_path(name)
    status = mcp_diff.main(["--store", str(store), "--label", f"hostile-{name}", "--script", str(script),
                            "--right-cmd", f"{BINARY} mcp"])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "differing: 0" in report and "RESULT: 0 differences" in report
    entries = sum(len(session["entries"]) for session in gen_mcp_fixtures.hostile_script(name)["sessions"])
    assert f"{entries} script entries" in report and entries >= {"conformance": 42, "injection": 9,
                                                                 "coercions": 385}[name]


@needs_binary
def test_the_encrypted_store_unlocked_agrees_too(capsys, tmp_path):
    store, keys = gen_mcp_fixtures.STORES["encrypted-locked"]
    passphrase = tmp_path / "passphrase.txt"
    passphrase.write_text(gen_mcp_fixtures.ENCRYPTED_PASSPHRASE + "\n")
    status = mcp_diff.main(["--store", str(store), "--keys", str(keys), "--label", "encrypted-unlocked",
                            "--passphrase-file", str(passphrase),
                            "--right-cmd", f"{BINARY} mcp --passphrase-file {mcp_diff.PASSPHRASE_SLOT}"])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "identical: 398" in report and "differing: 0" in report
    assert "allowed (kb23-locked-midsession-text, counted apart): 0" in report, "unlocked: the swap changes nothing"


@needs_binary
@pytest.mark.parametrize("store", [gen_mcp_fixtures.PRIVACY_SEED, gen_mcp_fixtures.STORES["synthetic"][0],
                                   gen_mcp_fixtures.STORES["empty"][0]])
def test_the_stock_client_leg_passes_against_the_rust_server(capsys, store):
    status = mcp_stock_client.main(["--store", str(store), "--cmd", f"{BINARY} mcp"])
    report = capsys.readouterr().out
    assert status == 0, report
    assert "12 tool calls, 0 problems" in report


def test_the_rust_server_answers_the_methods_the_python_one_registers():
    from disconect import mcp_server
    python = set(mcp_server.server._lowlevel_server._request_handlers) - {"server/discover", "subscriptions/listen"}
    source = (CRATE / "src" / "mcp.rs").read_text()
    table = source.split("const HANDLED", 1)[1].split("];", 1)[0]
    assert set(re.findall(r'"([a-z/A-Z]+)"', table)) == python
