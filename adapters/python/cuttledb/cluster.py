"""Cluster — client-side composition over multiple CuttleDB nodes.

A small wrapper class that lets a Python application talk to a fleet of
CuttleDB servers as if it were one logical store. Three patterns:

  1. **Read replicas.** Round-robin reads across all nodes; writes go to
     a designated primary. Combine with the ``cuttledb.replicate`` worker
     to keep replicas in sync.

  2. **Sharding.** A user-supplied ``shard_by(key)`` function picks one
     node per key. Each shard is independent; cross-shard queries are
     N parallel queries + client-side merge.

  3. **Fanout writes.** ``write_to_all`` issues the same write to every
     node — useful for low-write, high-read replicated state when you
     don't want a separate replicator.

This class is intentionally thin. Distribution is a topology choice,
not a database feature, and you compose what you need.

Usage::

    from cuttledb.cluster import Cluster

    # Pattern 1: primary + 2 replicas, sharded reads
    cluster = Cluster.with_primary_and_replicas(
        primary="primary.local:7780",
        replicas=["r1.local:7780", "r2.local:7780"],
    )
    hid = cluster.primary.open()
    tid = cluster.primary.create(hid, "users", [("name", 2)])
    cluster.primary.insert(hid, tid, ["alice"])
    print(cluster.read_round_robin().count(hid, tid))

    # Pattern 2: sharded by user_id
    cluster = Cluster(["s0:7780", "s1:7780", "s2:7780"])
    db = cluster.shard_by(user_id, fn=lambda k: hash(k) % len(cluster.nodes))
    db.insert(hid, tid, [user_id, name])
"""
from __future__ import annotations

import itertools
import threading
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from . import CuttleDB, CuttleDBError


Endpoint = Tuple[str, int]


def _parse(addr: str | Endpoint) -> Endpoint:
    if isinstance(addr, tuple):
        return addr
    host, _, port = addr.partition(":")
    return host, int(port or "7780")


class Cluster:
    """A collection of CuttleDB connections plus routing helpers.

    The cluster does NOT enforce a topology — it just wires together the
    pieces. You decide whether writes are single-primary, fanned-out, or
    sharded; the helpers below cover the common cases.

    Thread-safety: ``Cluster`` itself is thread-safe for the
    round-robin counter. Individual ``CuttleDB`` connections are
    thread-compatible (serialized through their internal lock). For
    parallel reads across nodes, spin one thread per node.
    """

    def __init__(
        self,
        nodes: Sequence[str | Endpoint],
        primary: Optional[str | Endpoint] = None,
        auth: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        if not nodes:
            raise ValueError("Cluster requires at least one node")
        self.endpoints: List[Endpoint] = [_parse(n) for n in nodes]
        self.primary_endpoint: Optional[Endpoint] = _parse(primary) if primary else None
        self.auth = auth
        self.timeout = timeout
        self.nodes: List[CuttleDB] = []
        self._primary: Optional[CuttleDB] = None
        self._rr_idx = 0
        self._lock = threading.Lock()

    # ── Construction patterns ────────────────────────────────────────

    @classmethod
    def with_primary_and_replicas(
        cls,
        primary: str | Endpoint,
        replicas: Iterable[str | Endpoint],
        auth: Optional[str] = None,
    ) -> "Cluster":
        """Convenience factory for Pattern 1 (writes → primary, reads spread)."""
        nodes = [primary, *replicas]
        c = cls(nodes=nodes, primary=primary, auth=auth)
        c.connect()
        return c

    @classmethod
    def sharded(
        cls,
        shards: Iterable[str | Endpoint],
        auth: Optional[str] = None,
    ) -> "Cluster":
        """Convenience factory for Pattern 2 (no replication, key-routed)."""
        c = cls(nodes=list(shards), auth=auth)
        c.connect()
        return c

    # ── Lifecycle ────────────────────────────────────────────────────

    def connect(self) -> None:
        for host, port in self.endpoints:
            self.nodes.append(CuttleDB.connect(host, port, timeout=self.timeout, auth=self.auth))
        if self.primary_endpoint is not None:
            # Re-use the connection that points at the primary endpoint.
            for ep, node in zip(self.endpoints, self.nodes):
                if ep == self.primary_endpoint:
                    self._primary = node
                    break
            if self._primary is None:
                # Primary wasn't in the node list; open a dedicated connection.
                self._primary = CuttleDB.connect(
                    *self.primary_endpoint, timeout=self.timeout, auth=self.auth,
                )

    def close(self) -> None:
        for n in self.nodes:
            try: n.close()
            except Exception: pass
        if self._primary is not None and self._primary not in self.nodes:
            try: self._primary.close()
            except Exception: pass
        self.nodes = []
        self._primary = None

    def __enter__(self) -> "Cluster":
        if not self.nodes:
            self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Access patterns ──────────────────────────────────────────────

    @property
    def primary(self) -> CuttleDB:
        """The designated primary node for writes (Pattern 1).

        Raises if the cluster was constructed without a primary.
        """
        if self._primary is None:
            raise CuttleDBError(
                "no primary configured — use with_primary_and_replicas() "
                "or pass primary= to Cluster()"
            )
        return self._primary

    def read_round_robin(self) -> CuttleDB:
        """Return the next node in round-robin order. Useful for spreading
        read load. Returned ``CuttleDB`` is a live, owned connection — use it
        for one request; don't hold it across many."""
        with self._lock:
            node = self.nodes[self._rr_idx % len(self.nodes)]
            self._rr_idx += 1
        return node

    def shard_by(
        self,
        key: object,
        fn: Optional[Callable[[object, int], int]] = None,
    ) -> CuttleDB:
        """Route to one node by hashing ``key``. The optional ``fn`` takes
        ``(key, node_count)`` and returns the node index; default is
        ``hash(key) % node_count`` (Python hash is unstable across runs —
        provide your own ``fn`` for reproducible sharding)."""
        n = len(self.nodes)
        if fn is None:
            idx = hash(key) % n
        else:
            idx = fn(key, n) % n
        return self.nodes[idx]

    def write_to_all(self, write_fn: Callable[[CuttleDB], object]) -> List[object]:
        """Run ``write_fn(node)`` against every node, return their results.
        Use for low-frequency replicated writes when you don't want a
        separate replicator process.

        If ``write_fn`` raises on any node, the others still complete —
        but the cluster is now inconsistent. Use Pattern 1 (the
        replicator) for high-volume replicated writes.
        """
        results: List[object] = []
        errors: List[Tuple[int, Exception]] = []
        for i, node in enumerate(self.nodes):
            try:
                results.append(write_fn(node))
            except Exception as e:
                errors.append((i, e))
                results.append(None)
        if errors:
            msgs = "; ".join(f"node[{i}]: {e}" for i, e in errors)
            raise CuttleDBError(f"write_to_all partial failure: {msgs}")
        return results

    # ── Diagnostics ──────────────────────────────────────────────────

    def info(self) -> List[dict]:
        """Return ``INFO`` from every node, in node order."""
        return [n.info() for n in self.nodes]

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        return iter(self.nodes)
