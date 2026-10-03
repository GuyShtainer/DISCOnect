"""The Rust MCP server (``disconect-core mcp``) against the Python one and against the stock SDK clients.

Bet 11e, slice 2. A sibling of ``test_mcp_diff.py`` (which tests the harness against Python) and of
``test_serve_rust.py`` (the same idea for ``serve``): the harness itself is the gate, wrapped here so ``pytest``
runs it.

* ``tools/mcp_diff.py``, Python against the Rust debug binary, at 0 differences on every store of the oracle
  set: synthetic, its schema-v1 twin, empty, the privacy seed, a store that was never imported, and the
  encrypted store **locked** (both exit 9 before reading stdin), plus the encrypted store **unlocked**: Python
  from ``DISCONECT_PASSPHRASE``, Rust from ``--passphrase-file`` (``{PASSPHRASE_FILE}`` in ``--right-cmd``);
* the stock-client leg (``ClientSession`` and ``Client(mode="auto")``) against the Rust command;
* the methods the Rust server answers are the ones the Python server registers, bar the two the handshake-era
  protocol gates away (``server/discover``, ``subscriptions/listen``).
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import mcp_diff  # noqa: E402
import mcp_stock_client  # noqa: E402

import gen_mcp_fixtures  # noqa: E402

CRATE = ROOT / "projects" / "disconect-core"
BINARY = CRATE / "target" / "debug" / "disconect-core"

needs_binary = pytest.mark.skipif(not BINARY.exists(), reason="build projects/disconect-core first (cargo build)")


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
        assert "identical: 13" in report, "locked: both servers exit 9 and write nothing, 13 sessions"
    else:
        assert "identical: 373" in report and "allowed (kb23-pydantic-validation-text, counted apart): 59" in report


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
    assert "identical: 373" in report and "differing: 0" in report


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
