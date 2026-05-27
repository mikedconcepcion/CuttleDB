"""WAL mid-transaction kill recovery — the durability promise.

When the server is killed between BEGIN and COMMIT, replay on restart
MUST recover to the pre-tx state. Uncommitted INSERT frames in the WAL
must not become visible.

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

from cuttledb import CuttleDB, ColType


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
    d = tempfile.mkdtemp(prefix="cuttledb-wal-midtx-")
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


def test_wal_kill_mid_transaction_replays_to_pre_tx_state(wal_dir):
    """Phase 1: commit some rows + open a tx and INSERT without committing.
    Phase 2 (after kill): server restart MUST NOT show the uncommitted rows."""
    port = _free_port()

    # ── Phase 1: write committed rows, then start tx and DON'T commit.
    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        hid = db.open()
        tid = db.create(hid, "t", [
            ("name", ColType.STRING),
            ("v",    ColType.INT),
        ])
        db.insert(hid, tid, ["committed_1", 100])
        db.insert(hid, tid, ["committed_2", 200])
        assert db.count(hid, tid) == 2

        # Open tx, INSERT, but DO NOT commit.
        db.begin()
        db.insert(hid, tid, ["in_tx_1", 999])
        db.insert(hid, tid, ["in_tx_2", 999])
        db.insert(hid, tid, ["in_tx_3", 999])
        # Close the client first so server-side tx state goes away cleanly.
        db.close()
    finally:
        # Hard kill the server (not graceful terminate) — simulates crash.
        _kill(srv)

    # WAL must exist with no _TXC marker for the open tx.
    assert (Path(wal_dir) / "0.cuttledb-wal").exists()

    # ── Phase 2: restart, query. Committed rows back; in-tx rows gone.
    srv = _start_server(port, wal_dir)
    try:
        db = CuttleDB.connect("127.0.0.1", port)
        # After recovery, handle 0 table 0 has the pre-tx state.
        assert db.count(0, 0) == 2, \
            "uncommitted INSERTs leaked into recovered state"
        assert db.sum(0, 0, 1) == 300  # 100 + 200 only

        # Sanity: the recovered state still accepts new writes.
        db.begin()
        db.insert(0, 0, ["post_recovery", 50])
        db.commit()
        assert db.count(0, 0) == 3
        db.close()
    finally:
        _kill(srv)
