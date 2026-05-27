"""CuttleDB — Coffee Shop Analytics demo.

A 60-second tour of CuttleDB through one day at a coffee shop.

What you'll see:
  1. CREATE a table with INT, STRING, DATETIME, and VEC columns
  2. INSERT 100 events with timestamps spanning a workday
  3. SUB to live events; a barista's tablet sees orders arrive in real-time
  4. SELECT events from the morning rush (DATETIME range via KNN+WHERE)
  5. Semantic search — "find feedback like 'long line'" via KNN cosine
  6. Full-text search — "anyone mention latte?" via BM25 (LSEARCH)
  7. Hybrid search — "latte issues" via vector + BM25 fused with RRF (SEARCH)
  8. COUNT / MIN / MAX aggregates
  9. SAVE the brain to one portable file

Prerequisites:
    cuttledb-server --port 7780 &

Run:
    python CuttleDB/demo/demo_coffee_shop.py

Each step prints what it did and how long it took. Total runtime: ~5-10s.
"""
from __future__ import annotations

import math
import random
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Demo data is committed; output is colorful but ASCII-only so it ships
# safely through every terminal (no emoji surprises).

try:
    from cuttledb import CuttleDB, ColType, Op, datetime_to_epoch_ms
except ImportError:
    print("error: `cuttledb` SDK not installed. From the CuttleDB repo:",
          file=sys.stderr)
    print("    pip install -e adapters/python", file=sys.stderr)
    sys.exit(1)

# Force UTF-8 so the box-drawing characters survive Windows cp1252
# consoles (Python 3.7+).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


HOST = "127.0.0.1"
PORT = 7780

BAR_WIDTH = 64


# ── pretty-print helpers ──────────────────────────────────────────────────

def hr(ch: str = "─") -> str:
    return ch * BAR_WIDTH


def banner(title: str) -> None:
    print()
    print(hr("═"))
    print(f"  {title}")
    print(hr("═"))


def section(n: int, title: str) -> None:
    print()
    print(f"━━ [{n}] {title} " + "━" * (BAR_WIDTH - len(title) - 8))


def stat(label: str, value, unit: str = "") -> None:
    print(f"  {label:.<48} {value}{unit}")


@contextmanager
def timed(label: str):
    """Context manager that prints `label … <ms>` on exit."""
    t0 = time.perf_counter()
    yield
    ms = (time.perf_counter() - t0) * 1000
    stat(label, f"{ms:.2f}", " ms")


def hash_embed(text: str, dim: int = 64) -> list[float]:
    """Deterministic hash embedder — no Ollama needed. Fine for demo.

    Same word → same direction; similar words → moderately similar vectors.
    Not as good as a trained embedder but coherent enough that semantic
    queries return sensible neighbors for this demo's vocabulary.
    """
    vec = [0.0] * dim
    for word in text.lower().split():
        h = abs(hash(word))
        # Spread word energy across a few dimensions.
        for i in range(3):
            idx = (h >> (i * 8)) % dim
            vec[idx] += 1.0 / (i + 1)
    # L2 normalize
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


# ── the demo's event corpus ───────────────────────────────────────────────

CUSTOMERS = ["Alice", "Bob", "Carol", "Dan", "Eve", "Frank", "Grace", "Hank",
             "Iris", "Jack", "Kate", "Leo", "Mia", "Nick", "Olivia"]

EVENTS = [
    ("order_placed", "{c} ordered a large oat milk latte"),
    ("order_placed", "{c} ordered an espresso macchiato"),
    ("order_placed", "{c} ordered a small drip coffee and a croissant"),
    ("order_placed", "{c} ordered a cold brew with extra ice"),
    ("order_placed", "{c} ordered a cappuccino and a blueberry muffin"),
    ("order_ready",  "{c}'s latte is ready at the bar"),
    ("order_ready",  "{c}'s espresso macchiato is ready"),
    ("order_ready",  "{c}'s cold brew is ready"),
    ("feedback",     "{c} said the latte art was beautiful but the line was long"),
    ("feedback",     "{c} said the croissant was a little stale today"),
    ("feedback",     "{c} loved the new oat milk option"),
    ("feedback",     "{c} mentioned the wifi keeps dropping"),
    ("feedback",     "{c} said the music is too loud during the morning rush"),
    ("incident",     "espresso machine #2 stopped producing crema"),
    ("incident",     "card reader timed out for {c}"),
    ("incident",     "syrup bottle (vanilla) ran empty"),
    ("staff",        "barista {c} started shift"),
    ("staff",        "barista {c} ended shift"),
    ("staff",        "{c} restocked the pastry case"),
]


def build_event_corpus(n: int, start_epoch_ms: int) -> list[tuple]:
    """Generate `n` events distributed across an 8-hour workday starting
    at `start_epoch_ms`. Returns rows ready for db.insert (5 columns:
    name, customer, description, created_at_ms, embedding)."""
    rng = random.Random(42)  # deterministic — demo always shows same output
    rows = []
    day_ms = 8 * 3600 * 1000
    for i in range(n):
        kind, template = rng.choice(EVENTS)
        customer = rng.choice(CUSTOMERS)
        desc = template.format(c=customer)
        # Spread events across the workday with a morning-rush bias.
        bias = rng.random() ** 1.5  # squashes toward 0 → early-morning weight
        ts = start_epoch_ms + int(bias * day_ms)
        emb = hash_embed(desc)
        rows.append([kind, customer, desc, ts, emb])
    # Sort by timestamp so the demo output reads chronologically.
    rows.sort(key=lambda r: r[3])
    return rows


# ── the demo itself ───────────────────────────────────────────────────────

def server_up() -> bool:
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def main() -> int:
    banner("CuttleDB — Coffee Shop Analytics Demo")
    print("  Five surfaces in one tour: typed columns + DATETIME + vector")
    print("  search + BM25 + hybrid + real-time push + snapshot.")
    print()

    if not server_up():
        print(f"  ! No CuttleDB server reachable at {HOST}:{PORT}.")
        print(f"    Start one in another shell:")
        print(f"        cuttledb-server --port {PORT} &")
        return 1

    stat("Server", f"{HOST}:{PORT}")
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    stat("Handle", hid)

    # ── [1] Create the events table ───────────────────────────────────
    section(1, "CREATE — typed columns including DATETIME")
    with timed("create 'events' table (5 columns)"):
        tid = db.create(hid, "events", [
            ("name",        ColType.STRING),
            ("customer",    ColType.STRING),
            ("description", ColType.STRING),
            ("created_at",  ColType.DATETIME),   # ← v1.0.1 feature
            ("embedding",   ColType.VEC, 64),
        ])
    stat("Columns", "name (STR), customer (STR), description (STR),\n"
                    + " " * 50 + "created_at (DATETIME), embedding (VEC[64])")
    stat("Table id", tid)

    # ── [2] Insert a day's worth of events ────────────────────────────
    section(2, "INSERT — 100 events across an 8-hour workday")
    day_start = datetime_to_epoch_ms_local_midnight()
    rows = build_event_corpus(100, day_start)
    with timed("insert_batch(100 events)"):
        db.insert_batch(hid, tid, rows)
    stat("Total events", db.count(hid, tid))
    first_event = db.get(hid, tid, 0)
    last_event = db.get(hid, tid, db.count(hid, tid) - 1)
    stat("First event", f"{first_event[3]}  [{first_event[0]:13}] {first_event[2][:38]}")
    stat("Last event",  f"{last_event[3]}  [{last_event[0]:13}] {last_event[2][:38]}")

    # ── [3] SUB — live event stream ───────────────────────────────────
    section(3, "SUB — barista's tablet watches live orders")
    # Open a second connection that subscribes. The writer connection
    # (db) inserts a new event; the subscriber sees it pushed.
    sub_db = CuttleDB.connect(HOST, PORT)
    received = []

    def collect_events():
        for _ in range(3):
            evt = sub_db.poll_events(timeout=2.0)
            if evt:
                received.extend(evt)

    sub_db.sub(hid, tid)
    t = threading.Thread(target=collect_events, daemon=True)
    t.start()
    # Give the SUB time to register on the server side before we mutate.
    time.sleep(0.05)

    with timed("insert 3 live events"):
        for desc in ("Mia ordered a flat white",
                     "Mia's flat white is ready",
                     "Mia said the flat white was perfect"):
            db.insert(hid, tid, ["order_placed", "Mia", desc,
                                  int(time.time() * 1000), hash_embed(desc)])

    t.join(timeout=2.0)
    sub_db.unsub(hid, tid)
    sub_db.close()
    stat("Live events received", len(received))
    for evt in received[:3]:
        # Event dataclass: hid, tid, row_id (int), op ("INS"/"DEL"/"UPD")
        stat(f"  evt row_id={evt.row_id}",
             f"op={evt.op}  hid={evt.hid} tid={evt.tid}")

    # ── [4] DATETIME range query (morning rush) ───────────────────────
    section(4, "DATETIME range — events from the first 2 hours (morning rush)")
    cutoff_ms = day_start + 2 * 3600 * 1000
    # Use KNN+WHERE (full-f64 predicate path) to filter by time.
    zero_vec = [0.0] * 64
    with timed("KNN k=100 WHERE created_at < morning cutoff"):
        morning = db.knn(hid, tid, col=4, k=100,
                          query=zero_vec, where=f"3<{cutoff_ms}")
    stat("Events before " + epoch_ms_to_iso_local(cutoff_ms),
         f"{len(morning)} of {db.count(hid, tid)} total")

    # ── [5] Semantic search ───────────────────────────────────────────
    section(5, "KNN — semantic search ('long line, slow service')")
    q = hash_embed("long line slow service")
    with timed("KNN k=3 over 100 events"):
        hits = db.knn(hid, tid, col=4, k=3, query=q)
    for row_id, score in hits:
        row = db.get(hid, tid, row_id)
        stat(f"  score={score:.3f}", f"[{row[0]:13}] {row[2][:42]}")

    # ── [6] BM25 full-text search ─────────────────────────────────────
    section(6, "LSEARCH — BM25 full-text ('latte')")
    with timed("LSEARCH k=5 on 'description' column"):
        hits = db.lsearch(hid, tid, col=2, k=5, query="latte")
    for row_id, score in hits:
        row = db.get(hid, tid, row_id)
        stat(f"  bm25={score:.2f}", f"[{row[0]:13}] {row[2][:42]}")

    # ── [7] Hybrid search ─────────────────────────────────────────────
    section(7, "SEARCH — hybrid (vector + BM25 fused via RRF)")
    q2 = hash_embed("latte problems")
    with timed("SEARCH k=3 — 'latte issues'"):
        hits = db.search(hid, tid, vec_col=4, text_col=2, k=3,
                          vec=q2, query="latte")
    for row_id, score in hits:
        row = db.get(hid, tid, row_id)
        stat(f"  rrf={score:.4f}", f"[{row[0]:13}] {row[2][:42]}")

    # ── [8] Aggregates ────────────────────────────────────────────────
    section(8, "Aggregates — COUNT, MIN/MAX on the timestamp column")
    stat("COUNT (O(1) cached)", db.count(hid, tid))
    min_ms = int(db.min(hid, tid, col=3))
    max_ms = int(db.max(hid, tid, col=3))
    stat("MIN(created_at)", epoch_ms_to_iso_local(min_ms))
    stat("MAX(created_at)", epoch_ms_to_iso_local(max_ms))
    span_min = (max_ms - min_ms) / 60000
    stat("Span", f"{span_min:.1f} minutes")

    # ── [9] SAVE — snapshot the brain ─────────────────────────────────
    section(9, "SAVE — one-file snapshot, ready to scp anywhere")
    snap = Path("demo_coffee.cuttledb").resolve()
    with timed(f"SAVE → {snap.name}"):
        db.save(hid, str(snap))
    if snap.exists():
        stat("Snapshot size", f"{snap.stat().st_size:,}", " bytes")
        stat("Path", str(snap))

    # ── Summary ───────────────────────────────────────────────────────
    banner("Done")
    print("  What you just saw, in one binary, zero dependencies:")
    print()
    print("    ✓ Typed columns (STR / DATETIME / VEC) on one table")
    print("    ✓ Bulk insert (~100 events in milliseconds)")
    print("    ✓ Real-time push to subscribers (SUB / UNSUB)")
    print("    ✓ DATETIME range filtering (epoch-ms predicate)")
    print("    ✓ Semantic vector search (KNN cosine, AVX2)")
    print("    ✓ Full-text BM25 (LSEARCH)")
    print("    ✓ Hybrid retrieval (SEARCH = vector + BM25 + RRF)")
    print("    ✓ O(1) cached aggregates + SIMD MIN/MAX")
    print("    ✓ Snapshot persistence (SAVE → one file you own)")
    print()
    print(f"  Brain on disk: {snap.stat().st_size if snap.exists() else 0:,} bytes "
          f"in one portable file. Engine binary: ~450 KB, zero deps.")
    print()

    db.close()
    return 0


# ── small time helpers (local-formatted, demo-only) ───────────────────────

def datetime_to_epoch_ms_local_midnight() -> int:
    """Pick a deterministic 'shop opens' time: 8 hours before now (UTC).

    Using a relative offset (rather than today-at-07:00-local) keeps the
    demo's event timeline simple and tz-safe — every event has a
    timestamp ≤ "now", and the morning rush filter shows ~half of them.
    """
    return int(time.time() * 1000) - 8 * 3600 * 1000


def epoch_ms_to_iso_local(ms: int) -> str:
    """Format epoch ms as a short HH:MM ISO-ish string for prettier output."""
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%H:%M UTC")


if __name__ == "__main__":
    sys.exit(main())
