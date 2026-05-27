"""CuttleDB vs SQLite — head-to-head on a small in-memory workload.

Reproducible bench backing the SQLite numbers in bench/RESULTS.md
§ 2. Run it; you'll get your own honest numbers. The full
methodology + measured-then framing lives in RESULTS.md; this
file's docstring is the short version.

Honest framing:
  - SQLite runs **in-process** (sqlite3 stdlib + `:memory:`).
  - CuttleDB runs over **TCP loopback** (the production deployment
    shape; the WASM in-process build exists but isn't first-class
    yet).

That is NOT an apples-to-apples comparison on the latency axis —
SQLite skips the kernel networking stack entirely. We report it
that way because it matches the deployment model most users hit:
SQLite gets embedded; CuttleDB gets a port. The ratios you'd
expect:

  - Read-heavy ops with bulk results (SELECT WHERE returning many
    rows) — CuttleDB's SIMD scan + cached aggregates can beat
    SQLite's per-row B-tree walk even with the network round-trip
    overhead.
  - Per-call ops with small results (single SUM, single COUNT) —
    SQLite wins on raw latency because there's no network at all.
  - Bulk inserts — depends on pipelining; CuttleDB's
    INSERT_BATCH batches efficiently.

Run:

    # 1. Start a CuttleDB server:
    cuttledb-server --port 7780

    # 2. Run the bench:
    python bench/bench_sqlite.py

Environment:
    CUTTLEDB_HOST (default 127.0.0.1)
    CUTTLEDB_PORT (default 7780)
    BENCH_ROWS    (default 1000)
    BENCH_ITERS   (default 100)

Output: a printed table of medians (ms) per op, and the multiplier
relative to SQLite. We use medians not means so an outlier (Windows
scheduler jitter, GC pause, etc.) doesn't skew the headline.
"""
from __future__ import annotations

import os
import random
import sqlite3
import statistics
import sys
import time
from typing import Callable, List, Tuple

try:
    from cuttledb import CuttleDB, ColType, Op
except ImportError:
    print("error: cuttledb package not importable. Install with:")
    print("    pip install -e adapters/python")
    sys.exit(1)


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7780"))
ROWS = int(os.environ.get("BENCH_ROWS", "1000"))
ITERS = int(os.environ.get("BENCH_ITERS", "100"))


def timed(fn: Callable[[], None]) -> float:
    """Return the wall time of one call to fn(), in milliseconds."""
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def median_ms(fn: Callable[[], None], n: int) -> float:
    """Run fn() n times, report median wall time per call (ms)."""
    samples = [timed(fn) for _ in range(n)]
    return statistics.median(samples)


def make_rows(n: int, seed: int = 42) -> List[Tuple[str, int]]:
    rng = random.Random(seed)
    # name = random 8-char string; value = random int 0..10000.
    return [(f"row_{rng.randint(0, 10**9)}", rng.randint(0, 10000))
            for _ in range(n)]


# ─── SQLite path ───────────────────────────────────────────────────────────

def sqlite_setup(rows: List[Tuple[str, int]]) -> sqlite3.Connection:
    """Fresh `:memory:` DB with synchronous OFF (matches the README
    config we're benchmarking against). Pre-populated with `rows`."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("CREATE TABLE t (name TEXT, value INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    return conn


def bench_sqlite(rows: List[Tuple[str, int]]) -> dict:
    """Return dict of {op_name: median_ms_per_op}."""
    out = {}

    # INSERT 1000 (bulk) — fresh table each iteration
    def insert_bulk():
        c = sqlite3.connect(":memory:")
        c.execute("PRAGMA synchronous=OFF")
        c.execute("CREATE TABLE t (name TEXT, value INTEGER)")
        c.executemany("INSERT INTO t VALUES (?, ?)", rows)
        c.commit()
        c.close()
    out["INSERT 1000 (bulk)"] = median_ms(insert_bulk, 30)

    # Steady-state setup for the read-side ops
    conn = sqlite_setup(rows)
    cur = conn.cursor()

    def sum_call():
        cur.execute("SELECT SUM(value) FROM t").fetchone()
    out["SUM × 1"] = median_ms(sum_call, ITERS)

    def min_call():
        cur.execute("SELECT MIN(value) FROM t").fetchone()
    out["MIN × 1"] = median_ms(min_call, ITERS)

    def count_where_call():
        cur.execute("SELECT COUNT(*) FROM t WHERE value > 5000").fetchone()
    out["COUNT WHERE × 1"] = median_ms(count_where_call, ITERS)

    def select_where_call():
        cur.execute("SELECT * FROM t WHERE value > 9000").fetchall()
    out["SELECT WHERE × 1"] = median_ms(select_where_call, ITERS)

    conn.close()
    return out


# ─── CuttleDB path ─────────────────────────────────────────────────────────

def cuttledb_bench(rows: List[Tuple[str, int]]) -> dict:
    out = {}

    # INSERT 1000 (bulk) via insert_batch (pipelined)
    def insert_bulk():
        db = CuttleDB.connect(HOST, PORT)
        hid = db.open()
        tid = db.create(hid, "t", [("name", ColType.STRING),
                                      ("value", ColType.INT)])
        db.insert_batch(hid, tid, [list(r) for r in rows])
        db.close()
    out["INSERT 1000 (bulk)"] = median_ms(insert_bulk, 30)

    # Steady-state setup
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING),
                                  ("value", ColType.INT)])
    db.insert_batch(hid, tid, [list(r) for r in rows])

    def sum_call():
        db.sum(hid, tid, 1)
    out["SUM × 1"] = median_ms(sum_call, ITERS)

    def min_call():
        db.min(hid, tid, 1)
    out["MIN × 1"] = median_ms(min_call, ITERS)

    def count_where_call():
        db.fcount_gt(hid, tid, 1, 5000)
    out["COUNT WHERE × 1"] = median_ms(count_where_call, ITERS)

    def select_where_call():
        db.select_gt(hid, tid, 1, 9000)
    out["SELECT WHERE × 1"] = median_ms(select_where_call, ITERS)

    db.close()
    return out


# ─── Report ────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"# CuttleDB vs SQLite — reproducible bench")
    print(f"# rows={ROWS}, iters per op={ITERS}, "
          f"CuttleDB at {HOST}:{PORT} (TCP loopback)")
    print()

    # Sanity: can we reach CuttleDB?
    try:
        db = CuttleDB.connect(HOST, PORT, timeout=2.0)
        db.ping()
        db.close()
    except Exception as e:
        print(f"error: cannot reach CuttleDB at {HOST}:{PORT}: {e}", file=sys.stderr)
        print("Start a server with: cuttledb-server --port " + str(PORT),
              file=sys.stderr)
        return 1

    rows = make_rows(ROWS)

    print("Measuring SQLite...")
    sqlite_t = bench_sqlite(rows)
    print("Measuring CuttleDB...")
    cuttledb_t = cuttledb_bench(rows)

    print()
    print(f"| Op | CuttleDB (TCP) | SQLite (in-proc) | Ratio (SQLite / CuttleDB) |")
    print(f"|---|---|---|---|")
    for op in ["INSERT 1000 (bulk)", "SUM × 1", "MIN × 1",
               "COUNT WHERE × 1", "SELECT WHERE × 1"]:
        c = cuttledb_t[op]
        s = sqlite_t[op]
        ratio = s / c if c > 0 else float("inf")
        if ratio >= 1.0:
            verdict = f"**{ratio:.1f}× CuttleDB faster**"
        else:
            verdict = f"SQLite {1/ratio:.1f}× faster"
        print(f"| {op} | {c:.3f} ms | {s:.3f} ms | {verdict} |")

    print()
    print("# Notes:")
    print("# - SQLite runs in-process via the Python stdlib `sqlite3` module")
    print("#   with `PRAGMA synchronous=OFF, journal_mode=MEMORY`.")
    print("# - CuttleDB runs over TCP loopback — the comparison is NOT")
    print("#   apples-to-apples on latency (CuttleDB pays the network")
    print("#   round-trip cost SQLite doesn't). We report it this way")
    print("#   because it matches the deployment model most users hit.")
    print("# - Medians of N iterations, not means. Outliers (scheduler")
    print("#   jitter, GC pauses) don't skew the headline numbers.")
    print(f"# - rows={ROWS}, iters={ITERS}. Tune via BENCH_ROWS / BENCH_ITERS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
