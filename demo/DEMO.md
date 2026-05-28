# CuttleDB Coffee Shop Demo — captured output

> Captured 2026-05-25 from `python demo/demo_coffee_shop.py` against
> a fresh `cuttledb-server --port 7780`. Real numbers, real timings,
> nothing hand-tuned. Your output will look the same modulo
> microsecond timings and current UTC time.

```
════════════════════════════════════════════════════════════════
  CuttleDB — Coffee Shop Analytics Demo
════════════════════════════════════════════════════════════════
  Five surfaces in one tour: typed columns + DATETIME + vector
  search + BM25 + hybrid + real-time push + snapshot.

  Server.......................................... 127.0.0.1:7780
  Handle.......................................... 0

━━ [1] CREATE — typed columns including DATETIME ━━━━━━━━━━━━━━━
  create 'events' table (5 columns)............... 0.12 ms
  Columns......................................... name (STR), customer (STR), description (STR),
                                                  created_at (DATETIME), embedding (VEC[64])
  Table id........................................ 0

━━ [2] INSERT — 100 events across an 8-hour workday ━━━━━━━━━━━━
  insert_batch(100 events)........................ 3.51 ms
  Total events.................................... 100
  First event..................................... 2026-05-24T10:37:13.720Z  [staff        ] barista Olivia started shift
  Last event...................................... 2026-05-24T18:26:41.543Z  [feedback     ] Olivia mentioned the wifi keeps droppi

━━ [3] SUB — barista's tablet watches live orders ━━━━━━━━━━━━━━
  insert 3 live events............................ 0.61 ms
  Live events received............................ 3
    evt row_id=100................................ op=INS  hid=0 tid=0
    evt row_id=101................................ op=INS  hid=0 tid=0
    evt row_id=102................................ op=INS  hid=0 tid=0

━━ [4] DATETIME range — events from the first 2 hours (morning rush)
  KNN k=100 WHERE created_at < morning cutoff..... 0.14 ms
  Events before 12:37 UTC......................... 45 of 103 total

━━ [5] KNN — semantic search ('long line, slow service') ━━━━━━━
  KNN k=3 over 100 events......................... 0.10 ms
    score=0.508................................... [feedback     ] Mia said the latte art was beautiful but t
    score=0.498................................... [feedback     ] Iris said the latte art was beautiful but
    score=0.463................................... [feedback     ] Leo said the latte art was beautiful but t

━━ [6] LSEARCH — BM25 full-text ('latte') ━━━━━━━━━━━━━━━━━━━━━━
  LSEARCH k=5 on 'description' column............. 2.68 ms
    bm25=2.40..................................... [order_placed ] Mia ordered a large oat milk latte
    bm25=2.40..................................... [order_placed ] Bob ordered a large oat milk latte
    bm25=2.25..................................... [order_ready  ] Mia's latte is ready at the bar
    bm25=2.25..................................... [order_ready  ] Iris's latte is ready at the bar
    bm25=1.79..................................... [feedback     ] Iris said the latte art was beautiful but

━━ [7] SEARCH — hybrid (vector + BM25 fused via RRF) ━━━━━━━━━━━
  SEARCH k=3 — 'latte issues'..................... 0.12 ms
    rrf=0.0328.................................... [order_placed ] Mia ordered a large oat milk latte
    rrf=0.0323.................................... [order_placed ] Bob ordered a large oat milk latte
    rrf=0.0317.................................... [order_ready  ] Mia's latte is ready at the bar

━━ [8] Aggregates — COUNT, MIN/MAX on the timestamp column ━━━━━
  COUNT (O(1) cached)............................. 103
  MIN(created_at)................................. 10:37 UTC
  MAX(created_at)................................. 18:37 UTC
  Span............................................ 480.0 minutes

━━ [9] SAVE — one-file snapshot, ready to scp anywhere ━━━━━━━━━
  SAVE → demo_coffee.cuttledb....................... 0.77 ms
  Snapshot size................................... 35,181 bytes
  Path............................................ E:\GPL\CuttleDB\adapters\python\demo_coffee.cuttledb

════════════════════════════════════════════════════════════════
  Done
════════════════════════════════════════════════════════════════
  What you just saw, in one binary, zero dependencies:

    ✓ Typed columns (STR / DATETIME / VEC) on one table
    ✓ Bulk insert (~100 events in milliseconds)
    ✓ Real-time push to subscribers (SUB / UNSUB)
    ✓ DATETIME range filtering (epoch-ms predicate)
    ✓ Semantic vector search (KNN cosine, AVX2)
    ✓ Full-text BM25 (LSEARCH)
    ✓ Hybrid retrieval (SEARCH = vector + BM25 + RRF)
    ✓ O(1) cached aggregates + SIMD MIN/MAX
    ✓ Snapshot persistence (SAVE → one file you own)

  Brain on disk: 35,181 bytes in one portable file. Engine binary: ~450 KB, zero deps.
```

## What to look at

A few things worth noticing in the output above:

**[2] INSERT.** 100 typed rows (5 columns each, including a 64-dim vector
and a DATETIME) in 3.5 ms. That's ~28 μs per row including parse, type
validation, embedding storage, and the cached running aggregates.

**[3] SUB.** Three events inserted by one connection, all three caught
by the subscriber on a separate connection — broadcast happens on the
writer's thread, no poll, no queue.

**[4] DATETIME range filter.** Powered by the new v1.0.1 DATETIME type
through the KNN+WHERE predicate path. Filter is ~140 μs over 103 rows.

**[5] KNN semantic.** Top-3 over 100 64-dim vectors in 0.1 ms — single C
call with AVX2 cosine + partial-sort. The demo uses a deterministic
hash-embedder so output is repeatable; with a real semantic embedder
(Ollama, OpenAI, etc.), the matches get even more nuanced.

**[6] LSEARCH BM25.** First call builds the inverted index lazily
(~2.7 ms for 100 rows). Subsequent calls are sub-millisecond.

**[7] SEARCH hybrid.** Reciprocal Rank Fusion of vector + BM25 in
0.12 ms — the dual-ranker path costs barely more than vector-only.

**[8] MIN/MAX on DATETIME.** Pre-existing SIMD horizontal reduction
over the f64 column (which is where DATETIME stores its epoch ms).
Returns raw epoch ms; the demo formats with stdlib `datetime`.

**[9] SAVE.** 35 KB on disk for the whole brain (103 events × 5 cols
including the embeddings). One file. SCP it. Open it elsewhere. The
binary format is documented and stable from v1.0.

## Re-running the demo

The demo is **idempotent against a fresh server**. Restart the server
between runs (or use a separate handle via `db.open()` for each run)
to keep the output clean — otherwise old events from previous runs
will accumulate in handle 0 and affect MIN/MAX timing.
