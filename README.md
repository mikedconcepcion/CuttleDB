# CuttleDB

[![PyPI](https://img.shields.io/pypi/v/cuttledb?label=PyPI&color=ff7849)](https://pypi.org/project/cuttledb/)
[![npm](https://img.shields.io/npm/v/cuttledb?label=npm&color=ff7849)](https://www.npmjs.com/package/cuttledb)
[![CI](https://img.shields.io/github/actions/workflow/status/mikedconcepcion/CuttleDB/ci.yml?branch=main&label=CI)](https://github.com/mikedconcepcion/CuttleDB/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![sigstore](https://img.shields.io/badge/sigstore-signed-blueviolet)](SECURITY.md#verifying-a-release-binary)
[![Platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-lightgrey)](#install)

> **CuttleDB — an embedded realtime database with vector search, WAL
> durability, and event streaming. One self-contained binary, no
> external runtime dependencies.**
>
> Five-mode retrieval (KNN, BM25, RRF hybrid, Boolean DSL, filtered
> KNN), real-time SUB/UNSUB push, ACID transactions, TLS, and an
> audit log. Releases are sigstore-signed.

📖 **Docs site:** [mikedconcepcion.github.io/CuttleDB](https://mikedconcepcion.github.io/CuttleDB)
📦 **Install:** `pip install cuttledb` &middot; `npm install cuttledb` &middot; [binary releases](https://github.com/mikedconcepcion/CuttleDB/releases/latest)

```bash
$ cuttledb-server --port 7780
CuttleDB server listening on tcp://0.0.0.0:7780

# Production: gate access, throttle, cap concurrent connections,
# log slow queries to a structured file, durable WAL:
$ cuttledb-server --port 7780 \
    --auth $CUTTLEDB_TOKEN \
    --rate-limit 1000 \
    --max-conn 512 \
    --slow-log-ms 5 --slow-log-file /var/log/cuttledb \
    --audit-dir /var/log/cuttledb/audit \
    --wal-dir /var/lib/cuttledb/wal
```

Copy the binary, run it. No external runtime to install.

## Current Status (v0.8)

CuttleDB is **production-orientation, surface still expanding**. The
substrate is real and tested; the relational + retrieval surface is
broad as of v0.8; v1.0 ships when graph types and distributed sync
arrive.

**Stable** (shipped, tested, durable):

- Relational tables (INT, FLOAT, STRING, VEC, DATETIME columns)
- Hybrid retrieval — KNN, LSEARCH (BM25), SEARCH (RRF fusion),
  BSEARCH (Boolean DSL), filtered KNN (`KNN ... WHERE`)
- HNSW ANN index for VEC columns (12.7× faster than brute force at
  100K × 128)
- ACID transactions: `BEGIN` / `COMMIT` / `ROLLBACK`
- Write-ahead log with CRC32 frames + crash-recovery (including
  mid-transaction kill replay — pinned by integration tests)
- Real-time push: `SUB` / `UNSUB` / `LOG` per-table change feed
- Aggregations: O(1) `COUNT`/`SUM`; SIMD `MIN`/`MAX`/`FCOUNT`;
  `GROUPBY` with COUNT/SUM/MIN/MAX/AVG, multi-column `BY` (tuple
  keys), `HAVING`, `ORDER`, `LIMIT`
- Joins (`JOIN` wire verb): hash equi-join (O(N+M)), non-equi
  `GT`/`LT`, and `left`/`right`/`full` outer joins (-1 NULL sentinel)
- Composite secondary indexes + `FINDC` (multi-column exact lookup
  in one wire call)
- String-column `UPDATE` (`UPDRS` by row id, `UPDATES` by predicate)
- DDL inside transactions (`CREATE` / `INDEX` / `ALTER` commit or
  roll back atomically with row mutations)
- Multi-token auth (`TOKEN ADD`/`LIST`/`REVOKE`), audit log
  (NDJSON per UTC day), rate limit, idle timeout, slow-query log
  (NDJSON, day-rotated), `--max-conn` DoS cap, HTTP `/health` probe,
  Prometheus `/metrics` endpoint
- TLS (RSA cert + server-side, `CUTTLEDB_WITH_TLS=1` build flag)
- Multi-platform CI: Linux + macOS + Windows × Python 3.10/3.12

**Experimental** (works, but contract may evolve):

- ML wire verbs — `MATMUL`, `MATMUL_B` (binary-framed), `FLASH_ATTN_B`
  (server-side matmul + attention; useful for ML adapters)
- `Cluster` adapter + `cuttledb.replicate` companion for client-side
  composition (no native server-side cluster)

**Not yet implemented** (on the v0.8+ / v1.0 path):

- Graph types + traversal (`MATCH` verb)
- Native CRDT / distributed sync
- Mutual TLS (mTLS), EC private keys, cipher allow-list, cert hot-reload
- Reproducible-build attestation
- `SELECT AS OF <ts>` temporal queries (substrate ready, surface absent)
- Predicate-filtered `SUB` (substrate ready, surface absent)
- GPU HNSW index (substrate present; index lives on CPU today)

See [docs/ROADMAP.md](docs/ROADMAP.md) for the path to v1.0.

## Why it exists

Most modern application stacks pull in three or four databases to do
what one process can do at the substrate level:

- A KV store for low-latency state (Redis ~5 MB)
- An embedded relational store for structured data (SQLite ~1 MB)
- A vector index for retrieval (Pinecone, Qdrant ~50 MB)
- A full-text engine (Elasticsearch ~hundreds of MB)
- A pub/sub broker for real-time updates
- A WAL or log layer for durability

Each is its own deploy, monitor, version, and security surface.
CuttleDB collapses these into one binary that runs as a TCP /
WebSocket server, with a uniform Redis-style line protocol and
trivial SDKs in any language. Trade-off: smaller community, less
ecosystem tooling, no SQL parser.

---

## What it solves

| Problem | How CuttleDB handles it |
|---|---|
| **Embedded apps want a real DB, not localStorage** | One binary; TCP or WebSocket from a browser. Same data model in every transport. |
| **Aggregates over thousands of rows have to be fast** | AVX2 SIMD predicate scans + cached O(1) aggregates. `COUNT`/`SUM` are constant-time; `MIN`/`MAX`/`FCOUNT` are sub-millisecond. On 1K-row aggregate workloads, faster than SQLite `:memory:` despite paying for TCP round-trips — see [bench/RESULTS.md](bench/RESULTS.md) for the methodology and the SQLite INSERT loss we don't paper over. |
| **Polling for changes wastes cycles** | `SUB` / `UNSUB` real-time push. Every insert/update/delete lands on subscribed clients in microseconds. |
| **Full-text search needs a separate service** | First-class `BM25` index + `LSEARCH` verb. No Elasticsearch, no Meilisearch, no separate process. |
| **Vector search needs another separate service** | First-class `VEC` columns + `KNN`/HNSW. Top-10 over 10,000 embeddings in 2ms; 100K with HNSW in <1ms. No Pinecone. |
| **Hybrid ranking should be one call, not three** | `SEARCH` fuses vector + BM25 via Reciprocal Rank Fusion in lockstep. Same wire roundtrip. |
| **Long-running workers need to catch up after disconnect** | `LOG` per-table ring buffer + cursor tail. Replay the last 1024 events. |
| **Multiple writers/readers need to coordinate** | Multi-client TCP server, thread-per-conn, mutex-serialized writes, transactions (`BEGIN`/`COMMIT`/`ROLLBACK`). |
| **You don't want your data on someone else's machine** | Local-first by design. No cloud, no telemetry, no API key, no rate limits. |

See [docs/WHY_CUTTLEDB.md](docs/WHY_CUTTLEDB.md) for the full use-case
breakdown.

---

## Four features that matter

1. **Substrate-level speed.** Column store with running aggregates.
   COUNT and SUM are O(1). MIN/MAX/FCOUNT are AVX2 SIMD scans.
   SELECT WHERE runs entirely in C. On 1K-row aggregate workloads,
   faster than SQLite `:memory:` despite paying for TCP round-trips —
   see [bench/RESULTS.md](bench/RESULTS.md) for the bench table and
   the SQLite INSERT loss we don't paper over.

2. **Real-time push.** `SUB <hid> <tid>` registers your client for
   change events. Every mutation triggers a `>EVT` line on every
   subscribed socket. Subscribers react instead of polling — UIs
   update on write, workers process events on arrival, agents react
   to state changes.

3. **Vector search.** `VEC` columns store fixed-dim f32 embeddings as
   a packed buffer. `KNN <k> <query>` does AVX2 cosine similarity +
   partial sort in one C call. Top-10 over 10K vectors: 2ms. For
   larger corpora, `INDEX <hid> <tid> <col> HNSW` builds an HNSW ANN
   index in-place; KNN queries on that column auto-route through it.
   At 100K × 128 dim: **12.7× faster than the SIMD brute-force
   baseline** (1 ms / query). Index persists in snapshots;
   INSERT/DELETE maintain it incrementally.

4. **Change feed.** Per-table ring buffer (last 1024 events) with a
   monotonic cursor. `LOG <hid> <tid> [since]` returns events since
   the cursor. Long-running workers can disconnect, reconnect, and
   replay without missing changes.

Full feature breakdown: [docs/FEATURES.md](docs/FEATURES.md).

---

## Benchmarks

All benchmark scripts live in [`bench/`](./bench) and reproduce from
a clean checkout. The full methodology + measured numbers are in
[**bench/RESULTS.md**](./bench/RESULTS.md) — read it before quoting
any specific multiplier, because the SQLite comparison runs SQLite
in-process against CuttleDB over TCP loopback (deliberate, matches
deployment shape; still asymmetric on the latency axis and the
results document explains the asymmetry first).

Two reproducible scripts ship today:

- **`bench/bench_sqlite.py`** — CuttleDB (TCP) vs SQLite (in-process)
  on 1K-row aggregate workloads. Results: SQLite wins bulk INSERT
  8.4× (TCP overhead dominates a small in-memory load); CuttleDB
  wins SUM / MIN / COUNT / SELECT WHERE by 1.4–1.8× despite the
  network handicap.
- **`bench/bench_hnsw.py`** — HNSW vs brute-force KNN. Apples-to-
  apples (both inside CuttleDB). At 100K × 128 dim: HNSW **12.7×**
  faster than the AVX2+FMA SIMD brute-force baseline, recall@10 = 1.0.
  Full table in [`bench/HNSW_BENCH.md`](./bench/HNSW_BENCH.md).

What we don't yet benchmark (with reproducible scripts) — deferred
to v0.8+ per [`bench/RESULTS.md`](./bench/RESULTS.md) § 4:

- CuttleDB-over-TCP vs Redis-over-TCP (the fair comparison on the
  INSERT axis; both sides pay the socket cost)
- Stress workloads (subscriber fan-out, WAL throughput, recovery time)
- Cold-start latency + idle memory footprint

---

## Quickstart — Docker (server only, ~25 MB image)

```bash
docker run --rm -p 7780:7780 \
    ghcr.io/mikedconcepcion/cuttledb-server:latest
```

The container is distroless, runs as non-root (UID 65532), and persists
WAL via a `/var/lib/cuttledb/wal` volume. Build locally with
`docker build --build-arg VERSION=0.8.0 -t cuttledb .` — the build
verifies the binary's sigstore signature against Rekor before assembling
the image.

## Quickstart — Python

```bash
pip install cuttledb
```

```python
from cuttledb import CuttleDB, ColType

with CuttleDB.connect("127.0.0.1", 7780) as db:
    hid = db.open()
    tid = db.create(hid, "memory", [
        ("text",      ColType.STRING),
        ("embedding", ColType.VEC, 768),
    ])

    db.insert(hid, tid, ["hello world", [0.1, 0.2, ...]])

    hits = db.knn(hid, tid, col=1, k=5, query=[0.15, 0.18, ...])
    for row_id, score in hits:
        print(score, db.get(hid, tid, row_id))

    # Subscribe to live changes — register, then drain pending events
    # or open a streaming context. The wire protocol delivers events
    # asynchronously; the SDK exposes both pull (poll_events) and
    # iterator (stream_events) flavors.
    db.sub(hid, tid)
    for evt in db.poll_events(timeout=1.0):
        print("changed:", evt)
```

See [examples/python_quickstart.py](examples/python_quickstart.py)
and [cuttledb-cli.py](cuttledb-cli.py) (interactive REPL).

## Quickstart — Node.js

```bash
npm install cuttledb
```

> **Note:** The package is **ESM-only**. Use `import` (ES modules), not
> `require()` (CommonJS). CJS projects must switch to ESM (or use a
> dynamic `import()`).

```js
import { CuttleDB } from "cuttledb";

const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
await db.connect();

const hid = await db.open();
const tid = await db.create(hid, "memory", [
    ["text",      2],
    ["embedding", 3, 768],
]);

await db.insert(hid, tid, ["hello world", [0.1, 0.2, /* ... */]]);

const hits = await db.knn(hid, tid, 1, 5, [0.15, 0.18, /* ... */]);
console.log(hits);

db.on("event", (evt) => console.log("changed:", evt));
await db.sub(hid, tid);
```

## Quickstart — Browser (WebSocket)

```html
<script type="module">
import { CuttleDB } from "https://unpkg.com/cuttledb/browser.js";

const db = new CuttleDB({ transport: "ws", url: "ws://localhost:7780" });
await db.connect();

const hid = await db.open();
const tid = await db.create(hid, "notes", [["title", 2], ["body", 2]]);
await db.insert(hid, tid, ["hello", "world"]);
console.log(await db.count(hid, tid));   // 1

// Real-time: pushed automatically on every change.
db.on("event", (evt) => console.log("changed:", evt));
await db.sub(hid, tid);
</script>
```

Open two tabs against the same server and you have shared state with
real-time push.

---

## Scaling out

CuttleDB is single-instance native. Multi-machine deployments
**compose from existing primitives** — `LOG` (change feed), `SUB`
(push), and `SAVE`/`LOAD` (snapshots). Five reference architectures
live in [**docs/DEPLOYMENT.md**](docs/DEPLOYMENT.md):

| Pattern | Use when |
|---|---|
| **Primary + read replicas** | Read-heavy, single writer |
| **Sharded** | Data exceeds one machine's RAM |
| **Geo-replicated reads** | Browser users across regions |
| **Hot/cold tiering** | Most queries on recent data |
| **Local-first / mobile** | App must work offline |

Composition uses the `Cluster` adapter class
(`from cuttledb.cluster import Cluster` /
`import { Cluster } from "cuttledb/cluster"`) and the
`cuttledb.replicate` companion script.

---

## Architecture

CuttleDB ships as one self-contained binary. There is no daemon
hierarchy, no embedded language interpreter, no plugin loader, no
external runtime — the server, the column store, the indexes, the
WAL, the subscription broadcast, and the TLS handshake are all the
same process.

```
[client] ── line protocol over TCP / WebSocket ── [cuttledb-server]
                                                         │
                                                         ├── column store (INT / FLOAT / STRING / VEC / DATETIME)
                                                         ├── HNSW + BM25 indexes
                                                         ├── WAL (CRC32-framed, replay-on-start)
                                                         ├── subscription broadcast (per-table)
                                                         └── audit + metrics + slow-query log
```

For the wire format, see [PROTOCOL.md](PROTOCOL.md). For the
roadmap, see [docs/ROADMAP.md](docs/ROADMAP.md).

---

## License

Apache-2.0. See [LICENSE](LICENSE).

All client adapters, SDKs, docs, examples, benchmark scripts, and
the wire protocol specification in this repository are open source
under Apache-2.0.

The `cuttledb-server` binary is distributed for free use (development,
production, commercial). Source for the database engine inside the
binary is not published in this repository; the binary plus the open
adapters plus the wire protocol cover every supported integration.

Release binaries are signed via sigstore (`cosign sign-blob` keyless
flow). See [SECURITY.md](SECURITY.md) for the verification recipe
and the disclosure policy.
