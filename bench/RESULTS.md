# CuttleDB benchmarks — measured, reproducible

> **All numbers on this page reproduce from a clean checkout.** Each
> result has a script in `bench/`; the README links here rather than
> embedding a results table so the methodology you're about to read
> stays attached to the numbers.

The goal of this document is not to convince you CuttleDB is the
fastest at everything. It's to show you, with the exact workload,
exact hardware, and exact code, what we measured — so you can
re-run any of it on your machine and get your own honest result.

---

## 1. Methodology — read this first

### Hardware + toolchain

- Windows 11 Home, 10.0.26200
- MinGW gcc (msys2 mingw64) 15.2.0
- Build flags: `-O3 -march=native -flto`
- TCP loopback (`127.0.0.1`)
- Python 3.13.2

Your hardware will differ. Re-run the scripts; you'll get different
absolute numbers but the relative shape should hold.

### Dataset

- 1,000 rows
- 2 columns: `name TEXT` (8-char random string) + `value INTEGER` (0–10000)
- Fixed seed (42); deterministic across runs

### Iterations

- 30 iterations for bulk-insert measurements
- 100 iterations for per-op measurements
- We report **medians**, not means — outliers (Windows scheduler
  jitter, GC pauses) skew means but not medians, and the median is
  the number a user would actually feel

### The asymmetry that matters

This is the single most important framing on this page:

- **SQLite** runs **in-process** in the benchmark harness — same
  Python address space, no kernel networking, no socket setup, no
  TCP round-trip. The cost of a SQLite call is the cost of a C
  function call plus the query.
- **CuttleDB** runs as a **TCP server** the harness connects to over
  loopback. Every operation pays: serialize → write to socket →
  context switch into the kernel → loopback → context switch back
  out → CuttleDB read → response → reverse trip. The TCP-loopback
  cost is real (typically 30–80 µs round-trip on Linux/macOS,
  somewhat more on Windows).

This is NOT apples-to-apples on the latency axis. We report it this
way because it matches the deployment model people actually use:
SQLite gets embedded into your process; CuttleDB gets a port. If
you want an apples-to-apples in-process comparison, that lands as
`bench/bench_wasm_sqlite.py` in v0.7+ (the WASM build runs CuttleDB
entirely in the caller's process).

### How to run any number on this page

```bash
# Start a CuttleDB server (download the binary from the latest GitHub
# Release and place it on PATH or set CUTTLEDB_SERVER_BIN):
cuttledb-server --port 7780

# Then in another shell:
python bench/bench_sqlite.py     # the table in § 2
python bench/bench_hnsw.py       # the result in § 3
```

Both scripts have configurable parameters via environment variables
(`BENCH_ROWS`, `BENCH_ITERS`); read the header comments.

---

## 2. SQLite (in-process) vs CuttleDB (TCP loopback)

Script: `bench/bench_sqlite.py`

We lead with the result that doesn't flatter us, because if your
first reaction to the table is "wait, isn't this comparison
unfair?" — yes, and that's the point of this section.

### Result 2.1 — INSERT bulk

| Op | CuttleDB (TCP) | SQLite (in-proc) | Verdict |
|---|---:|---:|---|
| INSERT 1000 rows (bulk) | 6.34 ms | 0.75 ms | **SQLite 8.4× faster** |

**What this measures:** the cost of a one-shot bulk insert of 1,000
small rows.

**Why SQLite wins:** SQLite's `executemany` is a tight C loop in the
same process — zero socket overhead, zero serialization. CuttleDB's
`insert_batch` pipelines all 1,000 rows in one wire roundtrip, but
that one roundtrip still costs more than SQLite's zero. At this
scale the architectural difference dominates the work itself.

**What this does NOT mean:** CuttleDB has slow inserts. The same
INSERT_BATCH wire verb sustains ~30K inserts/sec when pipelined
against typical row sizes; the bottleneck is the TCP roundtrip per
batch, not the server's work per row. The fair comparison is
CuttleDB-over-TCP vs another networked store (Redis, Postgres) over
TCP, where both sides pay for the socket. That lands as
`bench/bench_redis.py` in v0.7+.

**What this is good for:** an honest signal that if your workload is
"bulk-load 1K rows once at app startup and never write again," and
you don't need anything else CuttleDB offers (vector search,
real-time push, hybrid retrieval, etc.), SQLite is the right answer.
CuttleDB is for workloads where the network model + the substrate
features earn their cost.

### Result 2.2 — in-memory aggregates and predicate scans

| Op | CuttleDB (TCP) | SQLite (in-proc) | Verdict |
|---|---:|---:|---|
| SUM (1 call) | 0.026 ms | 0.042 ms | **CuttleDB 1.6×** |
| MIN (1 call) | 0.032 ms | 0.049 ms | **CuttleDB 1.5×** |
| COUNT WHERE (1 call) | 0.024 ms | 0.043 ms | **CuttleDB 1.8×** |
| SELECT WHERE (1 call) | 0.058 ms | 0.080 ms | **CuttleDB 1.4×** |

**Scoped claim:** *on in-memory aggregate queries and predicate
scans over 1,000 rows, CuttleDB completes SUM / MIN / COUNT 1.5–1.8×
faster than SQLite, and SELECT WHERE 1.4× faster, despite paying
the TCP-loopback round-trip cost SQLite skips entirely.*

That's exactly what was measured. We don't claim "CuttleDB is faster
than SQLite" in general — these are four specific queries at one
specific dataset size on one specific machine.

**Why CuttleDB wins here, despite the network handicap:** the
substrate uses AVX2 SIMD predicate scans + cached O(1) aggregates.
COUNT and SUM read from a running counter — constant time
regardless of row count. MIN/MAX/FCOUNT run a SIMD lane over the
column. SQLite walks the B-tree per row even at this small scale.
The substrate-level work CuttleDB does is fast enough to repay the
TCP cost.

**Caveat on absolute numbers:** these are medians on the test machine.
Your machine will differ — typically Linux + native gcc shows
CuttleDB's TCP loopback round-trip in the 30–50 µs range
(noticeably faster than Windows). Re-run the script.

---

## 3. HNSW vs brute-force KNN

Script: `bench/bench_hnsw.py`. Detailed methodology + tables in
[`HNSW_BENCH.md`](./HNSW_BENCH.md).

**Headline:** at 100,000 × 128-dim vectors, HNSW is **12.7× faster**
than the AVX2+FMA SIMD brute-force baseline (~1 ms per query vs
~13 ms). Recall@10 stays at 1.0 with default
`M=16, ef_construction=200`.

Unlike § 2, this comparison is apples-to-apples — both code paths run
inside the same CuttleDB binary, over the same TCP transport. The
12.7× is pure algorithmic improvement (HNSW vs O(N) brute force).

---

## 4. What we don't yet benchmark (deferred to v0.7+)

The following claims appeared in earlier README iterations without
reproducible scripts. They're pulled until we have backing.

### v0.7 — `bench/bench_redis.py`

CuttleDB-over-TCP vs Redis-over-TCP. This is the fair comparison to
INSERT bulk (§ 2.1) — both sides pay for the socket. Will exercise:

- Bulk INSERT throughput (CuttleDB INSERT_BATCH vs Redis pipelined
  SET)
- GET round-trip latency
- SUB/UNSUB push fan-out vs Redis pub/sub
- Memory usage at steady state

### v0.7 — `bench/bench_wasm_sqlite.py`

CuttleDB-in-process (WASM build) vs SQLite-in-process. Removes the
TCP-loopback asymmetry from § 2. Exposes the substrate's real
in-process cost.

### v0.7 — `bench/bench_stress.py`

The historical "Tier 1 stress" numbers (16 subscribers × 100 events;
10K × 128-dim vector insert; 10K change-log events) ran during
v0.5.x substrate validation but the script wasn't preserved.
Re-runs under v0.7+ with reproducible output.

### v0.7 — `bench/bench_startup.py`

Cold-start latency, memory footprint at idle, WAL recovery time —
all called out in agent-audit feedback but not yet measured.

---

## 5. What this document is not

- Not marketing. There are categories where CuttleDB loses to
  specialized engines (Qdrant on pure vector throughput at scale;
  Postgres on complex multi-table joins; ClickHouse on petabyte
  OLAP). CuttleDB optimizes for "many-database surface in one
  binary," not "fastest at any single axis."

- Not stable across machines. Re-run the scripts. We commit to
  honest methodology, not to specific absolute numbers.

- Not the final word. Every section here has a follow-up planned in
  § 4. v0.7's benchmark suite is the next iteration, not the last.

---

If you find a result here that doesn't reproduce on your machine,
that's exactly what we want to hear about. Open an issue with the
script, your output, your hardware, and we'll either fix the bench,
fix the substrate, or update the documented expectations.
