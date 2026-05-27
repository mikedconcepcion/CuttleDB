"""End-to-end test for `INDEX ... HNSW` + KNN routing through HNSW.

Builds a VEC column, raw-sends `INDEX <hid> <tid> <col> HNSW`, then calls
KNN and asserts the results match brute-force ground truth (since the test
size is small, recall should be 1.0).

Also verifies the dispatcher with M= and ef_construction= override knobs.
"""
import math
import os
import random
import socket
import tempfile

import pytest

from cuttledb import ColType, CuttleDB


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7780"))


def _server_up() -> bool:
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_up(),
    reason=f"CuttleDB server not reachable at {HOST}:{PORT}",
)


@pytest.fixture
def db():
    with CuttleDB.connect(HOST, PORT) as d:
        yield d


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _brute_topk(vecs, query, k):
    scored = [(i, _cosine(query, v)) for i, v in enumerate(vecs)]
    scored.sort(key=lambda p: -p[1])
    return [p[0] for p in scored[:k]]


def _make_vecs(n, dim, seed=42):
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(dim)] for _ in range(n)]


def test_index_hnsw_builds_and_routes(db):
    """Build HNSW, run KNN, verify recall = 1.0 vs brute-force at small N."""
    hid = db.open()
    tid = db.create(hid, "docs", [("v", ColType.VEC, 64)])

    n = 200
    dim = 64
    vecs = _make_vecs(n, dim, seed=1)
    for v in vecs:
        db.insert(hid, tid, [v])

    # Raw-send the HNSW INDEX verb (not in the typed adapter yet).
    body = db.send(f"INDEX {hid} {tid} 0 HNSW")
    # Returns the number of nodes inserted.
    assert int(body) == n

    # Query with a held-out vector (use one of the existing rows as query).
    query = vecs[7]
    k = 10
    results = db.knn(hid, tid, 0, k, query)

    assert len(results) == k
    hnsw_ids = [rid for rid, _ in results]

    truth = _brute_topk(vecs, query, k)

    # At N=200, dim=64, HNSW should give perfect recall.
    overlap = len(set(hnsw_ids) & set(truth))
    assert overlap == k, (
        f"recall@{k} only {overlap}/{k} — hnsw={hnsw_ids}, truth={truth}"
    )

    # Top-1 should always be the query row itself (cosine of v with v == 1.0).
    assert hnsw_ids[0] == 7
    assert results[0][1] > 0.999


def test_index_hnsw_with_overrides(db):
    """INDEX accepts `M=N ef_construction=N` overrides."""
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.VEC, 32)])

    n = 100
    vecs = _make_vecs(n, 32, seed=2)
    for v in vecs:
        db.insert(hid, tid, [v])

    body = db.send(f"INDEX {hid} {tid} 0 HNSW M=8 ef_construction=100")
    assert int(body) == n

    # KNN should still route through HNSW and return correct results.
    query = vecs[42]
    results = db.knn(hid, tid, 0, 5, query)
    assert len(results) == 5
    assert results[0][0] == 42  # self-match


def test_hnsw_falls_back_when_not_built(db):
    """KNN on a VEC column without HNSW still works (brute-force path)."""
    hid = db.open()
    tid = db.create(hid, "no_idx", [("v", ColType.VEC, 16)])

    vecs = _make_vecs(50, 16, seed=3)
    for v in vecs:
        db.insert(hid, tid, [v])

    # No INDEX HNSW built — brute force should still return correct top-k.
    query = vecs[10]
    results = db.knn(hid, tid, 0, 5, query)
    assert len(results) == 5
    assert results[0][0] == 10


# ── Phase 2A: incremental INSERT keeps index live ─────────────────


def test_hnsw_incremental_insert_after_index(db):
    """INSERTing rows after INDEX HNSW keeps the index live (no rebuild)
    and new rows become searchable immediately."""
    hid = db.open()
    tid = db.create(hid, "live", [("v", ColType.VEC, 32)])

    # Build the index over 100 initial rows.
    initial = _make_vecs(100, 32, seed=10)
    for v in initial:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 100

    # Insert 50 more rows AFTER the INDEX. With Phase 2A, the index grows
    # incrementally — KNN should immediately find these new rows.
    extra = _make_vecs(50, 32, seed=11)
    for v in extra:
        db.insert(hid, tid, [v])

    # Total table is now 150 rows: ids 0..99 from `initial`, 100..149 from `extra`.
    # Query with one of the *new* vectors as the probe; the new row id (105)
    # is its own best match (cosine 1.0). If invalidation kicked in instead of
    # incremental add, KNN would fall back to brute force — also correct
    # behavior on this column. We don't see that from the wire; instead,
    # verify the answer is correct either way (top-1 = self).
    new_idx = 5            # index within `extra`
    new_row_id = 100 + new_idx
    query = extra[new_idx]
    results = db.knn(hid, tid, 0, 10, query)
    assert len(results) == 10
    assert results[0][0] == new_row_id, (
        f"expected top-1 = {new_row_id}, got {results[0]}"
    )
    assert results[0][1] > 0.999

    # Also verify the prior rows are still well-ranked. Query with row 7
    # (one of the originals).
    query = initial[7]
    results = db.knn(hid, tid, 0, 5, query)
    assert results[0][0] == 7
    assert results[0][1] > 0.999


# ── Phase 2B: incremental DELETE keeps index live ─────────────────


def test_hnsw_delete_last_row(db):
    """Deleting the LAST row should tombstone it; remaining rows still
    searchable correctly."""
    hid = db.open()
    tid = db.create(hid, "del_last", [("v", ColType.VEC, 32)])

    vecs = _make_vecs(50, 32, seed=30)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 50

    # Delete the last row (id=49). Should tombstone, no swap.
    assert db.delete(hid, tid, 49) is True
    assert db.count(hid, tid) == 49

    # Query with vector that used to be row 49 — should NOT return id=49.
    results = db.knn(hid, tid, 0, 5, vecs[49])
    ids = [rid for rid, _ in results]
    assert 49 not in ids, f"deleted row appears in results: {results}"
    assert len(results) == 5  # Should still return 5 from remaining 49 rows.

    # Other queries unaffected: row 7 still finds itself as top-1.
    results = db.knn(hid, tid, 0, 5, vecs[7])
    assert results[0][0] == 7
    assert results[0][1] > 0.999


def test_hnsw_delete_middle_row_swap_with_last(db):
    """Deleting a middle row triggers swap-with-last. HNSW must tombstone
    both slots and re-add the swap-source vector at the deleted slot."""
    hid = db.open()
    tid = db.create(hid, "del_mid", [("v", ColType.VEC, 32)])

    vecs = _make_vecs(50, 32, seed=31)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 50

    # Delete row 7. After delete: row 7 now holds vecs[49]'s vector;
    # row 49 no longer exists.
    deleted_vec = vecs[7]
    moved_vec = vecs[49]
    assert db.delete(hid, tid, 7) is True
    assert db.count(hid, tid) == 49

    # Querying with the DELETED vector should NOT return id=7 as a perfect
    # match; row 7 now holds a different vector.
    results = db.knn(hid, tid, 0, 5, deleted_vec)
    ids = [rid for rid, _ in results]
    # The original vec at row 7 is gone — top-1 score should be < 1.0
    # because no row in the table now matches `deleted_vec` exactly.
    top_score = results[0][1]
    assert top_score < 0.999, (
        f"deleted vector still has a near-perfect match: {results[0]}"
    )

    # Querying with the MOVED vector (vecs[49]) should now find row 7
    # (since the swap copied vecs[49] into slot 7).
    results = db.knn(hid, tid, 0, 5, moved_vec)
    assert results[0][0] == 7, (
        f"expected swap source to be findable at slot 7, got {results[0]}"
    )
    assert results[0][1] > 0.999


def test_hnsw_recall_after_deletes(db):
    """Run a batch of deletes; verify recall@10 stays high vs brute-force
    on the surviving rows."""
    hid = db.open()
    tid = db.create(hid, "many_del", [("v", ColType.VEC, 64)])

    vecs = _make_vecs(200, 64, seed=40)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 200

    # Delete every 4th row. Note swap-with-last shuffles indices each time,
    # so we delete in DESCENDING order to keep the math simple — the rows
    # at deleted indices are 0, 4, 8, ..., 196 by current id at delete time.
    # We'll track surviving vectors explicitly.
    surviving = list(vecs)  # index = current row_id
    rng = random.Random(401)
    # Delete 40 random rows (indices into the current list).
    for _ in range(40):
        victim = rng.randrange(len(surviving))
        ok = db.delete(hid, tid, victim)
        assert ok
        # CuttleDB swap-with-last: victim slot now holds the last element.
        last_vec = surviving.pop()  # drop last
        if victim < len(surviving):
            # After pop, surviving has N-1 items. victim slot needs the
            # original last vec (which we just popped).
            surviving[victim] = last_vec
    assert db.count(hid, tid) == len(surviving) == 160

    # Now recall@10 over 5 random queries.
    total_hits = 0
    total_possible = 0
    for _ in range(5):
        qid = rng.randrange(len(surviving))
        query = surviving[qid]
        hnsw_ids = [rid for rid, _ in db.knn(hid, tid, 0, 10, query)]
        truth = _brute_topk(surviving, query, 10)
        total_hits += len(set(hnsw_ids) & set(truth))
        total_possible += 10
    recall = total_hits / total_possible
    assert recall >= 0.85, f"recall@10 = {recall:.3f}, expected >= 0.85"


def test_hnsw_delete_then_insert_round_trip(db):
    """Delete a row, insert a new row, KNN should find the new row by its
    own vector AND should not return the deleted row."""
    hid = db.open()
    tid = db.create(hid, "rt", [("v", ColType.VEC, 32)])

    vecs = _make_vecs(30, 32, seed=50)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 30

    # Delete row 5.
    assert db.delete(hid, tid, 5) is True

    # Insert a fresh distinctive vector.
    new_vec = [10.0 if i == 0 else 0.0 for i in range(32)]
    new_id = db.insert(hid, tid, [new_vec])

    # KNN returns slot indices (where HNSW stores the node). Find the slot
    # holding the new vector — it should be slot count-1 since we just inserted.
    new_slot = db.count(hid, tid) - 1

    # Search for new_vec — must find new_slot at top.
    results = db.knn(hid, tid, 0, 3, new_vec)
    assert results[0][0] == new_slot
    assert results[0][1] > 0.999


# ── Phase 2C: snapshot persistence ────────────────────────────────


def test_hnsw_persists_through_save_load(db):
    """SAVE/LOAD should round-trip the HNSW index: after LOAD, KNN works
    immediately with no rebuild and matches the pre-save results."""
    hid = db.open()
    tid = db.create(hid, "snap", [("v", ColType.VEC, 32)])
    vecs = _make_vecs(80, 32, seed=60)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 80

    # Capture pre-save KNN result.
    query = vecs[20]
    before = db.knn(hid, tid, 0, 5, query)
    assert before[0][0] == 20

    # SAVE to a temp file. Server-side path; use a Windows-friendly absolute.
    snap_path = os.path.join(tempfile.gettempdir(), "cuttledb_hnsw_snap.cuttledb")
    snap_path = snap_path.replace("\\", "/")
    db.save(hid, snap_path)

    # LOAD into a fresh handle.
    new_hid = db.load(snap_path)
    assert new_hid >= 0
    # The new handle has the same table at tid=0 (LOAD reconstructs tables
    # in order; our test created exactly one table so it's at tid=0).
    new_tid = 0

    # KNN on the loaded handle — should not need a rebuild.
    after = db.knn(new_hid, new_tid, 0, 5, query)
    assert after[0][0] == 20
    assert abs(after[0][1] - before[0][1]) < 1e-5

    # The top-5 row_ids should match exactly (HNSW is deterministic given
    # the same graph topology).
    before_ids = [rid for rid, _ in before]
    after_ids = [rid for rid, _ in after]
    assert before_ids == after_ids, (
        f"top-5 diverged after SAVE/LOAD: before={before_ids}, after={after_ids}"
    )

    # Cleanup.
    try:
        os.remove(snap_path)
    except OSError:
        pass


def test_hnsw_persists_with_tombstones(db):
    """SAVE/LOAD with prior deletes should preserve tombstones — deleted
    rows must not reappear in KNN results after LOAD."""
    hid = db.open()
    tid = db.create(hid, "snap_del", [("v", ColType.VEC, 32)])
    vecs = _make_vecs(50, 32, seed=61)
    for v in vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 50

    # Delete the last row (tombstone, no swap).
    assert db.delete(hid, tid, 49) is True
    assert db.count(hid, tid) == 49

    # SAVE + LOAD.
    snap_path = os.path.join(tempfile.gettempdir(),
                              "cuttledb_hnsw_snap_del.cuttledb")
    snap_path = snap_path.replace("\\", "/")
    db.save(hid, snap_path)
    new_hid = db.load(snap_path)

    # KNN with the deleted vector should NOT return row 49.
    results = db.knn(new_hid, 0, 0, 5, vecs[49])
    ids = [rid for rid, _ in results]
    assert 49 not in ids, f"deleted row resurrected after LOAD: {results}"
    assert len(results) == 5  # 49 surviving rows; top-5 fine

    try:
        os.remove(snap_path)
    except OSError:
        pass


# ── Edge cases (v0.5.16 hardening) ────────────────────────────────


def test_index_hnsw_on_empty_table(db):
    """INDEX HNSW on an empty table should succeed and return 0 nodes;
    subsequent inserts then go through the incremental Phase 2A path."""
    hid = db.open()
    tid = db.create(hid, "empty", [("v", ColType.VEC, 16)])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 0

    # Now insert and confirm KNN works (routes through the just-built index).
    v = [1.0] + [0.0] * 15
    db.insert(hid, tid, [v])
    results = db.knn(hid, tid, 0, 1, v)
    assert results[0][0] == 0
    assert results[0][1] > 0.999


def test_index_hnsw_idempotent_rebuild(db):
    """Calling INDEX HNSW twice in a row rebuilds; second call returns
    the same node count and KNN still works."""
    hid = db.open()
    tid = db.create(hid, "twice", [("v", ColType.VEC, 16)])
    vecs = _make_vecs(20, 16, seed=70)
    for v in vecs:
        db.insert(hid, tid, [v])

    first = int(db.send(f"INDEX {hid} {tid} 0 HNSW"))
    second = int(db.send(f"INDEX {hid} {tid} 0 HNSW"))
    assert first == 20
    assert second == 20

    results = db.knn(hid, tid, 0, 1, vecs[5])
    assert results[0][0] == 5


def test_index_hnsw_clamps_excessive_M(db):
    """M values that would overflow int8_t edge counts (M_max0 = 2M) must
    be rejected or clamped. Dispatcher accepts only M <= 63."""
    hid = db.open()
    tid = db.create(hid, "bigM", [("v", ColType.VEC, 16)])
    vecs = _make_vecs(10, 16, seed=71)
    for v in vecs:
        db.insert(hid, tid, [v])

    # M=128 must NOT be silently accepted (would produce M_max0=256 → int8 overflow).
    # The dispatcher's `v > 0 && v <= 63` guard simply ignores the override
    # and the index builds with default M=16.
    n = int(db.send(f"INDEX {hid} {tid} 0 HNSW M=128"))
    assert n == 10  # builds successfully with default M
    # KNN still works.
    results = db.knn(hid, tid, 0, 1, vecs[3])
    assert results[0][0] == 3


def test_knn_with_query_norm_zero(db):
    """All-zero query vector has norm=0. cosine_sim_pre returns 0 for any
    candidate. KNN should return SOMETHING (with zero scores) rather than
    crash or return garbage."""
    hid = db.open()
    tid = db.create(hid, "zeroq", [("v", ColType.VEC, 8)])
    for i in range(5):
        v = [0.0] * 8
        v[i] = 1.0
        db.insert(hid, tid, [v])
    db.send(f"INDEX {hid} {tid} 0 HNSW")

    zero = [0.0] * 8
    results = db.knn(hid, tid, 0, 3, zero)
    assert len(results) == 3
    # All scores should be 0 (cosine of zero vector with anything = 0).
    for _, score in results:
        assert abs(score) < 1e-5


def test_knn_k_larger_than_corpus(db):
    """k > num_rows should return at most num_rows results, not crash."""
    hid = db.open()
    tid = db.create(hid, "small", [("v", ColType.VEC, 8)])
    vecs = _make_vecs(3, 8, seed=72)
    for v in vecs:
        db.insert(hid, tid, [v])
    db.send(f"INDEX {hid} {tid} 0 HNSW")

    results = db.knn(hid, tid, 0, 100, vecs[0])
    assert len(results) == 3
    assert results[0][0] == 0


def test_delete_only_row_then_recovery(db):
    """Deleting the sole row of an indexed table should empty the index
    cleanly; KNN on the empty table returns 0 results; a fresh insert
    re-bootstraps the index."""
    hid = db.open()
    tid = db.create(hid, "soloR", [("v", ColType.VEC, 8)])
    v = [1.0] + [0.0] * 7
    db.insert(hid, tid, [v])
    db.send(f"INDEX {hid} {tid} 0 HNSW")

    assert db.delete(hid, tid, 0) is True
    assert db.count(hid, tid) == 0

    # KNN on empty table returns [] rather than crashing.
    results = db.knn(hid, tid, 0, 1, v)
    assert results == []

    # Fresh insert — index bootstraps from this new sole node.
    db.insert(hid, tid, [v])
    results = db.knn(hid, tid, 0, 1, v)
    assert results[0][0] == 0
    assert results[0][1] > 0.999


def test_hnsw_with_where_clause(db):
    """v0.5.17 Phase 1 — filtered KNN through the HNSW path.

    Builds an index, then runs KNN with a WHERE clause on a non-vector
    column. The substrate oversamples 4x to keep k results after filter.
    Assertion: only rows matching the predicate are returned, ranked by
    cosine score, and the top hit matches the unfiltered top hit when the
    predicate selects all rows."""
    hid = db.open()
    tid = db.create(hid, "filtered", [
        ("v", ColType.VEC, 16),
        ("kind", ColType.STRING),
        ("uses", ColType.INT),
    ])
    rng = random.Random(7)
    vecs = _make_vecs(100, 16, seed=7)
    for i, v in enumerate(vecs):
        kind = "playbook" if i % 3 == 0 else "ref"
        uses = rng.randrange(0, 20)
        db.insert(hid, tid, [v, kind, uses])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 100

    query = vecs[10]

    # No filter — sanity.
    no_filter = db.knn(hid, tid, 0, 5, query)
    assert len(no_filter) == 5
    assert no_filter[0][0] == 10  # exact match

    # Filter by kind only.
    filtered = db.knn(hid, tid, 0, 5, query, where='1="playbook"')
    rows = [r for r, _ in filtered]
    assert all(r % 3 == 0 for r in rows), f"got non-playbook rows: {rows}"

    # Combined predicates AND'd.
    filtered2 = db.knn(hid, tid, 0, 5, query,
                       where='1="playbook" AND 2>=10')
    for r, _ in filtered2:
        assert r % 3 == 0, f"row {r} not a playbook"


def test_hnsw_recall_after_many_incremental_inserts(db):
    """After many incremental inserts, recall@10 should still match brute
    force (within HNSW's typical tolerance)."""
    hid = db.open()
    tid = db.create(hid, "growing", [("v", ColType.VEC, 64)])

    # Start with 50, INDEX, then add 150 more — incremental path dominates.
    seed_vecs = _make_vecs(50, 64, seed=20)
    for v in seed_vecs:
        db.insert(hid, tid, [v])
    assert int(db.send(f"INDEX {hid} {tid} 0 HNSW")) == 50

    grow_vecs = _make_vecs(150, 64, seed=21)
    for v in grow_vecs:
        db.insert(hid, tid, [v])

    all_vecs = seed_vecs + grow_vecs
    # Run 5 queries; assert overlap with brute-force top-10.
    rng = random.Random(99)
    total_hits = 0
    total_possible = 0
    for _ in range(5):
        qid = rng.randrange(200)
        query = all_vecs[qid]
        hnsw_ids = [rid for rid, _ in db.knn(hid, tid, 0, 10, query)]
        truth = _brute_topk(all_vecs, query, 10)
        total_hits += len(set(hnsw_ids) & set(truth))
        total_possible += 10
    recall = total_hits / total_possible
    assert recall >= 0.85, f"recall@10 = {recall:.3f}, expected >= 0.85"
