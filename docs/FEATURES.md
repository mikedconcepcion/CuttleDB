# CuttleDB Features

> Each feature is documented in three ways: **what it does**, **why it
> matters** for real workloads, and **how it works** under the hood so
> you can reason about performance.

---

## Data model

### Columns

A table is a set of typed parallel arrays. There are five column types:

| Type | Storage | Tag in `CREATE` | Notes |
|---|---|---|---|
| Integer  | `double[]` (promoted) | `0` | Stored as f64 internally; returned as integer-valued string. |
| Float    | `double[]` | `1` | Returned as `%g`. |
| String   | handle table | `2` | Variable-length, deduplicated via the server's string arena. |
| Vector   | `float[num_rows * dim]` packed | `3:dim` | Fixed-dim f32 per row. The dim is part of the schema. |
| Datetime | `double[]` (int64 epoch ms as f64) | `4` | UTC. INSERT/predicate accept ISO 8601 string or raw epoch ms; GET returns ISO 8601. (v1.0.1) |

Tables are addressed by `(hid, tid)` — handle id + table id. Up to
**16 handles**, **256 tables per handle**, **32 columns per table**.
Each handle is independent (different schema, different data).

### Identity and keys

Rows are addressed by their **row id**, assigned at insert time
(monotonically increasing per table). There are no auto-generated UUIDs
and no primary keys in the SQL sense — rows are positional. If you need
a logical key, store it as a string column and use `SELECT WHERE` to
look it up.

---

## Read path

### O(1) aggregates: `COUNT`, `SUM`

The server keeps a **cached running sum** per numeric column and
updates it on every `INSERT` / `UPDATE` / `DELETE`. `SUM` returns the
cached value with one memory load — completely independent of table
size. `COUNT` returns the `num_rows` field — also O(1).

| Op | Complexity | Typical latency on 1K rows |
|---|---|---|
| `COUNT` | O(1) | <0.01ms |
| `SUM`   | O(1) | <0.01ms |

### SIMD scans: `MIN`, `MAX`, `FCOUNT`, `SELGT`

`MIN` and `MAX` are AVX2 horizontal reductions over the column's `f64[]`
buffer. `FCOUNT` (count rows where col > threshold) and `SELGT`
(select those rows) use AVX2 predicate scans with masked compress.

On x86-64 (AVX2): 4 doubles per vector × multiple per cycle.
On WASM: SIMD128 fallback, 2 doubles per vector but same algorithm.

| Op | Complexity | Typical latency on 1K rows |
|---|---|---|
| `MIN` / `MAX` | O(N) | ~0.03ms |
| `FCOUNT`      | O(N) | ~0.02ms |
| `SELGT`       | O(N) + write rows | ~0.05ms |

For columns < 1M rows on modern x86, all of these are sub-millisecond.

---

## Write path

### `INSERT` and `INS_BATCH`

Inserts append to each column's underlying array. Numeric columns also
update the cached running sum atomically (under the global DB lock).

`INSERT` is one row per command. `INS_BATCH` accepts an array of rows
and inserts them in one C call — no per-row mutex acquire/release,
no per-row parse overhead. Use `insert_batch` (Python) /
`insertBatch` (JS) for bulk loads.

Column growth doubles capacity when full. Amortized insert is O(1).

### `DELETE`

Removes a row by id. Implementation is **swap-with-last**: copy the
last row over the deleted slot, decrement `num_rows`, update the cached
sum. Row ids are not stable across deletes — the moved row keeps its
original id (the server tracks this in an id→position map).

### `SAVE` / `LOAD`

`SAVE <hid> <path>` writes the entire handle (all tables, all columns,
the string arena) to a single binary file. `LOAD <path>` reads it back
and returns a fresh `hid`.

Format is intentionally simple: magic header, version, per-table
schema + raw column buffers. Forward-compatible up to point releases;
v1.0 will lock the format.

This is **snapshot persistence**, not durability. A crash between
saves loses the unwritten window. The v0.5 WAL mode replaces this as
the default.

---

## Real-time subscriptions

### `SUB` / `UNSUB`

A client issues `SUB <hid> <tid>` to register for change events on a
table. The connection is added to the table's subscriber list (small
array, scanned linearly — fine up to thousands of subscribers).

Every `INSERT` / `UPDATE` / `DELETE` runs `broadcast_event(hid, tid,
row_id, op)` under the global DB lock. The broadcast scans all live
connections; subscribed ones receive a `>EVT <hid> <tid> <row_id> <op>`
line, written directly to their socket under a per-connection write
mutex (to prevent interleaving with that connection's regular
response).

| Property | Value |
|---|---|
| Subscriber registration | O(subs per table) — small linear scan |
| Broadcast cost per event | O(live conns × subs/conn) |
| 16 subs × 100 events | 68ms total (100% delivery) |

Closing the TCP connection clears all subscriptions automatically.

---

## Change feed

### `LOG <hid> <tid> [since]`

Every table has a **ring buffer of the last 1024 events** with a
monotonic counter. Each event records the timestamp (process-monotonic
ms), the row id, and the op (`I` / `D` / `U`).

`LOG` returns events at or after `since` (or all 1024 if `since` is
omitted), along with the current cursor. Store the cursor in your
worker's checkpoint; on restart, ask `LOG` for everything after it.

Recovery window: 1024 events. Long-outage recovery is what the planned
v0.5 WAL mode addresses.

---

## Vector search

### `VEC` columns

A vector column has a fixed dimension `dim` declared at `CREATE` time
(e.g. `embedding:3:768` is a 768-dim f32 vector). Storage is a single
packed `float[num_rows * dim]` buffer — cache-friendly and SIMD-ready.

### `KNN <hid> <tid> <col> <k> <query>`

Computes cosine similarity between the query vector and every row's
vector in the column, then returns the top-k by score (descending).

Implementation:
1. **Dot products**: AVX2 fused multiply-add, processing 8 floats per
   instruction. 768-dim takes ~96 FMAs per row.
2. **Norms**: query norm is computed once; row norms are computed
   inline during the dot loop.
3. **Top-k**: partial sort via a min-heap of size k. For k=10 over
   10,000 vectors, this is the dominant cost (still <1ms).

| Workload | Typical latency |
|---|---|
| KNN top-10 over 1K × 128-dim | ~0.4ms |
| KNN top-10 over 10K × 128-dim | ~2ms |
| KNN top-10 over 100K × 768-dim | ~120ms |

### `INDEX <hid> <tid> <col> HNSW [M=N] [ef_construction=N]` (v0.5.16)

Builds an HNSW (Hierarchical Navigable Small World) approximate
nearest-neighbor index on a VEC column. Once built, `KNN` queries on
that column auto-route through the graph. The brute-force SIMD path
above stays the default for unindexed columns — both share the same
`KNN` verb at the wire level.

```
INDEX 0 0 3 HNSW                          # defaults: M=16, M_max0=32, ef=200
INDEX 0 0 3 HNSW M=32 ef_construction=400 # tuned for higher recall
```

`M` is clamped to 1–63 (`M_max0 = 2M` fits the level-0 edge count's
int8 field). `ef_construction` is clamped to 1–2000.

| Workload | Brute force | HNSW |
|---|---|---|
| KNN top-10 over 10K × 128-dim | 1.46 ms | 0.71 ms (2.1×) |
| KNN top-10 over 100K × 128-dim | 11.14 ms | 0.99 ms (12.7×) |

**Crossover.** Below ~1-2K rows, brute-force SIMD wins thanks to
graph-traversal constant factors. Build only above that.

**Lifecycle.** After `INDEX HNSW`, `INSERT` and `DELETE` maintain the
graph incrementally — no rebuild required. `SAVE` persists the index
inside the OCTO v4 snapshot; `LOAD` reconstructs it in milliseconds.
At 100K nodes: snapshot 62 MB on disk, loads in 49 ms.

Full bench writeup and reproduction script: `bench/HNSW_BENCH.md`.

### `KNN ... WHERE <predicates>` (v0.5.17)

Filter the KNN result set by structured predicates on non-vector
columns. Same `KNN` verb at the wire level; the optional WHERE clause
extends the existing grammar.

```
KNN 0 0 3 5 v1|v2|... WHERE 4="kernel" AND 5>3
```

Grammar: `col_idx OP value [AND col_idx OP value ...]`. OPs:
`= != < <= > >=`. Up to 8 predicates AND'd. String values are quoted,
numeric values are bare. The HNSW path oversamples 4× to keep `k`
matching results after the filter.

### `INDEX <hid> <tid> <col> BM25 [k1=N] [b=N]` (v0.5.17)

Builds a Lucene-style BM25 inverted index on a STRING column.
Defaults match Lucene/Elasticsearch: `k1=1.5`, `b=0.75`. The tokenizer
splits on non-alphanumeric, lowercases.

### `LSEARCH <hid> <tid> <col> <k> <query>` (v0.5.17)

Top-`k` lexical search via BM25. Auto-builds the index on first call
if `INDEX ... BM25` wasn't run first.

```
LSEARCH 0 0 2 5 hnsw ef_construction
```

### `SEARCH <hid> <tid> <vec_col> <text_col> <k> <vec> ||| <query>` (v0.5.17)

Hybrid retrieval. Runs KNN over `vec_col` and LSEARCH over
`text_col` independently then merges by Reciprocal Rank Fusion:

```
RRF(d) = sum over rankers of 1 / (60 + rank(d))
```

The `|||` triple-pipe splits the inline vector from the lexical
query. Optional WHERE applies to both streams before fusion.

```
SEARCH 0 0 3 2 5 0.1|0.2|0.3|0.4 ||| brown fox WHERE 4="playbook"
```

The 60 is the standard RRF constant from Cormack et al. 2009.

### `BSEARCH <hid> <tid> <k> <expr>` (v0.5.17)

Boolean DSL. Filter predicates combine with scoring atoms in one
expression.

```
BSEARCH 0 0 5 (4="playbook" OR 4="ref") AND 1~V[0.1|0.2|0.3] AND 5>3
```

Grammar:
- `or_expr   := and_expr ('OR' and_expr)*`
- `and_expr  := atom ('AND' atom)*`
- `atom      := '(' or_expr ')' | predicate`
- `predicate := col_idx OP value | col_idx '~' V'['floats']' | col_idx '~' "phrase"`

Filter atoms (`=`, `!=`, `<`, etc.) reduce the candidate set per-row.
Scoring atoms (`~V[...]` for vectors, `~"phrase"` for BM25)
contribute to a per-row RRF score; results are ranked by fused score
when any scoring atom is present, otherwise by row_id ascending.

### `INDEX <hid> <tid> <c0> <c1> [c2…]` — composite index (v0.8.0)

Builds a **multi-column exact index** over two or more columns, queried
by `FINDC` in one wire call. It serves the "match on several columns at
once" shape — `(make, model, year)`, `(tenant, status)`,
`(symbol, date)` — that a single-column `FIND` could only answer with a
linear `BSEARCH` scan.

This overloads the existing `INDEX` verb. The form is disambiguated by
its second token: a **digit** means another column id (composite),
whereas `HNSW` / `BM25` start with a letter.

```
INDEX 0 1 2 3 1        # composite over fitment columns (make, model, year)
```

- **2–8 columns.** Numeric and string columns may both participate;
  `VEC` columns are rejected. Up to 8 composite indexes per table.
- **Ordered.** `(2, 3, 1)` and `(1, 2, 3)` are distinct indexes;
  `FINDC` must present the same ordered column list to hit the index.
- Returns the number of rows indexed. Idempotent — rebuilding the same
  column list drops and recreates it.

### `FINDC <hid> <tid> <ncols> <c0> <c1> … <v0>\x1f<v1>…` (v0.8.0)

Composite point lookup: returns `[r0;r1;…]` for the rows where **every**
`col == value`.

```
FINDC 0 1 3 2 3 1 HONDA<US>CIVIC<US>2018
                       └ values, 0x1f-separated ─┘
```

The value block runs to end-of-line and fields are separated by the
`0x1f` unit separator (shown `<US>` above), so values may contain spaces
and commas without escaping. `FINDC` is **always correct**: O(1) average
when a composite index over the same ordered column list exists,
otherwise an O(N) scan over per-row composite keys.

**Canonicalization.** Stored cells and query values pass through one form
so client-sent values round-trip: integral numbers as `%lld`, other
numbers as `%.17g`, strings as raw bytes. `2018`, `2018.0`, and
`2018.00` collapse to the same key.

**Lifecycle.** `INSERT` and `DELETE` maintain the index incrementally
(the swap-with-last delete mirrors the per-column string index). A
pending transaction drops composite indexes conservatively — rebuild via
`INDEX` after commit — matching the HNSW/BM25 invalidate-on-tx behavior.
`SAVE` persists only the column-list definitions inside the **OCTO v5**
snapshot; `LOAD` rebuilds the hash from rows in milliseconds. v1/v2/v4
snapshots still load unchanged.

**Benchmark — real-world driver.** The CuttleSearch tire store runs a
fitment table of **628 K rows** and the "2018 Honda Civic" query shape.
Before the composite index, CuttleDB answered it with a linear
`BSEARCH` scan; after, with `FINDC`:

| Vehicle-shape query | SQLite (FTS5) | CuttleDB before | CuttleDB after |
|---|---|---|---|
| Isolated backend primitive, p50 | 1.07 ms | ~16.5 ms (scan) | **3.71 ms** |
| End-to-end router.search, p99 | 44.63 ms | — | **9.24 ms** |

The composite index removes the 628 K-row scan; the residual cost is now
one wire round-trip per `(make, model)` pair, not the scan. End-to-end
the tail is **~5× tighter than SQLite's**. Results are an exact match
vs SQLite (2018 Civic 233 rows, 2017 Corolla 211, Ford F-150 37).
Reproduce with `stores/gtatire/bench_router.py` in the CuttleSearch repo.

---

## Security

### Authentication

Start the server with `--auth <token>` to require AUTH before any
non-PING/HELLO command. Every connection arrives unauthenticated; only
`PING`, `HELLO`, and `AUTH <token>` are allowed until AUTH succeeds.
The matched token is compared in constant time to deter timing attacks.

Without the flag, all connections are pre-authenticated — drop-in
compatible with v0.2 deployments.

`HELLO` advertises `auth_required` so clients can prompt for credentials
proactively:

```
HELLO  → +OK cuttledb 0.3.0-dev proto 1 auth_required
AUTH foo  → -ERR auth failed
AUTH s3cret  → +OK authenticated
```

### TLS

The **default binary links no TLS implementation** — that would break
the zero-dependency invariant. For that build, terminate TLS at
`stunnel` / `nginx` / a cloud load balancer and run CuttleDB on
`127.0.0.1`; the wire protocol passes through any TLS terminator
unchanged.

For deployments that want TLS in-process, an **opt-in build**
(`CUTTLEDB_WITH_TLS=1`) links a vendored, audited TLS 1.2 stack — still
no system package dependency. It is configured entirely by flags:

| Flag | Effect |
|---|---|
| `--tls-cert <pem>` / `--tls-key <pem>` | Enable TLS with this server certificate + key. RSA **and** EC (P-256 / P-384) keys are accepted. |
| `--tls-ciphers <csv>` | Restrict negotiated suites to an explicit allow-list (OpenSSL-style names, e.g. `ECDHE-RSA-AES256-GCM-SHA384`). Unknown names fail fast at startup. |
| `--tls-client-ca <bundle.pem>` | **Mutual TLS** — require and verify a client certificate against this CA bundle. Missing or untrusted client certs are rejected at the handshake. |

Certificates **hot-reload**: rotating the cert/key files on disk is
picked up on the next connection (mtime-polled in the accept loop), so
you can roll certificates without a restart or dropping live
connections. There is deliberately **no OCSP / CRL** — revocation is
handled by short-lived certificates rotated via hot-reload and narrowed
by mTLS, which keeps the surface small and auditable.

### Client-side encrypted columns

Selected STRING cells can be encrypted **in the adapter** before they
reach the wire and decrypted after read-back, so the server stores only
opaque ciphertext — at-rest privacy that does not depend on disk
encryption or trusting the server host. There is no server-side crypto
and no new wire verb; an encrypted cell is an ordinary STRING value.

- **Python** — `FieldCipher` + `insert_enc` / `insert_batch_enc` /
  `get_dec`, gated behind the optional `cuttledb[crypto]` extra. The
  base install stays zero-dependency.
- **JS / TS** — `FieldCipher` + `insertEnc` / `insertBatchEnc` /
  `getDec`, built on `node:crypto`.

Values use AES-256-GCM (fresh 12-byte nonce, 16-byte tag) in a
language-neutral `enc:v1:<base64(nonce‖ciphertext‖tag)>` token, so a
value encrypted by one adapter decrypts in the other. Key management is
the caller's responsibility; CuttleDB never sees the key or plaintext.

## Networking

### Wire protocol

Simple ASCII line protocol, inspired by Redis inline-command form.
`VERB ARG1 ARG2 ...\n` requests; `+OK <value>\r\n` or `-ERR <msg>\r\n`
responses. See [PROTOCOL.md](../PROTOCOL.md) for the full spec.

### Transports — same protocol, three carriers

The line protocol runs identically over three transports:

- **Raw TCP** — the default. Python, Node, anything with a socket.
- **WebSocket** — RFC 6455 framing. Same port as TCP, auto-detected on
  the first request. One command per text frame. No external crypto
  dependency: SHA-1 + Base64 are inline in the server. Use for browsers
  and any environment without raw TCP access.
- **In-process** — `cuttledb_exec_line(line, outbuf, outlen)`. No socket,
  no network. Used by the WASM build and any embedded native caller.

The adapter API is identical across all three:

```js
new CuttleDB({ transport: "tcp",  host, port });    // Node, native
new CuttleDB({ transport: "ws",   url });           // Browser
new CuttleDB({ transport: "wasm", wasmUrl });       // Browser, experimental
```

### Pipelining

Multiple commands in one TCP packet → all responses batched back in
one packet. Server flushes only when the recv buffer drains. The
adapters' `insertBatch` / `insert_batch` / `sendBatch` methods use this
to amortize round-trip time across many ops.

A bulk-INSERT of 1000 rows via pipelining: **39ms total** (4.5×
faster than Redis on the same loopback).

### Multi-client server

`cuttledb-server --port 7780` (or the embedded equivalent) listens on
TCP. Each accepted connection runs on its own thread (`CreateThread`
on Windows, `pthread_create` elsewhere). Per-connection state
(recv/send buffers, subscription list, write mutex) is heap-allocated
in the thread — no thread-local storage.

A **global DB lock** serializes all writes. Reads do not take the
lock if the column is cached-aggregate-only (`SUM`, `COUNT`); otherwise
they take it briefly. This is intentional: write throughput is high
enough that mutex overhead doesn't dominate, and the lock keeps the
implementation auditable.

Limits:
- 256 live connections (raise the cap in source if needed)
- 16 subscriptions per connection
- 64KB max line length per command
- 64KB max response payload per single write (large `SELGT` results
  flush mid-stream)

---

## In-process / WASM mode

The same engine compiles to WebAssembly. Loading the `cuttledb.wasm`
module exposes `cuttledb_exec_line(line, outbuf, outlen)` — same wire
protocol, no socket. The JS adapter uses this for the `wasm` transport.

| Property | Value |
|---|---|
| WASM module size | 189KB |
| JS glue size | ~85KB |
| Total page payload | ~280KB |
| Cold start | ~50ms (one-time WASM compile) |
| Per-command overhead vs TCP | ~5μs (no socket, direct call) |

What's available in WASM mode: the full data model, all read/write
verbs, `SUB`/`UNSUB` (events queued for polling rather than pushed),
`LOG`, `KNN`, `SAVE`/`LOAD` (via virtual filesystem). What's not: the
multi-client server (it's an in-process DB by definition).

---

## Operations and observability

### `PING` / `HELLO`

`PING` returns `+OK PONG` — used for connection health.
`HELLO` returns `+OK cuttledb <version>` — used for protocol version
negotiation. Clients should call `HELLO` on connect to verify
compatibility.

### `INFO`

Returns a single line with key=value pairs: version, uptime_ms, handles,
total_tables, total_rows, total_events. Use for health dashboards and
monitoring.

### `--rate-limit <N>`

Per-connection sliding 1-second window. Every line counts (including
`PING` and `AUTH` — so a misbehaving client can't dodge the limit with
cheap verbs). Over-limit responses are `-ERR rate limit\r\n`. Default:
off. Recommended for production: set well above peak normal load
(e.g. `1000`) to protect against runaway clients without throttling
real workloads.

### `--slow-log-ms <N>`

When set, any command whose dispatch exceeds N ms is logged to stderr as
`[slow Xms] VERB ARG…`. Useful for finding tables that have grown past
comfortable scan size, or KNN over too-many vectors. Default: off (the
`now_ms()` measurement itself is elided when the flag is zero).

### `STATS [hid] [tid]`

With no args: aggregate counters since process start (inserts, deletes,
selects, evictions). With `hid tid`: per-table counters.

---

## What's not yet here (and what plans to)

As of v0.9.0 the write path, durability, transactions (incl. DDL),
secondary + composite indexes, HNSW ANN, BM25, joins, the full
hardening tier (AUTH, TLS, audit, rate limit, `/health`, `/metrics`),
TLS hardening (mTLS, EC keys, cipher allow-list, cert hot-reload), and
client-side encrypted columns have all shipped. What remains:

| Feature | Status | Planned |
|---|---|---|
| OCSP / CRL revocation | not planned — short-lived certs + hot-reload + mTLS instead | — |
| Graph types + traversal (`MATCH`) | absent | v1.0 |
| Native CRDT / distributed sync | compose via `LOG`/`SUB` (`Cluster` adapter) | v1.0 |
| `SELECT AS OF <ts>` temporal queries | substrate ready, surface absent | v1.0 |
| Predicate-filtered `SUB` | substrate ready, surface absent | v1.0 |
| GPU HNSW index | index lives on CPU | v1.0 |
| Reproducible-build attestation | sigstore-signed releases today | v1.0 |

See [ROADMAP.md](ROADMAP.md) for trajectory.
