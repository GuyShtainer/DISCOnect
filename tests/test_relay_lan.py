# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LAN relay across the two cores: the Rust core serves a relay folder over HTTP on
127.0.0.1 (``disconect-core relay-serve``); a Rust device reaches it over the LAN, a Python device reads
the same folder directly (the server's folder IS the folder relay, and Python has no LAN transport).
After the rounds the daily rows of every device are equal and the replay of the raw set changes none.
Synthetic data only; every listener binds 127.0.0.1."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from disconect import cli
from disconect.relay.folder import FolderRelay
from test_converge_10b import _export
from test_converge_10b_mixed_core import (Device, Fleet, _assert_rust_reparse_quiet, _converged, _quiet)
import monorepo

monorepo.require()
from test_core_parity import BINARY, _rust_env  # noqa: E402

needs_binary = monorepo.needs_binary
pytestmark = needs_binary


class RelayServe:
    """``disconect-core relay-serve`` as a child: serves ``folder`` for the account of ``device``'s key."""

    def __init__(self, device: Device, folder: pathlib.Path):
        self.proc = subprocess.Popen(
            [str(BINARY), "--db", str(device.db), "relay-serve", "--relay", str(folder), "--listen", "127.0.0.1:0"],
            env=_rust_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first = self.proc.stdout.readline().strip()
        assert first.startswith("listening http://127.0.0.1:"), first
        self.url = first.removeprefix("listening ")

    def close(self) -> str:
        self.proc.kill()
        self.proc.wait(timeout=10)
        return self.proc.stderr.read()


@pytest.fixture
def fleet(tmp_path):
    fleet = Fleet(tmp_path)
    yield fleet
    fleet.close()


def _rust_lan(device: Device, action: str, url: str) -> None:
    Fleet._rust(device, "sync", action, "--relay", url)


def test_a_python_folder_device_and_a_rust_lan_device_converge(tmp_path, fleet):
    python_seat, rust_seat = fleet.device("a", "py"), fleet.device("b", "rs")
    fleet.do_import(python_seat, _export(tmp_path / "export_x", "X"))
    fleet.do_import(rust_seat, _export(tmp_path / "export_y", "Y"))
    folder = tmp_path / "relay"
    server = RelayServe(rust_seat, folder)
    try:
        folder_relay = FolderRelay(folder, create_root=True)
        _rust_lan(rust_seat, "push", server.url)               # Rust -> LAN -> the served folder
        fleet.pull(python_seat, folder_relay)                  # Python reads that folder directly
        fleet.push(python_seat, folder_relay)                  # Python writes the folder directly
        _rust_lan(rust_seat, "pull", server.url)               # Rust reads it over the LAN
    finally:
        log = server.close()
    rows = _converged([python_seat, rust_seat])
    assert rows, "the devices converged on something"
    _quiet([python_seat, rust_seat])
    # the server logged method and status only: no object name, no address, no path
    assert log.split() and all(token in {"GET", "PUT", "DELETE"} or token.isdigit() for token in log.split()), log


def test_a_ring_of_two_rust_devices_and_one_python_device_mixes_lan_and_folder(tmp_path, fleet):
    lan_seat, python_seat, folder_seat = (fleet.device("a", "rs"), fleet.device("b", "py"), fleet.device("c", "rs"))
    for device, name in zip((lan_seat, python_seat, folder_seat), "XYZ"):
        fleet.do_import(device, _export(tmp_path / f"export_{name}", name))
    folder = tmp_path / "relay"
    server = RelayServe(lan_seat, folder)
    try:
        relay = FolderRelay(folder, create_root=True)
        _rust_lan(lan_seat, "push", server.url)                # a: LAN
        fleet.pull(python_seat, relay)                         # b: folder
        fleet.push(python_seat, relay)
        fleet.pull(folder_seat, relay)                         # c: folder
        fleet.push(folder_seat, relay)
        for _ in range(2):
            _rust_lan(lan_seat, "pull", server.url)
            _rust_lan(lan_seat, "push", server.url)
            fleet.pull(python_seat, relay)
            fleet.push(python_seat, relay)
            fleet.pull(folder_seat, relay)
            fleet.push(folder_seat, relay)
        _rust_lan(lan_seat, "pull", server.url)
    finally:
        server.close()
    _converged([lan_seat, python_seat, folder_seat])
    _quiet([lan_seat, python_seat, folder_seat])


def test_a_device_with_another_master_is_unverified_and_the_python_cli_has_no_lan_transport(tmp_path, fleet, capsys):
    seat = fleet.device("a", "rs")
    (tmp_path / "elsewhere").mkdir()
    foreign_fleet = Fleet(tmp_path / "elsewhere")   # its own master: another account, another token key
    stranger = foreign_fleet.device("s", "rs")
    server = RelayServe(seat, tmp_path / "relay")
    try:
        fleet.do_import(seat, _export(tmp_path / "export_x", "X"))
        _rust_lan(seat, "push", server.url)
        refused = subprocess.run([str(BINARY), "--db", str(stranger.db), "sync", "pull", "--relay", server.url],
                                 env=_rust_env(), capture_output=True, text=True, timeout=60)
        assert refused.returncode == 5 and "relay_unverified" in refused.stderr, refused.stderr
        # the Python CLI reads folders only: a usage error, never a connection attempt
        assert cli.main(["--db", str(seat.db), "sync", "pull", "--relay", server.url]) == cli.EXIT_USAGE
        assert "a LAN relay needs disconect-core" in capsys.readouterr().err
    finally:
        server.close()
        foreign_fleet.close()
