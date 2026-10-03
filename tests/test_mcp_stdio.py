"""The packaging gate: the installed ``disconect-mcp`` script answers over stdio.

Every other MCP test calls the server in-process. This one spawns the console
script the editable install provides, exactly as Claude Desktop would, and
checks the tool list and one structured answer. HOME and DISCONECT_DB point at
the test's temp dir so a misconfigured child can never reach a real database.
"""

import asyncio
import pathlib
import sys
import sysconfig

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from test_privacy import FORBIDDEN_TEXT, SERIAL, _seed, _walk

SCRIPT = pathlib.Path(sysconfig.get_path("scripts")) / ("disconect-mcp.exe" if sys.platform == "win32" else "disconect-mcp")


async def _round_trip(db_path, home, errlog):
    params = StdioServerParameters(command=str(SCRIPT), args=[],
                                   env={"DISCONECT_DB": str(db_path), "HOME": str(home), "USERPROFILE": str(home),
                                        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
    with anyio.fail_after(30):
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                result = await session.call_tool("get_data_health", {"window_days": 60})
                return names, result


@pytest.mark.skipif(not SCRIPT.exists(), reason="disconect-mcp script not installed in this interpreter")
def test_console_script_round_trip(db_path, tmp_path):
    _seed(db_path)
    from disconect import mcp_server
    expected = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    with open(tmp_path / "server.err", "w") as errlog:
        names, result = anyio.run(_round_trip, db_path, tmp_path, errlog)
    assert names == expected
    assert not result.is_error, result.content
    payload = result.structured_content
    assert payload["coverage"]["window"]["days"] == 60 and payload["coverage"]["statuses"][0] == "present"
    for path, text in _walk(payload):
        for needle in FORBIDDEN_TEXT:
            assert needle not in text, f"{needle!r} leaked at {path}"
    assert SERIAL not in str(payload)


@pytest.mark.skipif(not SCRIPT.exists(), reason="disconect-mcp script not installed in this interpreter")
def test_console_script_serves_an_encrypted_store(db_path, tmp_path, monkeypatch):
    """The server unlocks once at startup from the (test-only) env passphrase and never prompts."""
    import os
    from disconect import cli
    from disconect.storage import keys
    _seed(db_path)
    keys.set_kdf_params(None)   # the child process enforces the production KDF floor on the key file it reads
    os.environ[keys.PASSPHRASE_ENV] = "a perfectly fine passphrase"
    assert cli.main(["--db", str(db_path), "key", "init"]) == 0
    from disconect import storage
    assert storage.is_encrypted_file(db_path) is True
    params_env = {"DISCONECT_DB": str(db_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
                  "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
                  keys.PASSPHRASE_ENV: "a perfectly fine passphrase"}

    async def _go(errlog):
        params = StdioServerParameters(command=str(SCRIPT), args=[], env=params_env)
        with anyio.fail_after(30):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15) as session:
                    await session.initialize()
                    return await session.call_tool("get_data_health", {"window_days": 60})
    with open(tmp_path / "server.err", "w") as errlog:
        result = anyio.run(_go, errlog)
    assert not result.is_error, result.content
    assert result.structured_content["coverage"]["window"]["days"] == 60
    assert "a perfectly fine passphrase" not in (tmp_path / "server.err").read_text()
    # without any unlock path the server refuses to start instead of hanging on a prompt
    import subprocess
    proc = subprocess.run([str(SCRIPT)], env={"DISCONECT_DB": str(db_path), "HOME": str(tmp_path), "PATH": os.environ["PATH"],
                               "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"},
                          input=b"", capture_output=True, timeout=30)
    assert proc.returncode == 9 and b"key cache" in proc.stderr


def test_every_mcp_tool_works_with_no_network(db_path, monkeypatch, no_network):
    """The same tool calls as the privacy walk, with connect() and name resolution made to fail."""
    from test_privacy import CALLS
    _seed(db_path)
    monkeypatch.setenv("DISCONECT_DB", str(db_path))
    from disconect import mcp_server
    for tool, args in CALLS:
        result = asyncio.run(mcp_server.server.call_tool(tool, args))
        assert not result.is_error, tool
