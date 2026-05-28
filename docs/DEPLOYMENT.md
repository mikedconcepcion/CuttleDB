# Deploying CuttleDB at scale

> CuttleDB is single-instance native. **Distribution is configuration, not
> code.** This page documents five reference architectures built from the
> primitives already in v0.4. Pick the one that matches your workload
> and compose it from one-binary CuttleDB instances.

The composability hinges on three primitives:

- **`LOG <hid> <tid> [since]`** — per-table change ring buffer with a
  monotonic cursor. Tail this from a worker process to replay changes
  into another CuttleDB.
- **`SUB <hid> <tid>` / `>EVT`** — real-time push subscription. Use for
  millisecond-grain mirroring.
- **`SAVE` / `LOAD`** — binary snapshot transfer. Use to bootstrap a
  fresh replica or archive cold tables.

No new server features are required. The deployments below are running
the same `cuttledb-server` binary you already have.

---

## Pattern 1 — Primary + read replicas

**When:** Read-heavy workload, single writer, you want to spread reads
across several machines without buying a bigger one.

```
            writes ↓
        ┌──────────────────┐
        │  Primary CuttleDB  │
        │  :7780           │
        └────┬─────────────┘
             │ LOG cursor tail
             │ (cuttledb-replicate worker)
             ↓
   ┌─────────┼────────┬────────┐
   ↓         ↓        ↓        ↓
[ Replica 1 ][ Replica 2 ][ Replica 3 ]
   ↑         ↑        ↑
   └─ reads spread by client-side round-robin ─┘
```

**Components**

- 1× primary CuttleDB binary
- N× replica CuttleDB binaries
- 1× `cuttledb-replicate` worker (Python; ships with the repo)
- Client adapters that round-robin reads across replicas

**Bootstrap**

1. Start primary: `cuttledb-server --port 7780`
2. Take a snapshot: `SAVE 0 /tmp/snap.cuttledb` on the primary
3. `scp /tmp/snap.cuttledb` to each replica host
4. Start each replica: `cuttledb-server --port 7780` then `LOAD /tmp/snap.cuttledb`
5. Run `python -m cuttledb.replicate --primary primary:7780 --replicas r1:7780,r2:7780,r3:7780`

**What to monitor**

- Replication lag: replica `INFO`'s `events` count vs primary's
- Replicator process: alive? error rate?
- Round-robin balance: per-replica connection counts

---

## Pattern 2 — Sharded by key

**When:** Data volume exceeds one machine's RAM, or write throughput
exceeds one machine's mutex.

```
        ┌─────────────────────┐
        │   Cluster           │  hash(key) % N
        │   client adapter    │  → server index
        └──┬──────┬──────┬────┘
           ↓      ↓      ↓
       ┌──────┐ ┌──────┐ ┌──────┐
       │Shard0│ │Shard1│ │Shard2│   independent servers
       └──────┘ └──────┘ └──────┘   each owns its key range
```

**Components**

- N× independent CuttleDB binaries (no replication between shards)
- Client uses `Cluster` adapter with a `shard_by(key, fn)` routing function

**Trade-offs**

- **No cross-shard queries.** SUM across shards = N queries + client-side merge.
- **Resharding is manual.** Adding a 4th shard means moving roughly 1/4 of
  the keys; the client does the move, CuttleDB doesn't.
- **Each shard can have its own replicas** (combine with Pattern 1).

**Use when** sharding by tenant, user, region, or any natural partition
where queries rarely cross shards.

---

## Pattern 3 — Geo-replicated reads via WebSocket

**When:** Multi-region, latency-sensitive readers (browser apps, mobile),
single writer in a central region.

```
       ┌───────────────────┐
       │  Primary (US)     │  ← writes from anywhere
       │  WS :7780         │
       └──────┬────────────┘
              │ replication workers in each region
              ↓
   ┌──────────┼──────────┐
   ↓          ↓          ↓
[EU edge]  [AP edge]  [SA edge]   each replica serves local browsers
   ↑          ↑          ↑
   └─ browsers connect over WS ──┘
```

**Components**

- Primary CuttleDB in one region
- Edge CuttleDB instances in each region
- Replicators per edge tailing the primary's LOG
- Browsers connect via WebSocket to their nearest edge

**Why WebSocket here**

The browsers talk to *edges*, not the primary. Edges talk to primary via
TCP for low overhead. WS is the browser-side transport.

**Gotcha**

Writes from a non-primary region still hit the primary — that's the long
trip. This pattern is read-optimized. For write-heavy multi-region,
move to a CRDT-style multi-writer setup (v1.0 roadmap, not built yet).

---

## Pattern 4 — Hot/cold tiering

**When:** Most queries hit recent data (last day/week), but you want to
keep historical data accessible without paying for it in RAM.

```
       writes & recent reads        ad-hoc archival reads
              ↓                              ↓
       ┌──────────────┐                ┌──────────────┐
       │  Hot CuttleDB  │                │  Cold CuttleDB │  (loaded on demand)
       │  last 24h    │   periodic     │  archive     │
       │  in-memory   │  ──SAVE──▶    │  loaded from │
       └──────────────┘   slice         └──────────────┘
                           ↓
                    [filesystem / S3]
```

**Components**

- 1× hot CuttleDB (always running, holds recent window)
- 1× cold CuttleDB (started on demand, loads archived snapshots)
- A nightly job that saves yesterday's data and deletes it from hot
- Client picks hot or cold based on query time range

**Implementation sketch**

```python
# Nightly archive job
with CuttleDB.connect("hot:7780") as hot:
    hot.save(hid, f"/archive/{yesterday}.cuttledb")
    for row_id in old_rows:
        hot.delete(hid, tid, row_id)
```

The hot DB stays small and fast. Cold queries are slow on first hit
(snapshot load) but cached after — pin them in a long-running cold
instance if hits are frequent.

---

## Pattern 5 — Local-first / mobile with sync

**When:** App must work offline, sync to a central store when network
returns. The browser, mobile app, or laptop client carries its own
CuttleDB instance.

```
   [Browser tab]                       [Server]
   ┌──────────────┐                   ┌────────────┐
   │ WASM CuttleDB  │  ◀── WS ──▶       │ CuttleDB     │
   │ (in-process) │   on reconnect    │ central    │
   │  works       │   replay LOG      │ replicas   │
   │  offline     │   ─────────▶     │            │
   └──────────────┘                   └────────────┘
```

**Components**

- Embedded WASM CuttleDB in the client (still experimental in v0.3)
- WS connection to a central CuttleDB
- On reconnect: client sends its local LOG; server replays into the
  shared table; server pushes events client missed back via SUB

**Status**

This pattern is the v1.0 target ("local-first / Gun.js lineage" in the
roadmap). The primitives exist; the sync layer is sketched in
[`examples/browser_realtime.html`](../examples/browser_realtime.html) but
needs a proper conflict-resolution story to be production-ready.

---

## Choosing a pattern

| Symptom | Pattern |
|---|---|
| Reads are slow because too many clients | 1 (replicas) |
| Working set doesn't fit one machine's RAM | 2 (sharded) |
| Browser users are in many regions | 3 (geo replicas) |
| Recent data is hot, historical data is huge | 4 (hot/cold) |
| Users expect the app to work offline | 5 (local-first) |
| Workload exceeds 100k rps on one machine | 1 + 2 (replicated shards) |

Patterns compose. Real deployments are usually `Pattern 2 × Pattern 1`
(sharded with each shard replicated) — same primitives, just more
processes.

## What CuttleDB does not (yet) give you

Be honest with yourself about which problem you have:

- **Strong consistency across replicas.** CuttleDB replication is
  eventually consistent — a write lands on the primary, then propagates.
  If your domain demands "every reader sees the write within X ms," put
  Pattern 1 behind a quorum reader, or wait for v1.0's tighter sync.
- **Cross-shard transactions.** Pattern 2 shards are independent. If you
  need atomic writes across keys living on different shards, pick keys
  that co-locate, or use a single-primary setup.
- **Automatic failover.** No leader election. If a primary dies, the
  operator promotes a replica manually. v1.0 plans peer pairing but no
  Raft.

These aren't bugs; they're the price of being one-binary simple. If
your problem genuinely requires them, CuttleDB is wrong — reach for
Postgres + Citus, CockroachDB, or FoundationDB.

See [ROADMAP.md](ROADMAP.md) for what's planned. The deployment
patterns above are buildable today.
