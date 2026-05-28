"""HNSW vs brute-force benchmark for CuttleDB v0.5.16.

Measures, at multiple (N, dim) scales:
  - Insert throughput
  - Brute-force KNN latency (no index)
  - HNSW build time
  - HNSW KNN latency
  - Recall@10 (HNSW vs brute-force ground truth)
  - SAVE / LOAD time + snapshot size on disk
  - KNN latency after LOAD (no rebuild)

Run:
    # Start server first:
    #   cuttledb-server --port 7790
    cd CuttleDB/adapters/python
    CUTTLEDB_PORT=7790 python ../../bench/bench_hnsw.py

Scales (override with --scales N1xD1,N2xD2):
    10000x128, 100000x128

To run a 1M scale add e.g. --scales 1000000x64 (needs ~256MB free RAM on
the server side just for the vectors). Skipped by default to keep
default-run times bounded.
"""

import argparse
import math
import os
import random
import sys
import tempfile
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ADAPTER_DIR = os.path.normpath(os.path.join(THIS_DIR, "..", "adapters", "python"))
sys.path.insert(0, ADAPTER_DIR)

from cuttledb import ColType, CuttleDB


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7790"))


def make_vecs(n, dim, seed):
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(dim)] for _ in range(n)]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def brute_topk(vecs, query, k):
    return sorted(
        [(i, cosine(query, v)) for i, v in enumerate(vecs)],
        key=lambda p: -p[1],
    )[:k]


def banner(s):
    line = "=" * len(s)
    print(f"\n{line}\n{s}\n{line}")


def bench_scale(db, N, dim, n_queries=20, k=10, do_save_load=True):
    banner(f"scale: N={N:,} dim={dim} (k={k}, queries={n_queries})")

    hid = db.open()
    tid = db.create(hid, f"b_{N}_{dim}", [("v", ColType.VEC, dim)])

    # ── insert ───────────────────────────────────────────────────────
    t0 = time.perf_counter()
    vecs = make_vecs(N, dim, seed=N + dim)
    t_gen = time.perf_counter() - t0
    print(f"  generated {N:,} vectors of dim {dim} ({t_gen*1000:.0f}ms)")

    t0 = time.perf_counter()
    # Pipeline in chunks of 4096 to bound memory of the wire batch. Each
    # row is a list of column values; we have one column (the vec), so
    # wrap each vector as [vec].
    chunk = 4096
    for i in range(0, N, chunk):
        db.insert_batch(hid, tid, [[v] for v in vecs[i : i + chunk]])
    t_insert = time.perf_counter() - t0
    print(f"  inserted {N:,} rows in {t_insert:.2f}s "
          f"({N / t_insert:,.0f} rows/s)")

    # Pick query vectors. Use existing rows so top-1 is self (recall sanity).
    rng = random.Random(99)
    query_idx = [rng.randrange(N) for _ in range(n_queries)]
    queries = [vecs[i] for i in query_idx]

    # ── brute-force KNN baseline (no index) ──────────────────────────
    print(f"  -- brute-force KNN (no HNSW) --")
    t0 = time.perf_counter()
    bf_results = []
    for q in queries:
        bf_results.append(db.knn(hid, tid, 0, k, q))
    t_bf = time.perf_counter() - t0
    bf_per_q = t_bf / n_queries * 1000
    print(f"    {n_queries} queries in {t_bf*1000:.0f}ms "
          f"({bf_per_q:.2f}ms / query)")

    # Verify brute force is correct (top-1 = self for each).
    for qi, res in zip(query_idx, bf_results):
        assert res[0][0] == qi, f"brute-force top-1 wrong: q={qi} got {res[0]}"

    # ── HNSW build ───────────────────────────────────────────────────
    print(f"  -- HNSW build --")
    t0 = time.perf_counter()
    n_indexed = int(db.send(f"INDEX {hid} {tid} 0 HNSW"))
    t_build = time.perf_counter() - t0
    assert n_indexed == N, f"INDEX returned {n_indexed}, expected {N}"
    print(f"    built in {t_build:.2f}s ({N / t_build:,.0f} nodes/s)")

    # ── HNSW KNN ─────────────────────────────────────────────────────
    print(f"  -- HNSW KNN --")
    t0 = time.perf_counter()
    hnsw_results = []
    for q in queries:
        hnsw_results.append(db.knn(hid, tid, 0, k, q))
    t_hnsw = time.perf_counter() - t0
    hnsw_per_q = t_hnsw / n_queries * 1000
    print(f"    {n_queries} queries in {t_hnsw*1000:.0f}ms "
          f"({hnsw_per_q:.2f}ms / query)")
    speedup = bf_per_q / hnsw_per_q if hnsw_per_q > 0 else float("inf")
    print(f"    speedup vs brute force: {speedup:.1f}x")

    # ── Recall@k ─────────────────────────────────────────────────────
    total_hits = 0
    total_possible = 0
    top1_correct = 0
    score_loss_sum = 0.0
    for bf, hn in zip(bf_results, hnsw_results):
        bf_ids = {rid for rid, _ in bf}
        hn_ids = {rid for rid, _ in hn}
        total_hits += len(bf_ids & hn_ids)
        total_possible += k
        if bf[0][0] == hn[0][0]:
            top1_correct += 1
        # Score loss: how much worse is HNSW's k-th result vs BF's k-th.
        # With tight top-Ks of random vectors this can be tiny while id recall looks low.
        score_loss_sum += bf[-1][1] - hn[-1][1]
    recall = total_hits / total_possible
    avg_score_loss = score_loss_sum / n_queries
    print(f"    recall@{k} = {recall:.3f} "
          f"(top-1 correct {top1_correct}/{n_queries}, "
          f"avg k-th score loss {avg_score_loss:.4f})")

    # ── SAVE / LOAD ──────────────────────────────────────────────────
    if do_save_load:
        print(f"  -- SAVE / LOAD --")
        snap_path = os.path.join(
            tempfile.gettempdir(),
            f"cuttledb_bench_{N}_{dim}.cuttledb",
        ).replace("\\", "/")

        t0 = time.perf_counter()
        db.save(hid, snap_path)
        t_save = time.perf_counter() - t0
        try:
            snap_bytes = os.path.getsize(snap_path)
        except OSError:
            snap_bytes = -1
        print(f"    SAVE: {t_save*1000:.0f}ms "
              f"({snap_bytes / (1024*1024):.1f}MB on disk)")

        t0 = time.perf_counter()
        new_hid = db.load(snap_path)
        t_load = time.perf_counter() - t0
        print(f"    LOAD: {t_load*1000:.0f}ms")

        # Verify post-load KNN identical
        post_load = db.knn(new_hid, 0, 0, k, queries[0])
        assert post_load[0] == hnsw_results[0][0], (
            f"post-LOAD KNN diverged: {post_load[0]} vs {hnsw_results[0][0]}"
        )
        print(f"    post-LOAD KNN matches pre-SAVE")
        try:
            os.remove(snap_path)
        except OSError:
            pass

    return {
        "N": N,
        "dim": dim,
        "insert_s": t_insert,
        "bf_per_q_ms": bf_per_q,
        "build_s": t_build,
        "hnsw_per_q_ms": hnsw_per_q,
        "speedup": speedup,
        "recall": recall,
    }


def main():
    ap = argparse.ArgumentParser(description="HNSW vs brute-force benchmark")
    ap.add_argument(
        "--scales",
        default="10000x128,100000x128",
        help="Comma-separated N x dim scales (e.g., 10000x128,100000x128)",
    )
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--no-save-load", action="store_true",
                    help="Skip SAVE/LOAD timing")
    args = ap.parse_args()

    scales = []
    for spec in args.scales.split(","):
        spec = spec.strip()
        if not spec:
            continue
        n_str, _, d_str = spec.partition("x")
        scales.append((int(n_str), int(d_str)))

    # Big timeouts: INDEX HNSW at 100K+ takes 60-300s; the adapter's default
    # 10s recv timeout aborts mid-build. Bump to 10min.
    db = CuttleDB.connect(HOST, PORT, timeout=600.0)
    print(f"connected to {HOST}:{PORT}")

    summary = []
    for N, dim in scales:
        s = bench_scale(
            db, N, dim,
            n_queries=args.queries,
            k=args.k,
            do_save_load=not args.no_save_load,
        )
        summary.append(s)

    banner("SUMMARY")
    headers = ["N", "dim", "insert(s)", "bf(ms/q)", "build(s)", "hnsw(ms/q)",
               "speedup", "recall"]
    print(f"  {'  '.join(f'{h:>10}' for h in headers)}")
    for s in summary:
        print(f"  {s['N']:>10,}  {s['dim']:>10}  "
              f"{s['insert_s']:>10.2f}  {s['bf_per_q_ms']:>10.2f}  "
              f"{s['build_s']:>10.2f}  {s['hnsw_per_q_ms']:>10.2f}  "
              f"{s['speedup']:>10.1f}  {s['recall']:>10.3f}")


if __name__ == "__main__":
    main()
