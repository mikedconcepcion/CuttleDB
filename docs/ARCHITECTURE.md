# CuttleDB — architecture

A short consumer-facing tour. The wire-protocol reference is
[`../PROTOCOL.md`](../PROTOCOL.md); the per-feature surface is
[`FEATURES.md`](./FEATURES.md); deployment + ops is
[`DEPLOYMENT.md`](./DEPLOYMENT.md).

## What CuttleDB is, in one frame

CuttleDB ships as a **single self-contained server binary** that
speaks a Redis-style line protocol over **TCP or WebSocket**. Clients
in any language can implement the protocol in a few hundred lines;
the Python and JS adapters in this repo are one reference each.
There are no external runtime dependencies — no JVM, no Python on
the server side, no Redis, no SQLite library, no Vulkan unless you
build with GPU compute. One binary, one port.

## The layer cake

```
                ┌───────────────────────────────────┐
                │   Client SDKs (Python, JS)        │
                │   adapters/{python,*.js,*.d.ts}   │
                └────────────────┬──────────────────┘
                                 │  wire protocol
                                 │  (line-based, Redis-style)
                                 │  over TCP / WebSocket
                ┌────────────────▼──────────────────┐
                │   cuttledb-server (one binary)    │
                │                                   │
                │   ├── connection handler          │
                │   │   (thread-per-conn, --max-conn cap,
                │   │    rate limit, /health, /metrics, TLS)
                │   │                               │
                │   ├── wire dispatcher             │
                │   │   (verbs: KNN, LSEARCH, SEARCH, BSEARCH,
                │   │    INSERT, GET, COUNT, SUM, MIN, MAX,
                │   │    UPDATE, DELETE, BEGIN/COMMIT/ROLLBACK,
                │   │    SUB/UNSUB/LOG, INDEX, FIND, AUTH,
                │   │    TOKEN, AUDIT, ALTER, DATETIME,
                │   │    GROUPBY, JOIN, SAVE, LOAD, ...)
                │   │                               │
                │   ├── columnar engine             │
                │   │   (INT, FLOAT, STRING, VEC, DATETIME
                │   │    columns; AVX2 SIMD scans;
                │   │    O(1) COUNT + SUM; HNSW + BM25 indexes)
                │   │                               │
                │   ├── WAL durability              │
                │   │   (CRC32 frames; mid-tx kill
                │   │    recovery; configurable sync mode;
                │   │    snapshot checkpoint)
                │   │                               │
                │   └── observability               │
                │       (audit log NDJSON, slow-query
                │        log NDJSON, Prometheus /metrics)
                └───────────────────────────────────┘
```

## Data model

A CuttleDB **handle** owns one or more **tables**. A table is a
set of typed columns:

| Column type | Storage | Notes |
|---|---|---|
| `INT` | row-aligned `int64` array | range queries via SIMD predicate scan |
| `FLOAT` | row-aligned `double` array | same SIMD path |
| `STRING` | interned handles into a per-handle string pool | BM25 index optional |
| `VEC` | row-aligned packed `float32` of fixed dimension | HNSW index optional |
| `DATETIME` | `int64` epoch ms UTC, stored as f64 (53-bit mantissa = exact dates ±285,000 years) | INSERT/predicate accept ISO 8601 strings or raw ms |

Cached running aggregates per numeric column make `COUNT` and `SUM`
O(1) regardless of row count. Other aggregates (`MIN`/`MAX`/`FCOUNT`)
run a SIMD lane over the column — sub-millisecond at typical sizes.

## Storage model

By default, all state is in-process memory. Optionally:

- **WAL** (`--wal-dir <path>`) — every write is logged as a framed
  binary record with CRC32. On restart, the WAL is replayed to
  reconstruct state. Sync mode is configurable: `always` (fsync per
  commit), `interval=N` (default 20 ms; bounded data loss window),
  or `none` (best-effort, fastest).
- **Snapshot** (`SAVE` / `LOAD`) — operator-triggered binary
  snapshots of a handle's full state. Works alongside WAL; the WAL
  is checkpointed when it crosses a size threshold and the snapshot
  becomes the new replay base.

Crash recovery (kill server mid-transaction → restart) is exercised
by `adapters/python/tests/test_wal_mid_tx.py`. Uncommitted writes do
not survive; committed writes do.

## Retrieval model

Five modes, all served by the same binary, all dispatched through
the wire protocol:

| Mode | Verb | What it does |
|---|---|---|
| **Vector KNN** | `KNN <hid> <tid> <col> <k> <vec>` | AVX2 cosine top-k. Brute force at small scale; auto-routes through HNSW above the threshold. |
| **Filtered KNN** | `KNN ... WHERE col OP value AND ...` | KNN with predicates AND'd in. HNSW oversamples 4× to keep `k` results after filtering. |
| **BM25 lexical** | `LSEARCH <hid> <tid> <col> <k> <query>` | BM25 index over a STRING column. Lucene defaults (`k1=1.5, b=0.75`). |
| **Hybrid (RRF)** | `SEARCH <hid> <tid> <vec_col> <text_col> <k> <vec> \|\|\| <query>` | KNN + BM25 fused via Reciprocal Rank Fusion. One wire roundtrip; one result list. |
| **Boolean DSL** | `BSEARCH <hid> <tid> <k> <expr>` | Compose vector scoring atoms (`col~V[...]`), BM25 atoms (`col~"phrase"`), and predicates (`col>5`) with AND / OR / parens. |

Benchmark for the HNSW vs brute-force comparison:
[`../bench/HNSW_BENCH.md`](../bench/HNSW_BENCH.md). Methodology +
SQLite comparison: [`../bench/RESULTS.md`](../bench/RESULTS.md).

## Real-time push

`SUB <hid> <tid>` registers a subscription on a table. Every INSERT
/ UPDATE / DELETE on that table produces a `>EVT` line on every
subscribed socket. Subscribers either:

- **Stream**: `db.stream_events()` yields events as they arrive.
- **Poll**: `db.poll_events(timeout)` drains the pending queue.
- **Tail**: `LOG <hid> <tid> [since]` reads from a per-table ring
  buffer (last 1024 events) — useful for catch-up after a
  disconnect.

## Security surface

- **AUTH** — multi-token: a root token via `--auth <token>` plus
  runtime-minted tokens via `TOKEN ADD`. Each token has an id;
  audit log entries attribute every command to a token id.
- **TLS** — `--tls-cert` + `--tls-key` flags; requires
  `CUTTLEDB_WITH_TLS=1` build. RSA cert + server-side handshake.
  EC keys and mTLS are not shipped yet (on the roadmap).
- **Connection cap** — `--max-conn N` rejects new sockets beyond a
  cap (atomic counter; DoS defense).
- **Rate limit** — `--rate-limit N` per-connection commands/sec.
- **Idle timeout** — `--idle-timeout-ms N` drops sockets that go
  quiet (slow-loris defense).
- **Audit log** — `--audit-dir <dir>` writes NDJSON one-line-per-
  dispatched-command with `{ts, verb, token_id, fd, ok}`.
- **HTTP `/health`** — same port; pre-auth; k8s liveness/readiness
  probe target.
- **Prometheus `/metrics`** — same port; pre-auth; 9 series
  (counters: connections / commands / errors / max-conn-rejects;
  gauges: uptime / active-conns / handles / tables / subscribers).

Full disclosure policy: [`../SECURITY.md`](../SECURITY.md).

## What's NOT in this architecture

Listing it explicitly because the omissions are deliberate:

- **No SQL parser.** The wire protocol stays Redis-style. SQL is an
  adapter-level concern; CuttleDB doesn't bundle one.
- **No graph type.** Coming in v1.0; tracked in
  [`ROADMAP.md`](./ROADMAP.md).
- **No native CRDT / distributed sync.** Composition pattern via
  `Cluster` + `cuttledb.replicate` (client-side). Native sync
  arrives as a future companion layer.
- **No built-in inference / model hosting.** CuttleDB is the
  substrate for agents and ML adapters, not a model host.

The omissions are part of the design — the substrate is small on
purpose. Layers above can be built; CuttleDB doesn't try to be
them all.
