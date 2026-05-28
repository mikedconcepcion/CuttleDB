"""WAL durability + recovery tests.

These are integration tests that start their own server processes — they
need fine control over startup/shutdown to simulate a crash. They live
in their own file (not test_smoke.py) because the rest of the suite
shares a long-running server.

Skipped automatically unless the CuttleDB binary is available.
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

from cuttledb import CuttleDB, CuttleDBError, ColType, Op


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")

pytestmark = pytest.mark.skipif(
    not BINARY or not os.path.isfile(BINARY),
    reason="set CUTTLEDB_SERVER_BIN to point at a cuttledb-server binary",
)


def _free_port() -> int:
    """Grab an ephemeral port. Races are possible but unlikely in tests."""
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
    """Fresh temp dir; removed on teardown."""
    d = tempfile.mkdtemp(prefix="cuttledb-wal-test-")
    yield d
    # Best-effort cleanup — Windows may hold files briefly.
    for _ in range(5):
        try:
            shutil.rmtree(d)
            break
        except OSError:
            time.sleep(0.1)


class _Server:
    def __init__(self, port: int, wal_dir: str, **extra_flags):
        self.port = port
        self.wal_dir = wal_dir
        self.extra = extra_flags
        self.proc = None

    def start(self):
        cmd = [BINARY, "--cuttledb", str(self.port), "--wal-dir", self.wal_dir]
        for k, v in self.extra.items():
            cmd.extend([f"--{k.replace('_', '-')}", str(v)])
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_listening(self.port), f"server did not start: {self.proc.stderr.read()!r}"

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


def test_wal_basic_recovery(wal_dir):
    """Write data, kill, restart — data is back."""
    port = _free_port()

    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "users", [
                ("name",   ColType.STRING),
                ("salary", ColType.INT),
            ])
            db.insert(hid, tid, ["Alice", 100])
            db.insert(hid, tid, ["Bob",   250])
            db.insert(hid, tid, ["Carol", 500])
            assert db.count(hid, tid) == 3
            assert db.sum(hid, tid, 1) == 850

    # Simulate crash — server killed without snapshot.
    assert (Path(wal_dir) / "0.cuttledb-wal").exists()
    assert not (Path(wal_dir) / "0.cuttledb-snap").exists()

    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            assert db.count(0, 0) == 3
            assert db.sum(0, 0, 1) == 850
            assert db.get(0, 0, 0) == ["Alice", "100"]


def test_wal_recovers_with_mutations(wal_dir):
    """Mix INSERT + UPDATE + DELW + INDEX, verify post-recovery state."""
    port = _free_port()

    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "v", [
                ("name", ColType.STRING),
                ("x",    ColType.INT),
            ])
            db.insert_batch(hid, tid, [
                ["a", 10], ["b", 20], ["c", 30], ["d", 40], ["e", 50],
            ])
            db.index(hid, tid, 0)                       # index on name
            db.update_where(hid, tid, 1, 999, 1, Op.GT, 30)  # d,e → 999
            db.delete_where(hid, tid, 1, Op.GT, 100)         # delete the 999s

    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            assert db.count(0, 0) == 3                  # a, b, c left
            assert db.sum(0, 0, 1) == 10 + 20 + 30
            # Index was replayed — FIND should be fast (path tested in smoke;
            # here we just verify correctness).
            assert db.find(0, 0, 0, "a") == [0]
            assert db.find(0, 0, 0, "d") == []           # deleted


def test_wal_truncates_on_checkpoint(wal_dir):
    """When WAL crosses --wal-checkpoint-mb, snapshot is written and WAL
    shrinks. Use a 1-MB threshold so the test triggers quickly."""
    port = _free_port()

    with _Server(port, wal_dir, wal_sync="none", wal_checkpoint_mb=1) as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [
                ("name", ColType.STRING), ("v", ColType.INT),
            ])
            # Each INSERT frame is ~38 bytes after framing. Need >1 MB
            # (1,048,576 bytes) to trigger; ~35,000 inserts is comfortable.
            # Pipelined batches keep wall time short.
            N = 35_000
            for batch_start in range(0, N, 1000):
                rows = [[f"item_{batch_start+i}", batch_start + i]
                        for i in range(1000)]
                db.insert_batch(hid, tid, rows)
            assert db.count(hid, tid) == N

    # Checkpoint fired at least once mid-run.
    assert (Path(wal_dir) / "0.cuttledb-snap").exists(), \
        f"no snapshot in {os.listdir(wal_dir)}"

    # Restart from snapshot + remaining WAL — count must match.
    with _Server(port, wal_dir, wal_sync="none") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            assert db.count(0, 0) == 35_000


def test_wal_committed_tx_survives_restart(wal_dir):
    """A COMMITted tx is durable. Replay reproduces it atomically."""
    port = _free_port()
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [
                ("name", ColType.STRING), ("v", ColType.INT),
            ])
            db.insert(hid, tid, ["pre-tx", 1])
            with db.transaction():
                db.insert(hid, tid, ["in-tx-1", 10])
                db.insert(hid, tid, ["in-tx-2", 20])
            assert db.count(hid, tid) == 3
    # Restart — committed rows present.
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            assert db.count(0, 0) == 3
            assert db.sum(0, 0, 1) == 31


def test_wal_rolled_back_tx_not_in_wal(wal_dir):
    """A ROLLBACKed tx leaves nothing in the WAL. Restart shows pre-tx state."""
    port = _free_port()
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [("v", ColType.INT)])
            db.insert(hid, tid, [100])      # committed implicitly (no tx)
            db.begin()
            db.insert(hid, tid, [200])
            db.insert(hid, tid, [300])
            db.rollback()
            assert db.sum(hid, tid, 0) == 100
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            assert db.count(0, 0) == 1
            assert db.sum(0, 0, 0) == 100


def test_wal_alter_replays(wal_dir):
    """ALTER ADD column is logged + replayed on restart."""
    port = _free_port()
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [("name", ColType.STRING)])
            db.insert(hid, tid, ["alice"])
            db.alter_add(hid, tid, "salary", ColType.INT)
            db.insert(hid, tid, ["bob", 500])
    with _Server(port, wal_dir, wal_sync="always") as srv:
        with CuttleDB.connect("127.0.0.1", port) as db:
            # 2 columns, 2 rows recovered
            assert db.get(0, 0, 0) == ["alice", "0"]
            assert db.get(0, 0, 1) == ["bob", "500"]


def test_wal_disabled_by_default(wal_dir):
    """No --wal-dir = v0.4 ephemeral behavior. WAL files NOT created."""
    port = _free_port()

    # Start without --wal-dir.
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port)
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [("v", ColType.INT)])
            db.insert(hid, tid, [42])
        # No files in our wal_dir.
        assert os.listdir(wal_dir) == []
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)
