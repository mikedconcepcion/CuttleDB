"""Tests for the Cluster client-side composition layer.

These spin up against two independent CuttleDB servers configured by
scripts/test.sh on $CUTTLEDB_CLUSTER_PORT_A and $CUTTLEDB_CLUSTER_PORT_B.
Each test treats the two nodes as a fresh cluster — handles are
allocated per test, no state shared.
"""
from __future__ import annotations

import os
import socket
import pytest

from cuttledb import CuttleDB, CuttleDBError, ColType
from cuttledb.cluster import Cluster


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT_A = int(os.environ["CUTTLEDB_CLUSTER_PORT_A"]) if "CUTTLEDB_CLUSTER_PORT_A" in os.environ else None
PORT_B = int(os.environ["CUTTLEDB_CLUSTER_PORT_B"]) if "CUTTLEDB_CLUSTER_PORT_B" in os.environ else None


def _up(port):
    try:
        s = socket.create_connection((HOST, port), timeout=0.5); s.close(); return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    PORT_A is None or PORT_B is None or not _up(PORT_A) or not _up(PORT_B),
    reason="CUTTLEDB_CLUSTER_PORT_A and _B must point at running servers",
)


@pytest.fixture
def cluster():
    c = Cluster([f"{HOST}:{PORT_A}", f"{HOST}:{PORT_B}"])
    c.connect()
    yield c
    c.close()


def test_construct_and_connect(cluster):
    assert len(cluster) == 2
    infos = cluster.info()
    assert len(infos) == 2
    for info in infos:
        assert "version" in info


def test_round_robin_alternates(cluster):
    a = cluster.read_round_robin()
    b = cluster.read_round_robin()
    c = cluster.read_round_robin()
    assert a is not b           # alternates
    assert a is c               # cycles back


def test_shard_by_is_deterministic(cluster):
    """Same key always routes to the same node within a single process.
    (Python's built-in hash() is per-process; for cross-process stable
    sharding, pass a custom fn — that's tested below.)"""
    n1 = cluster.shard_by("alice")
    n2 = cluster.shard_by("alice")
    assert n1 is n2


def test_shard_by_custom_fn_split(cluster):
    """A round-robin-like custom fn should hit both nodes."""
    seen = set()
    for i in range(10):
        node = cluster.shard_by(i, fn=lambda k, n: int(k) % n)
        seen.add(id(node))
    assert len(seen) == 2  # both nodes hit


def test_write_to_all_fans_out(cluster):
    """write_to_all runs on every node; reading each independently shows the write."""
    hids = cluster.write_to_all(lambda n: n.open())
    assert len(hids) == 2
    # Each node now has the same handle id (both started clean for this test).
    # Create a table on each and verify independent state.
    tids = cluster.write_to_all(
        lambda n: n.create(hids[cluster.nodes.index(n)], "v", [("x", ColType.INT)])
    )
    assert len(tids) == 2
    # Insert a different value on each.
    cluster.nodes[0].insert(hids[0], tids[0], [42])
    cluster.nodes[1].insert(hids[1], tids[1], [99])
    assert cluster.nodes[0].count(hids[0], tids[0]) == 1
    assert cluster.nodes[1].count(hids[1], tids[1]) == 1


def test_primary_required_when_unconfigured(cluster):
    with pytest.raises(CuttleDBError, match="no primary"):
        _ = cluster.primary


def test_primary_and_replicas_factory():
    """The convenience factory wires primary→writes, others→reads."""
    c = Cluster.with_primary_and_replicas(
        primary=f"{HOST}:{PORT_A}",
        replicas=[f"{HOST}:{PORT_B}"],
    )
    try:
        # primary should be available
        hid = c.primary.open()
        tid = c.primary.create(hid, "x", [("v", ColType.INT)])
        c.primary.insert(hid, tid, [1])

        # round-robin returns connections (may be the primary or the replica)
        n = c.read_round_robin()
        assert n is not None
    finally:
        c.close()
