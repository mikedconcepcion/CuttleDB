# Why CuttleDB

> What it actually solves. Concrete, named workloads — not abstract claims.

Most databases were built around a request-response shape: a human types
into a form, an API server fields the call, the DB answers once and
goes quiet. CuttleDB is built around a different shape: **a process that
needs to read and write constantly, often in real time, sometimes
offline, always local.** That covers a wider set of workloads than the
shape implies — desktop apps, real-time UIs, embedded analytics, AI
agents, sync workers. Five workloads cover what people actually use
it for.

---

## 1. Local-first apps that never phone home

**The problem.** You want to build an app — notes, sheets, a personal
CRM, a coding assistant, a desktop tool — that runs entirely on the
user's machine, never sends data to a server, and survives the company
shutting down (or you losing interest). Every existing solution either
requires a cloud account, ships gigabytes, or uses a database that
wasn't built for this.

**How CuttleDB handles it.** One self-contained binary. Zero
dependencies. Same data model whether the user runs it on a desktop,
in a browser tab, or embedded in a tool. `SAVE` writes the entire DB
to a single file the user owns. `LOAD` reads it back. No format
lock-in — the wire protocol is documented and the binary format is
intentionally simple. If you stop maintaining the project, every
running install keeps working forever.

```python
# A complete local-first app's persistence layer.
db.save(hid, "~/Documents/my-notes.cuttledb")  # save on quit
hid = db.load("~/Documents/my-notes.cuttledb")  # resume on startup
```

This is the lineage of Gun.js, RxDB, and the local-first movement —
but with the speed of a native column store and the simplicity of one
binary.

---

## 2. Embedded analytics in a desktop or browser app

**The problem.** You're building a desktop app, a static-hosted browser
app, or an Electron-style tool. You need a real database — typed
columns, aggregates, vector search — but you don't want to ship
Postgres, run a service, or trust the browser's localStorage with
structured data.

**How CuttleDB handles it.** The same engine compiles to WebAssembly.
A 189KB WASM module gives you the full CuttleDB API inside a browser
tab. No server. No network. The DB lives in the page's memory; SAVE
serializes it to a Blob you can store in IndexedDB or download as a
file.

```js
// Pure browser. No HTTP. Nothing sent anywhere.
const db = new CuttleDB({ transport: "wasm" });
await db.connect();

const hid = await db.open();
const tid = await db.create(hid, "transactions",
    [["date", 2], ["amount", 1], ["category", 2]]);

// Load 50,000 transactions from a CSV the user dropped on the page.
await db.insertBatch(hid, tid, parseCsv(droppedFile));

// SIMD-accelerated aggregates in the browser.
const total       = await db.sum(hid, tid, 1);
const bigExpenses = await db.selectGt(hid, tid, 1, 1000);
```

SUM and COUNT are O(1). MIN/MAX are AVX2 (or SIMD128 in WASM) — both
sub-millisecond over thousands of rows. Works offline. Works on a
plane. The user's data never leaves the device.

---

## 3. Real-time dashboards and live UIs

**The problem.** You want a UI that updates the moment data changes —
new orders, sensor readings, user actions, agent steps. Polling every
second wastes CPU and battery and adds latency. Server-sent events
are clumsy when the source of truth is a database.

**How CuttleDB handles it.** `SUB <hid> <tid>` registers a TCP client as
a subscriber. Every `INSERT`/`UPDATE`/`DELETE` triggers a `>EVT` line
on every subscribed socket — broadcast on the writer's thread, no
poller, no queue. Round-trip from write to subscriber: microseconds.

```js
const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
await db.connect();

db.on("event", (evt) => {
    // evt = { hid, tid, rowId, op }
    renderNewRow(evt.rowId);
});
await db.sub(hid, ordersTid);
```

Each subscriber adds one entry in a small array. 16 concurrent
subscribers × 100 events each delivers in 68ms with 100% delivery.

---

## 4. AI agent memory and RAG for local LLMs

**The problem.** Your agent runs locally (Ollama, llama.cpp, MLX, or a
desktop client). It needs to remember conversations, files, code, and
prior decisions. Embeddings over your data need to be searchable in
milliseconds. The agent loop can't afford a network round-trip to a
hosted vector DB, and you can't send your data to one anyway.

**How CuttleDB handles it.** First-class `VEC` columns store fixed-dim f32
embeddings as a packed buffer. `KNN <k> <query>` does AVX2 cosine
similarity + partial-sort top-k in one C call. Inserting 10,000
embeddings takes 1.3 seconds; querying top-10 over them takes 2ms.

```python
# RAG memory loop in 12 lines.
with CuttleDB.connect("127.0.0.1", 7780) as db:
    hid = db.open()
    tid = db.create(hid, "memory", [
        ("doc_id",    ColType.STRING),
        ("chunk",     ColType.STRING),
        ("embedding", ColType.VEC, 768),
    ])

    for chunk, vec in chunks_with_embeddings(documents):
        db.insert(hid, tid, [chunk.doc_id, chunk.text, vec])

    query_vec = embed("how does the planner pick a tool?")
    hits = db.knn(hid, tid, col=2, k=5, query=query_vec)
    for row_id, score in hits:
        doc_id, text, _ = db.get(hid, tid, row_id)
        print(score, doc_id, text)
```

No separate vector DB. No hosted service. No API key. Same database
that stores your structured data also stores the embeddings.

---

## 5. Change-data-capture for workers that drop and reconnect

**The problem.** You have a long-running worker — a background agent,
a sync service, a notifier — that needs to react to *every* change in
a table but might crash, restart, or get disconnected. Subscribers
miss events while they're offline. You don't want to design an event
log infrastructure just for this.

**How CuttleDB handles it.** Every table has a built-in ring buffer
(last 1024 events) with a monotonic cursor. `LOG <hid> <tid> [since]`
returns events at or after `since`. Worker writes the cursor it last
saw to a checkpoint file; on restart, it reads `LOG` from the
checkpoint and catches up before re-subscribing.

```python
cursor = read_checkpoint() or 0
with CuttleDB.connect("127.0.0.1", 7780) as db:
    while True:
        events, cursor = db.log(hid, tid, since=cursor)
        for evt in events:
            handle(evt)
        write_checkpoint(cursor)
        time.sleep(0.05)
```

The ring buffer is in-memory and lock-free for the reader path —
overhead per event is two cache-line writes. The 1024-event window is
enough to recover from minute-scale outages; longer recovery uses the
WAL on disk.

---

## What CuttleDB is *not* trying to be

- **Not a SQL wire protocol.** Native protocol is Redis-style line
  commands — predictable parsing, single-roundtrip verbs, no query
  planner surprises. A SQL frontend on top of the wire is a separate
  adapter; the substrate stays minimal.
- **Not eventually-consistent.** Single-primary writes serialized by
  a global mutex. CRDT-style multi-writer merge is a v1.x consideration,
  not the current model.
- **Not a clustered system.** Single-primary + read-replicas via the
  change feed (composed from `LOG` + `SUB` + `SAVE`). Raft and consensus
  are out of scope.
- **Not a hosted product.** No SaaS, no managed offering. The whole
  point is that you run it yourself.
- **Not a JOIN-heavy relational engine *yet*.** v1.x adds multi-table
  JOIN and GROUP BY for general-purpose query coverage; today's path is
  client-side joining for cross-table queries.

---

## Compare to what you'd otherwise use

| If you'd reach for… | Use CuttleDB when… |
|---|---|
| **SQLite (file or `:memory:`)** | You want push subscriptions, native vector search, full-text BM25, or change-feed replay — in the same engine. |
| **Postgres + pgvector + pg_search** | You want to ship one binary, not a service + extensions + container. Local-first deployment. |
| **Redis** | You need typed columns, aggregates, full-text, or vector similarity — not just a KV. |
| **DuckDB** | You want push subscriptions and the data is live, not analytical-only. |
| **Pinecone / Weaviate / Qdrant** | You want vector search local-first, with no API key — and your structured data lives in the same DB. |
| **Elasticsearch / Meilisearch** | You want BM25 full-text without running a separate service. |
| **localStorage / IndexedDB** | You want SIMD aggregates, full-text, or vector search in the browser — not a key-value store. |
| **Gun.js / RxDB** | You want native speed and a typed schema, not a sync-first JS DB. |
| **MongoDB** | You want typed schema discipline + columnar speed, with the same multi-transport story (TCP / WS / WASM). |
