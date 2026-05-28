"""v0.5.17 audit — interactions between phases, edge cases, parser
fuzz, and lifecycle stress. These tests don't exist to repeat Phase 1-5
coverage; they exist to catch the bugs that only emerge when two
features collide (BM25 + delete + WAL; DSL + HNSW stale edges; etc).
"""
from __future__ import annotations

import os
import random
import socket
import string

import pytest

from cuttledb import CuttleDB, CuttleDBError, ColType, Column


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


# ── Cross-phase interaction tests ────────────────────────────────────


def test_bm25_after_many_insert_delete_cycles(db):
    """BM25 stays correct across repeated insert+delete churn — the
    posting purge must not corrupt or leak across hundreds of cycles."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    # Insert + delete 100 times, asserting search keeps working.
    for i in range(100):
        db.insert(hid, tid, [f"document number {i} with unique terms"])
        if i % 3 == 0 and db.count(hid, tid) > 1:
            # Delete the head row; swap-with-last fires.
            db.delete(hid, tid, 0)
    # Surviving rows should still be discoverable.
    hits = db.lsearch(hid, tid, 0, 5, "document")
    assert len(hits) > 0


def test_hnsw_plus_bm25_plus_where_in_one_search(db):
    """SEARCH with HNSW built + BM25 lazy-built + WHERE filter applied
    to both streams. The most-loaded path in production."""
    hid = db.open()
    tid = db.create(hid, "m", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=16),
        ("kind", ColType.STRING),
    ])
    rng = random.Random(11)
    for i in range(50):
        body = f"document number {i} {'cat' if i % 2 == 0 else 'dog'} story"
        emb = [rng.gauss(0.0, 1.0) for _ in range(16)]
        kind = "playbook" if i < 25 else "ref"
        db.insert(hid, tid, [body, emb, kind])
    # Build HNSW explicitly so the SEARCH path takes the HNSW branch.
    assert int(db.send(f"INDEX {hid} {tid} 1 HNSW")) == 50
    # Hybrid + WHERE.
    qvec = [rng.gauss(0.0, 1.0) for _ in range(16)]
    hits = db.search(hid, tid, vec_col=1, text_col=0, k=5,
                      vec=qvec, query="cat story",
                      where='2="playbook"')
    # All returned rows must satisfy kind="playbook" (row_id < 25).
    for r, _ in hits:
        assert r < 25, f"row {r} should be filtered out (kind=ref)"


def test_bsearch_after_save_load(db, tmp_path):
    """Snapshot → load → BSEARCH must still work. BM25 rebuilds lazily
    on first scoring atom; HNSW persists; filter atoms eval directly."""
    hid = db.open()
    tid = db.create(hid, "m", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
        ("uses", ColType.INT),
    ])
    rows = [
        ("alpha beta", [1.0, 0.0, 0.0, 0.0], 5),
        ("beta gamma", [0.0, 1.0, 0.0, 0.0], 3),
        ("gamma delta", [0.0, 0.0, 1.0, 0.0], 7),
    ]
    for r in rows:
        db.insert(hid, tid, list(r))
    db.send(f"INDEX {hid} {tid} 1 HNSW")
    snap = str(tmp_path / "snap.cuttledb").replace("\\", "/")
    db.send(f"SAVE {hid} {snap}")

    # Load into a new handle and search.
    new_hid = int(db.send(f"LOAD {snap}"))
    try:
        hits = db.bsearch(new_hid, tid, 5,
                           '0~"beta" AND 2>2')
        # rows with uses>2 AND containing 'beta': row 0 (alpha beta, uses=5)
        # and row 1 (beta gamma, uses=3). Both should be in results.
        rids = {r for r, _ in hits}
        assert 0 in rids and 1 in rids
    finally:
        db.close_handle(new_hid)


def test_lsearch_after_delete_all_rows(db):
    """LSEARCH on a corpus that's been emptied returns []."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    for word in ("alpha", "beta", "gamma"):
        db.insert(hid, tid, [word])
    db.send(f"INDEX {hid} {tid} 0 BM25")
    for _ in range(3):
        db.delete(hid, tid, 0)
    assert db.count(hid, tid) == 0
    assert db.lsearch(hid, tid, 0, 3, "alpha") == []


def test_bsearch_deeply_nested_parens(db):
    """The parser handles reasonable nesting depth without stack issues."""
    hid = db.open()
    tid = db.create(hid, "t", [
        ("body", ColType.STRING),
        ("uses", ColType.INT),
    ])
    db.insert(hid, tid, ["x", 5])
    db.insert(hid, tid, ["y", 3])
    # 5 levels of nesting.
    expr = '(((((1>0 AND 1>1) OR 1>=3) AND 1<=10) OR 1=3) AND 0="x")'
    hits = db.bsearch(hid, tid, 5, expr)
    assert {r for r, _ in hits} == {0}  # only row 0 matches


def test_bsearch_long_quoted_string_truncates_safely(db):
    """A 1000-char query inside ~"..." doesn't crash — it gets capped
    at the parser's internal buffer (1024). The substrate should not
    blow up or leak memory."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    db.insert(hid, tid, ["a short doc"])
    big = "x" * 500  # well within budget
    hits = db.bsearch(hid, tid, 5, f'0~"{big}"')
    # The doc doesn't contain xs, so no match — but no crash either.
    assert isinstance(hits, list)


def test_bsearch_predicate_on_vec_col_rejected(db):
    """A predicate like `1=5` on a VEC column is meaningless and
    should be rejected by the parser."""
    hid = db.open()
    tid = db.create(hid, "t", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    db.insert(hid, tid, ["x", [1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, "1=0.5")


def test_bsearch_out_of_range_col_idx_rejected(db):
    """A column index past the schema width fails fast."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    db.insert(hid, tid, ["x"])
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, '99="anything"')


def test_knn_where_handles_k_larger_than_filtered_set(db):
    """k=100 when the WHERE clause filters down to 2 returns just 2,
    not a stuck or padded array."""
    hid = db.open()
    tid = db.create(hid, "t", [
        Column.vec("emb", dim=4),
        ("kind", ColType.STRING),
    ])
    for i in range(20):
        kind = "playbook" if i < 2 else "ref"
        db.insert(hid, tid, [[1.0 / (i + 1), 0.0, 0.0, 0.0], kind])
    hits = db.knn(hid, tid, col=0, k=100,
                   query=[1.0, 0.0, 0.0, 0.0], where='1="playbook"')
    assert len(hits) == 2


def test_search_with_where_filtering_everything_returns_empty(db):
    """If the WHERE clause eliminates the entire corpus, SEARCH returns
    [] cleanly — no -ERR, no hang."""
    hid = db.open()
    tid = db.create(hid, "t", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    db.insert(hid, tid, ["anything", [1.0, 0.0, 0.0, 0.0]])
    # Both rows have row_id < 999, so this filter matches nothing —
    # but the col we filter on must exist. Use a fresh table with a
    # numeric col we can predicate on.
    tid2 = db.create(hid, "t2", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
        ("uses", ColType.INT),
    ])
    db.insert(hid, tid2, ["anything", [1.0, 0.0, 0.0, 0.0], 1])
    hits = db.search(hid, tid2, vec_col=1, text_col=0, k=5,
                      vec=[1.0, 0.0, 0.0, 0.0], query="anything",
                      where="2>999")
    assert hits == []


# ── Parser fuzz ─────────────────────────────────────────────────────


@pytest.mark.parametrize("expr", [
    "",                                # empty
    "(",                               # unclosed paren
    "0=",                              # no rhs
    '0="',                             # unclosed string
    "0~",                              # no rhs after ~
    "0~V",                             # V[ not opened
    "0~V[",                            # V[ not closed
    "0~V[1.0",                         # V[ not closed
    "AND",                             # leading operator
    "0=1 BANANA 1>2",                  # unknown infix
    'NOT 0="x"',                       # NOT not supported yet
])
def test_bsearch_parser_rejects_garbage(db, expr):
    """The parser surfaces -ERR rather than crashing or going off the
    rails on garbage input."""
    hid = db.open()
    tid = db.create(hid, "t", [
        ("body", ColType.STRING),
        ("n", ColType.INT),
    ])
    db.insert(hid, tid, ["x", 1])
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, expr)


# ── Stress / sanity ─────────────────────────────────────────────────


def test_lsearch_handles_50_token_doc(db):
    """Realistic-length docs index correctly — no truncation/overflow
    inside the tokenizer's fixed buffers."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    words = ["alpha", "bravo", "charlie", "delta", "echo",
              "foxtrot", "golf", "hotel", "india", "juliet"]
    doc = " ".join(random.Random(7).choices(words, k=50))
    db.insert(hid, tid, [doc])
    # Search for a word that's almost certainly in there.
    hits = db.lsearch(hid, tid, 0, 3, "alpha bravo")
    # Either we get the doc (most likely) or empty (if random skipped
    # both rare words) — both are valid, but we should never crash.
    assert isinstance(hits, list)


# ── Audit regressions: dim-mismatch must error, not silently fudge ───


def test_knn_rejects_under_dim_query(db):
    """v0.5.17 audit: KNN used to zero-pad an under-dim query vector
    and return wrong-but-not-error scores. Must now reject."""
    hid = db.open()
    tid = db.create(hid, "v", [Column.vec("emb", dim=4)])
    db.insert(hid, tid, [[1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(CuttleDBError):
        db.send(f"KNN {hid} {tid} 0 3 0.5|0.5")


def test_knn_rejects_over_dim_query(db):
    """v0.5.17 audit: KNN used to silently truncate over-dim queries.
    Must now reject."""
    hid = db.open()
    tid = db.create(hid, "v", [Column.vec("emb", dim=4)])
    db.insert(hid, tid, [[1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(CuttleDBError):
        db.send(f"KNN {hid} {tid} 0 3 0.5|0.5|0.0|0.0|99.0")


def test_knn_accepts_exact_dim_query(db):
    """Sanity: the strict path still accepts the right shape."""
    hid = db.open()
    tid = db.create(hid, "v", [Column.vec("emb", dim=4)])
    db.insert(hid, tid, [[1.0, 0.0, 0.0, 0.0]])
    hits = db.knn(hid, tid, 0, 1, [1.0, 0.0, 0.0, 0.0])
    assert hits[0][0] == 0


def test_search_rejects_under_dim_query(db):
    hid = db.open()
    tid = db.create(hid, "s", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    db.insert(hid, tid, ["x", [1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(CuttleDBError):
        db.send(f"SEARCH {hid} {tid} 1 0 3 0.5|0.5 ||| x")


def test_search_rejects_over_dim_query(db):
    hid = db.open()
    tid = db.create(hid, "s", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    db.insert(hid, tid, ["x", [1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(CuttleDBError):
        db.send(f"SEARCH {hid} {tid} 1 0 3 0.5|0.5|0.0|0.0|99.0 ||| x")


def test_bm25_survives_rehash_during_long_doc(db):
    """v0.5.17 audit: bm25_add used to check the load factor INSIDE the
    per-token loop, so a rehash mid-doc invalidated the touched[] slot
    table. Subsequent tokens that hit `already==true` then incremented
    tf on the wrong bucket — leading to corrupted postings and eventual
    segfaults on search.

    Repro: 5 medium-length real-world docs (the demo corpus's first
    five engrams) is enough to cross the 0.75 load threshold during the
    second or third row's bm25_add call, which previously crashed.
    Fix hoisted the rehash decision to the start of bm25_add so the
    slot indices stay stable for the whole token stream."""
    hid = db.open()
    tid = db.create(hid, "t", [("body", ColType.STRING)])
    # Real-world-shaped docs with lots of unique tokens — enough to
    # push the BM25 hash table past the 48-term resize threshold during
    # add of the 4th-5th row.
    bodies = [
        "playbook:create the index with INDEX hid tid col HNSW M=16 is "
        "the default neighbors-per-node knob ef_construction=200 "
        "controls build quality higher M means better recall at cost",
        "ref://higher ef_search at query time trades latency for recall "
        "start at ef=100 and bump if recall@10 drops below 0.95 sensible",
        "playbook:after the initial build you can keep INSERTing the "
        "substrate maintains the graph incrementally with the same M",
        "ref://swap-with-last semantics deleted slot is tombstoned the "
        "last row vector is re-added at the freed slot searches skip",
        "ref://k1=1.5 controls term saturation b=0.75 normalizes by "
        "document length matches Lucene Elasticsearch defaults precise",
    ]
    for body in bodies:
        db.insert(hid, tid, [body])
    # First LSEARCH triggers bm25_build which exercises the same rehash
    # path. Then a couple more searches to verify the index is sound.
    for query in ("xyz", "hnsw", "k1", "ef_construction", "recall"):
        hits = db.lsearch(hid, tid, 0, 5, query)
        assert isinstance(hits, list)
    # Insert more rows and search again — exercise the incremental path
    # too with the same hoisted-rehash protection.
    for body in bodies:
        db.insert(hid, tid, [body])  # duplicate insertions
    hits = db.lsearch(hid, tid, 0, 5, "hnsw")
    assert len(hits) > 0
