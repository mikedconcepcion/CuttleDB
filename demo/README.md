# CuttleDB demos

Runnable, no-narration tours of CuttleDB. The first one — **the coffee shop
analytics demo** — walks every distinctive CuttleDB surface in one coherent
~60-second narrative. If you're trying to see what CuttleDB *is*, start here.

## demo_coffee_shop.py

One day at a coffee shop. ~100 events with timestamps, customer names,
descriptions, and embeddings. The demo exercises:

- **Typed columns** including the v1.0.1 `DATETIME` type
- **Bulk INSERT** — 100 events in single-digit milliseconds
- **Real-time SUB/UNSUB** — a barista's tablet sees new orders arrive live
- **DATETIME range filter** via KNN+WHERE (epoch-ms predicate, full f64)
- **Semantic search** — top-k by cosine similarity (KNN, AVX2)
- **BM25 full-text** — `LSEARCH` for keyword match
- **Hybrid search** — `SEARCH` fuses vector + BM25 via Reciprocal Rank Fusion
- **O(1) cached aggregates** + SIMD MIN/MAX
- **Snapshot persistence** — `SAVE` to one portable file

Each step prints what it did and how long it took. Total runtime is
~5–10 seconds depending on hardware.

### Run

```bash
# 1. Start an CuttleDB server (in a separate terminal)
cuttledb-server --port 7780

# 2. Install the Python SDK (from the CuttleDB repo root)
pip install -e adapters/python

# 3. Run the demo
python demo/demo_coffee_shop.py
```

### Expected output

See [`DEMO.md`](DEMO.md) for the captured output of a real run —
every section, every timing, every result. Useful if you want to see
what the demo does without cloning the repo.

### Why this scenario

A coffee shop's event log hits every CuttleDB surface naturally:
- Orders are **events with timestamps** (DATETIME)
- Customer feedback is **searchable text** (vector + BM25)
- The barista's tablet wants **live updates** (SUB)
- The shift summary needs **aggregates** (COUNT/MIN/MAX)
- End of day needs a **portable snapshot** (SAVE)

Real-world workloads — analytics dashboards, IoT, agent logs, audit
trails — all have the same shape. The demo is concrete enough to
follow and abstract enough to map to your domain.

### Reading the timings

The numbers in the output are real, measured wall-clock — not invented
marketing. Typical results on a modern laptop:

| Operation | Latency |
|---|---|
| CREATE table (5 cols) | <1 ms |
| INSERT batch (100 rows) | ~10 ms |
| KNN over 100 vectors | ~0.1 ms |
| LSEARCH (BM25) | ~5 ms (first call builds index) |
| SEARCH (hybrid RRF) | ~0.2 ms |
| MIN/MAX on 103 rows | <0.1 ms |
| SAVE (35 KB snapshot) | ~1 ms |

The CuttleDB engine itself is ~550 KB on disk. The brain you write
during a typical session is single-digit KB. The whole stack you
just exercised is smaller than a single React app icon.

### What's NOT in the demo

To keep it under 300 lines and one file:

- **Transactions** (BEGIN/COMMIT/ROLLBACK) — exercised in unit tests
- **WAL durability** — exercised in unit tests
- **Boolean DSL** (BSEARCH) — exercised in unit tests
- **WebSocket transport** — see `examples/browser_realtime.html`
- **WASM in-process mode** — see `examples/browser_quickstart.html`

Each of those gets its own focused example or test. The coffee-shop
demo is the "everything-at-once tour"; the others are the deep-dives.
