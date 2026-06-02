"""WAL replay of committed DDL-in-tx — v0.8.0 durability.

A CREATE / ALTER / INDEX committed inside a transaction is flushed to the WAL
inside the same _TXB/_TXC batch as the surrounding DML. On restart, replay
MUST reconstruct the table, the added column (with its value), and leave the
index queryable. Uncommitted DDL must NOT survive a crash.

Skipped unless the CuttleDB binary is available.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cuttledb import ColType, CuttleDB


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")

pytestmark = pytest.mark.skipif(
    not BINARY or not os.path.isfile(BINARY),
    reason="set CUTTLEDB_SERVER_BIN to point at a cuttledb-server binary",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


@pytest.fixture
def wal_dir():
    d = tempfile.mkdtemp(prefix="cuttledb-wal-ddltx-")
    yield d
    for _ in range(5):
        try:
            shutil.rmtree(d)
            break
        except OSError:
            time.sleep(0.1)


def _start_server(port: int, wal_dir: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port), "--wal-dir", wal_dir,
         "--wal-sync", "always"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert _wait_listening(port), \
        f"server did not start: {proc.stderr.read()!r}"
    return proc


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=2.0)


def test_committed_ddl_in_tx_replays(wal_dir):
    """CREATE + ALTER + INDEX committed in a tx must reconstruct on restart."""
    port = _free_port()
    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        hid = db.open()
        db.begin()
        tid = db.create(hid, "t", [
            ("name", ColType.STRING),
            ("v",    ColType.INT),
        ])
        db.insert(hid, tid, ["alpha", 11])
        db.insert(hid, tid, ["beta", 22])
        wcol = db.alter_add(hid, tid, "w", int(ColType.INT))
        db.update_row(hid, tid, 0, wcol, 99)
        db.index(hid, tid, 0)
        db.commit()
        db.close()
    finally:
        _kill(srv)

    # Restart and verify the whole committed DDL survives.
    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        assert db.count(0, 0) == 2
        assert db.sum(0, 0, 1) == 33          # 11 + 22
        assert db.sum(0, 0, 2) == 99          # added column, row 0 = 99
        assert db.find(0, 0, 0, "alpha") == [0]  # index queryable after replay
        db.close()
    finally:
        _kill(srv)


def test_uncommitted_ddl_in_tx_does_not_survive(wal_dir):
    """A CREATE opened in a tx but never committed must vanish on restart."""
    port = _free_port()
    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        hid = db.open()
        # One committed table so the WAL/state is non-empty.
        committed = db.create(hid, "keep", [("k", ColType.INT)])
        db.insert(hid, committed, [1])
        # Now open a tx, create a table + add a column, but DO NOT commit.
        db.begin()
        ghost = db.create(hid, "ghost", [("g", ColType.INT)])
        db.insert(hid, ghost, [7])
        db.close()  # drop client; server tx state dies uncommitted
    finally:
        _kill(srv)

    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        # The committed table is back.
        assert db.count(0, 0) == 1
        # The ghost table (tid 1) must not exist after replay.
        with pytest.raises(Exception):
            db.sum(0, 1, 0)
        db.close()
    finally:
        _kill(srv)
