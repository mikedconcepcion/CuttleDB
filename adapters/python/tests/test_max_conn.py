"""Test the --max-conn cap (v1.0.4, Tier 1 DoS defense).

Spins up its own server with --max-conn 4, opens 4 long-lived connections,
verifies the 5th is rejected with `-ERR max_conn`, closes one, verifies a
fresh connection succeeds again.

Skipped unless the binary is available (same pattern as test_wal.py).
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import pytest

from cuttledb import CuttleDB, CuttleDBError


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
def server_max_conn_4():
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port), "--max-conn", "4"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert _wait_listening(port), f"server did not start: {proc.stderr.read()!r}"
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_max_conn_rejects_over_cap(server_max_conn_4):
    """4 connections OK, 5th rejected, close one → 6th OK again."""
    port = server_max_conn_4

    # 4 healthy long-lived connections.
    dbs = []
    for _ in range(4):
        d = CuttleDB.connect("127.0.0.1", port)
        assert d.ping() == "PONG"
        dbs.append(d)

    # 5th must be rejected. The reject path sends `-ERR max_conn\r\n` and
    # closes immediately, so the client either gets an CuttleDBError on the
    # first PING or a clean EOF.
    try:
        rejected = CuttleDB.connect("127.0.0.1", port)
        # Some clients tolerate the close at connect-time; the error
        # surfaces on first verb.
        with pytest.raises((CuttleDBError, OSError, ConnectionError)):
            rejected.ping()
    except (CuttleDBError, OSError, ConnectionError):
        pass  # rejection at connect is also acceptable

    # Close one slot → a fresh connection must succeed.
    dbs[0].close()
    time.sleep(0.05)  # give the server-side cleanup a beat
    fresh = CuttleDB.connect("127.0.0.1", port)
    assert fresh.ping() == "PONG"
    fresh.close()

    for d in dbs[1:]:
        d.close()


def test_max_conn_unset_is_unlimited(tmp_path):
    """Without --max-conn, the cap is 0 (unlimited). 32 simultaneous
    connections must all succeed."""
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port), f"server did not start: {proc.stderr.read()!r}"
        dbs = [CuttleDB.connect("127.0.0.1", port) for _ in range(32)]
        for d in dbs:
            assert d.ping() == "PONG"
        for d in dbs:
            d.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
