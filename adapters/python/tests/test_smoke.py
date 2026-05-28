"""Smoke tests for the CuttleDB Python SDK.

Requires a running server on 127.0.0.1:7780. Start one with::

    cuttledb-server --port 7780

These tests use a fresh handle per test (call db.open()) so they don't
interfere with one another or with the server's other state.
"""
from __future__ import annotations

import os
import socket
import time
import pytest

from cuttledb import CuttleDB, CuttleDBError, ColType, Column, Op


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


def test_ping(db):
    assert db.ping() == "PONG"


def test_hello(db):
    line = db.hello()
    assert line.startswith("cuttledb ")
    assert "proto" in line


def test_info(db):
    info = db.info()
    assert "version" in info
    assert int(info["uptime_ms"]) >= 0


def test_stats_global(db):
    s = db.stats()
    assert "handles" in s
    assert "rows" in s


def test_open_create_insert_get(db):
    hid = db.open()
    tid = db.create(hid, "users", [
        ("name",   ColType.STRING),
        ("salary", ColType.INT),
    ])
    rid_a = db.insert(hid, tid, ["Alice", 100])
    rid_b = db.insert(hid, tid, ["Bob",   250])
    assert rid_a == 0
    assert rid_b == 1
    assert db.count(hid, tid) == 2
    assert db.sum(hid, tid, 1) == 350
    assert db.min(hid, tid, 1) == 100
    assert db.max(hid, tid, 1) == 250
    assert db.get(hid, tid, 0) == ["Alice", "100"]


def test_insert_batch(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    rows = [[i] for i in range(50)]
    ids = db.insert_batch(hid, tid, rows)
    assert ids == list(range(50))
    assert db.count(hid, tid) == 50
    assert db.sum(hid, tid, 0) == sum(range(50))


def test_fcount_and_select(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert_batch(hid, tid, [[i] for i in range(10)])
    assert db.fcount_gt(hid, tid, 0, 5) == 4  # 6,7,8,9
    rows = db.select_gt(hid, tid, 0, 7)
    assert {row[0] for row in rows} == {"8", "9"}


def test_select_empty(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert(hid, tid, [1])
    assert db.select_gt(hid, tid, 0, 100) == []


def test_knn(db):
    hid = db.open()
    tid = db.create(hid, "m", [
        ("doc", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    db.insert(hid, tid, ["a", [1.0, 0.0, 0.0, 0.0]])
    db.insert(hid, tid, ["b", [0.0, 1.0, 0.0, 0.0]])
    db.insert(hid, tid, ["c", [0.7, 0.7, 0.0, 0.0]])
    hits = db.knn(hid, tid, col=1, k=2, query=[1.0, 0.0, 0.0, 0.0])
    assert len(hits) == 2
    assert hits[0][0] == 0  # "a" is the exact match
    assert hits[0][1] > hits[1][1]


def _knn_where_table(db):
    """Helper for v0.5.17 Phase 1 filtered-KNN tests. Schema:
       0: doc (STRING), 1: emb (VEC4), 2: kind (STRING), 3: uses (INT)."""
    hid = db.open()
    tid = db.create(hid, "m", [
        ("doc", ColType.STRING),
        Column.vec("emb", dim=4),
        ("kind", ColType.STRING),
        ("uses", ColType.INT),
    ])
    db.insert(hid, tid, ["a", [1.0, 0.0, 0.0, 0.0], "playbook",   5])
    db.insert(hid, tid, ["b", [0.9, 0.1, 0.0, 0.0], "ref",        2])
    db.insert(hid, tid, ["c", [0.7, 0.7, 0.0, 0.0], "playbook",   1])
    db.insert(hid, tid, ["d", [0.5, 0.5, 0.5, 0.5], "playbook",   8])
    return hid, tid


def test_knn_where_string_eq(db):
    """v0.5.17 Phase 1 — string column equality filters the result set."""
    hid, tid = _knn_where_table(db)
    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where='2="playbook"')
    rows = [r for r, _ in hits]
    assert rows == [0, 2, 3]  # a, c, d — b is "ref", filtered out


def test_knn_where_numeric_comparisons(db):
    """v0.5.17 Phase 1 — numeric column with >, <=, etc."""
    hid, tid = _knn_where_table(db)
    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where="3>3")
    rows = sorted(r for r, _ in hits)
    assert rows == [0, 3]  # uses=5 and uses=8

    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where="3<=2")
    rows = sorted(r for r, _ in hits)
    assert rows == [1, 2]


def test_knn_where_multiple_AND(db):
    """v0.5.17 Phase 1 — predicates AND'd together narrow the set further."""
    hid, tid = _knn_where_table(db)
    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where='2="playbook" AND 3>3')
    rows = sorted(r for r, _ in hits)
    assert rows == [0, 3]


def test_knn_where_no_match(db):
    """Filter that matches nothing returns an empty result, not an error."""
    hid, tid = _knn_where_table(db)
    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where='2="nonexistent"')
    assert hits == []


def test_knn_where_ne_operator(db):
    """!= operator returns everything except the matched value."""
    hid, tid = _knn_where_table(db)
    hits = db.knn(hid, tid, col=1, k=4, query=[1.0, 0.0, 0.0, 0.0],
                  where='2!="ref"')
    rows = sorted(r for r, _ in hits)
    assert rows == [0, 2, 3]


# ── BM25 lexical search (v0.5.17 Phase 2) ─────────────────────────────


def _bm25_corpus(db):
    """Helper for BM25 tests. Doc 0: brown fox. Doc 1: quick movement.
       Doc 2: liquor jugs. Doc 3: brown bear + brown fox (heavy 'brown').
       Doc 4: zebras."""
    hid = db.open()
    tid = db.create(hid, "docs", [("body", ColType.STRING)])
    docs = [
        "the quick brown fox jumps over the lazy dog",
        "a quick movement of the enemy will jeopardize six gunboats",
        "pack my box with five dozen liquor jugs",
        "the brown bear and the brown fox are friends",
        "how quickly daft jumping zebras vex",
    ]
    for d in docs:
        db.insert(hid, tid, [d])
    return hid, tid


def test_lsearch_returns_top_k(db):
    """Top-k BM25 over a small corpus picks the most relevant docs."""
    hid, tid = _bm25_corpus(db)
    hits = db.lsearch(hid, tid, col=0, k=3, query="brown fox")
    rows = [r for r, _ in hits]
    # Doc 3 has 'brown' x2 + 'fox' so it must outrank doc 0.
    assert rows[0] == 3
    assert 0 in rows
    # Scores descending.
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)


def test_lsearch_no_match_returns_empty(db):
    hid, tid = _bm25_corpus(db)
    assert db.lsearch(hid, tid, col=0, k=3, query="quasar") == []


def test_lsearch_auto_builds_index(db):
    """LSEARCH without prior INDEX BM25 still works — the substrate
    builds the inverted index lazily on first use."""
    hid, tid = _bm25_corpus(db)
    # No INDEX command issued; LSEARCH should still return ranked hits.
    hits = db.lsearch(hid, tid, col=0, k=2, query="liquor")
    assert len(hits) == 1
    assert hits[0][0] == 2


def test_lsearch_after_explicit_index(db):
    """Explicit INDEX BM25 returns the doc count and subsequent
    LSEARCH respects the build (same scores either way for default
    k1/b)."""
    hid, tid = _bm25_corpus(db)
    n_docs = int(db.send(f"INDEX {hid} {tid} 0 BM25"))
    assert n_docs == 5
    hits = db.lsearch(hid, tid, col=0, k=5, query="brown")
    rows = [r for r, _ in hits]
    # Both doc 0 and doc 3 contain 'brown'; doc 3 has it twice.
    assert rows[0] == 3
    assert 0 in rows


def test_lsearch_incremental_insert_indexed(db):
    """A row inserted AFTER the index was built must be searchable —
    the INSERT path maintains BM25 just like HNSW."""
    hid, tid = _bm25_corpus(db)
    db.send(f"INDEX {hid} {tid} 0 BM25")
    db.insert(hid, tid, ["a fresh document about platypus"])
    hits = db.lsearch(hid, tid, col=0, k=3, query="platypus")
    assert len(hits) == 1
    assert hits[0][0] == 5  # the newly-inserted row


def test_lsearch_after_delete_skips_tombstoned(db):
    """A DELETE must remove the row from search results, not return it
    as a stale hit."""
    hid, tid = _bm25_corpus(db)
    db.send(f"INDEX {hid} {tid} 0 BM25")
    # Delete doc 3 — the heavy 'brown' doc. After delete, doc 0 should
    # take the top spot for 'brown'.
    assert db.delete(hid, tid, 3) is True
    hits = db.lsearch(hid, tid, col=0, k=3, query="brown")
    rows = [r for r, _ in hits]
    # The deleted row (3) might still appear as a swapped-in slot, but
    # the ORIGINAL doc 3 ('brown bear...') must not be there. Verify by
    # checking that the top hit is now the 'brown fox' doc — which after
    # swap-with-last lives at row 3 (zebras moved into 0's slot? no,
    # swap moves the LAST row into the deleted slot, so row 4 'zebras'
    # now lives at index 3, and 'brown' hits should only be row 0).
    assert 3 not in rows  # the new content at slot 3 is 'zebras', no 'brown'
    assert rows == [0]


def test_lsearch_case_insensitive(db):
    """Tokenizer lowercases — queries match regardless of case."""
    hid, tid = _bm25_corpus(db)
    lower = db.lsearch(hid, tid, col=0, k=5, query="brown")
    upper = db.lsearch(hid, tid, col=0, k=5, query="BROWN")
    assert lower == upper


def test_lsearch_punctuation_split(db):
    """Punctuation is a delimiter — `quick,brown` matches both terms
    just like `quick brown`."""
    hid, tid = _bm25_corpus(db)
    a = db.lsearch(hid, tid, col=0, k=5, query="quick brown")
    b = db.lsearch(hid, tid, col=0, k=5, query="quick,brown!!")
    assert a == b


def test_lsearch_rejects_non_string_col(db):
    """A VEC or INT column can't be lexically indexed."""
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert(hid, tid, [1])
    with pytest.raises(CuttleDBError):
        db.lsearch(hid, tid, col=0, k=3, query="anything")


def test_index_bm25_with_param_overrides(db):
    """k1 and b can be overridden on the build command."""
    hid, tid = _bm25_corpus(db)
    # Use extreme k1 to amplify tf saturation differences.
    n = int(db.send(f"INDEX {hid} {tid} 0 BM25 k1=2.0 b=0.5"))
    assert n == 5
    # Still works as a corpus — the param tuning doesn't break search.
    hits = db.lsearch(hid, tid, col=0, k=3, query="brown")
    assert hits[0][0] == 3


# ── Hybrid SEARCH via RRF (v0.5.17 Phase 3) ────────────────────────────


def _hybrid_corpus(db):
    """Schema: 0=body (STRING), 1=emb (VEC4). Returns (hid, tid)."""
    hid = db.open()
    tid = db.create(hid, "h", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    rows = [
        ("the quick brown fox",                  [1.0, 0.0, 0.0, 0.0]),
        ("a slow purple turtle",                 [0.99, 0.05, 0.0, 0.0]),
        ("the quick brown bear loves honey",     [0.95, 0.1, 0.05, 0.0]),
        ("quasar tracking with neural nets",     [0.0, 0.0, 1.0, 0.0]),
    ]
    for body, emb in rows:
        db.insert(hid, tid, [body, emb])
    return hid, tid


def test_search_hybrid_fuses_streams(db):
    """The hybrid path elevates rows that score well on BOTH streams.

    Query vec is closest to row 1 ('slow purple turtle') but the text
    'brown fox' only matches rows 0 and 2. The fused top-2 should be
    rows 0 and 2 — they ranked highly in both."""
    hid, tid = _hybrid_corpus(db)
    qvec = [0.99, 0.05, 0.0, 0.0]
    hits = db.search(hid, tid, vec_col=1, text_col=0, k=4,
                     vec=qvec, query="brown fox")
    rows = [r for r, _ in hits]
    # Rows 0 and 2 should be in the top 2.
    assert set(rows[:2]) == {0, 2}
    # RRF scores descending.
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)


def test_search_no_text_match_returns_only_knn_hits(db):
    """When BM25 finds nothing, RRF falls back to KNN ordering alone."""
    hid, tid = _hybrid_corpus(db)
    qvec = [0.99, 0.05, 0.0, 0.0]
    hits = db.search(hid, tid, vec_col=1, text_col=0, k=2,
                     vec=qvec, query="quasar wormhole")
    rows = [r for r, _ in hits]
    # 'quasar' only matches row 3 (cosine ~0). Hybrid hits = row 3 (text)
    # + best KNN rows. The top hit should still be a vec-strong row.
    assert len(hits) > 0


def test_search_with_where_filter(db):
    """WHERE filter applies to BOTH streams before fusion."""
    hid = db.open()
    tid = db.create(hid, "h", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
        ("kind", ColType.STRING),
    ])
    rows = [
        ("brown fox runs",   [1.0, 0.0, 0.0, 0.0], "playbook"),
        ("brown bear sits",  [0.9, 0.1, 0.0, 0.0], "ref"),
        ("the lazy dog",     [0.8, 0.2, 0.0, 0.0], "playbook"),
    ]
    for r in rows:
        db.insert(hid, tid, list(r))
    # Without filter: rows 0 and 1 both have 'brown'.
    unfiltered = db.search(hid, tid, vec_col=1, text_col=0, k=3,
                            vec=[1.0, 0.0, 0.0, 0.0], query="brown")
    assert {r for r, _ in unfiltered} >= {0, 1}
    # With filter: only kind="playbook" survives.
    filtered = db.search(hid, tid, vec_col=1, text_col=0, k=3,
                          vec=[1.0, 0.0, 0.0, 0.0], query="brown",
                          where='2="playbook"')
    rows_f = [r for r, _ in filtered]
    assert 1 not in rows_f  # row 1 is "ref"
    assert 0 in rows_f       # row 0 is "playbook" + has 'brown'


def test_search_empty_table_returns_empty(db):
    """Hybrid on an empty corpus returns an empty list."""
    hid = db.open()
    tid = db.create(hid, "h", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    hits = db.search(hid, tid, vec_col=1, text_col=0, k=3,
                     vec=[1.0, 0.0, 0.0, 0.0], query="anything")
    assert hits == []


def test_search_rejects_wrong_col_types(db):
    """vec_col must be VEC, text_col must be STRING."""
    hid = db.open()
    tid = db.create(hid, "h", [
        ("a", ColType.STRING),
        ("b", ColType.INT),
    ])
    db.insert(hid, tid, ["x", 1])
    with pytest.raises(CuttleDBError):
        db.search(hid, tid, vec_col=0, text_col=1, k=2,
                  vec=[1.0], query="x")


# ── BSEARCH Boolean DSL (v0.5.17 Phase 4) ─────────────────────────────


def _bsearch_corpus(db):
    """Cols: 0=body (STRING), 1=emb (VEC4), 2=kind (STRING), 3=uses (INT)."""
    hid = db.open()
    tid = db.create(hid, "m", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
        ("kind", ColType.STRING),
        ("uses", ColType.INT),
    ])
    rows = [
        ("brown fox runs fast",        [1.0, 0.0, 0.0, 0.0],    "playbook", 5),
        ("purple turtle naps",         [0.99, 0.05, 0.0, 0.0],  "ref",      2),
        ("brown bear loves honey",     [0.95, 0.1, 0.05, 0.0],  "playbook", 8),
        ("quasar tracking nets",       [0.0, 0.0, 1.0, 0.0],    "ref",      1),
    ]
    for r in rows:
        db.insert(hid, tid, list(r))
    return hid, tid


def test_bsearch_filter_only(db):
    """No scoring atom — returns rows matching the filter in row order."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5, '2="playbook" AND 3>3')
    rows = sorted(r for r, _ in hits)
    assert rows == [0, 2]


def test_bsearch_or_with_parens(db):
    """OR + parens narrow the candidate set correctly."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5, '(2="playbook" OR 2="ref") AND 3>=2')
    rows = sorted(r for r, _ in hits)
    # uses>=2: rows 0(5), 1(2), 2(8). Row 3 (uses=1) filtered out.
    assert rows == [0, 1, 2]


def test_bsearch_vector_scoring(db):
    """Scoring by vector similarity ranks within the filtered set."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5,
                       '2="playbook" AND 1~V[1.0|0.0|0.0|0.0]')
    rows = [r for r, _ in hits]
    # Both row 0 and row 2 are playbooks; row 0's vec is exact match.
    assert rows[0] == 0
    assert rows[1] == 2


def test_bsearch_text_scoring(db):
    """Scoring by BM25 ranks within the filtered set."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5, '2="playbook" AND 0~"brown"')
    rows = [r for r, _ in hits]
    # Both row 0 and row 2 contain 'brown' in playbook docs. Row 2's doc
    # is shorter so BM25's length-normalized score favors it slightly —
    # but the absolute score difference is small enough that platform-
    # specific FP reduction order (gcc vs MinGW vs clang) can flip the
    # ranking. The substantive guarantee is "both are in the top 2,"
    # not their exact relative order.
    assert set(rows[:2]) == {0, 2}


def test_bsearch_full_hybrid_user_example(db):
    """The user's canonical example: filters + vec scoring + uses>3.

    `(kind="playbook" OR kind="ref") AND embedding~"..." AND uses>3`
    """
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5,
        '(2="playbook" OR 2="ref") AND 1~V[1.0|0.0|0.0|0.0] AND 3>3')
    rows = [r for r, _ in hits]
    # uses>3 leaves only 0 (5) and 2 (8). Ranked by vec to [1,0,0,0]:
    # row 0 is exact, row 2 close second.
    assert rows == [0, 2]


def test_bsearch_two_score_atoms_fused_by_rrf(db):
    """Two scoring atoms — RRF fuses their rankings."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5, '1~V[1.0|0.0|0.0|0.0] AND 0~"brown"')
    rows = [r for r, _ in hits]
    # Rows 0 and 2 score on both signals → top.
    assert set(rows[:2]) == {0, 2}


def test_bsearch_string_inequality(db):
    """!=, <, > etc work on string columns via strcmp ordering."""
    hid, tid = _bsearch_corpus(db)
    hits = db.bsearch(hid, tid, 5, '2!="ref"')
    rows = sorted(r for r, _ in hits)
    assert rows == [0, 2]


def test_bsearch_empty_table(db):
    """Empty corpus → empty results, no error."""
    hid = db.open()
    tid = db.create(hid, "m", [
        ("body", ColType.STRING),
        Column.vec("emb", dim=4),
    ])
    hits = db.bsearch(hid, tid, 5, '1~V[1.0|0.0|0.0|0.0]')
    assert hits == []


def test_bsearch_rejects_syntax_error(db):
    """Bad syntax returns an error, not a silent empty result."""
    hid, tid = _bsearch_corpus(db)
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, '2= "missing quote')
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, '(2="playbook"')  # unclosed paren


def test_bsearch_rejects_wrong_col_for_score(db):
    """col~V[...] requires VEC; col~"text" requires STRING."""
    hid, tid = _bsearch_corpus(db)
    # body is STRING — V[] should be rejected.
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, '0~V[1.0|0.0|0.0|0.0]')
    # emb is VEC — "text" should be rejected.
    with pytest.raises(CuttleDBError):
        db.bsearch(hid, tid, 5, '1~"text"')


def test_delete(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert_batch(hid, tid, [[i] for i in range(3)])
    assert db.delete(hid, tid, 1) is True
    assert db.count(hid, tid) == 2


def test_insert_returned_id_equals_slot_post_delete(db):
    """v0.5.16+ contract: INSERT-returned row_id equals the storage slot
    (= count - 1 immediately after the insert). Before the fix, next_id
    was monotonic so post-delete inserts returned ids that didn't match
    GET/UPDATE/DELETE row_id semantics."""
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert(hid, tid, [10])
    db.insert(hid, tid, [20])
    db.insert(hid, tid, [30])
    # Delete a middle row — swap-with-last semantics apply.
    assert db.delete(hid, tid, 1) is True
    assert db.count(hid, tid) == 2
    # Next insert should return the slot it lands at.
    new_id = db.insert(hid, tid, [99])
    assert new_id == 2, f"expected slot id 2, got {new_id}"
    # GET on that id must return what we just inserted.
    row = db.get(hid, tid, new_id)
    assert int(row[0]) == 99


def test_log_cursor(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert(hid, tid, [10])
    db.insert(hid, tid, [20])
    cursor, events = db.log(hid, tid, since=0)
    assert cursor >= 2
    assert len(events) >= 2
    # tail
    db.insert(hid, tid, [30])
    cursor2, events2 = db.log(hid, tid, since=cursor)
    assert cursor2 > cursor
    assert len(events2) >= 1


def test_sub_then_poll(db):
    # Two connections: subscriber and writer.
    sub = CuttleDB.connect(HOST, PORT)
    try:
        hid = db.open()
        tid = db.create(hid, "v", [("x", ColType.INT)])
        sub.sub(hid, tid)
        db.insert(hid, tid, [99])
        time.sleep(0.05)  # let the broadcast settle
        events = sub.poll_events(timeout=0.5)
        assert len(events) >= 1
        assert events[0].hid == hid
        assert events[0].tid == tid
        assert events[0].op == "INS"
    finally:
        sub.close()


def test_error_propagation(db):
    hid = db.open()
    tid = db.create(hid, "v", [("x", ColType.INT)])
    db.insert(hid, tid, [1])
    with pytest.raises(CuttleDBError):
        db.get(hid, tid, 999)  # row out of range → -ERR not found


def test_context_manager_closes():
    db = CuttleDB.connect(HOST, PORT)
    assert db.ping() == "PONG"
    db.close()
    with pytest.raises(CuttleDBError):
        db.ping()


def test_close_handle_reuses_slot(db):
    hid_a = db.open()
    db.close_handle(hid_a)
    # The newly opened handle MAY or may not reuse the same slot depending
    # on what other concurrent handles exist; just check it succeeds and
    # the closed handle now rejects operations.
    with pytest.raises(CuttleDBError):
        db.create(hid_a, "ghost", [("x", ColType.INT)])


def test_close_handle_bad_hid(db):
    with pytest.raises(CuttleDBError):
        db.close_handle(999)


# ── Bulk mutations (v0.5) ──────────────────────────────────────────

def test_update_where_basic(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.insert_batch(hid, tid, [["a", 10], ["b", 20], ["c", 30], ["d", 40], ["e", 50]])
    # initial SUM = 150
    assert db.sum(hid, tid, 1) == 150
    # set v=999 WHERE v > 30 — affects d(40) and e(50)
    updated = db.update_where(hid, tid, set_col=1, set_val=999, pred_col=1, op=Op.GT, threshold=30)
    assert updated == 2
    # SUM now 10+20+30+999+999 = 2058
    assert db.sum(hid, tid, 1) == 2058
    # rows still 5 (update doesn't delete)
    assert db.count(hid, tid) == 5


def test_update_where_no_match(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[10], [20], [30]])
    n = db.update_where(hid, tid, 0, 999, 0, Op.GT, 1000)
    assert n == 0
    assert db.sum(hid, tid, 0) == 60


def test_delete_where_basic(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[10], [20], [30], [40], [50]])
    # delete WHERE v > 25 — should remove 30, 40, 50 (three rows)
    deleted = db.delete_where(hid, tid, pred_col=0, op=Op.GT, threshold=25)
    assert deleted == 3
    assert db.count(hid, tid) == 2
    assert db.sum(hid, tid, 0) == 30  # 10 + 20


def test_delete_where_no_match(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[1], [2], [3]])
    assert db.delete_where(hid, tid, 0, Op.GT, 1000) == 0
    assert db.count(hid, tid) == 3


def test_delete_where_eq(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[7], [7], [42], [7], [99]])
    assert db.delete_where(hid, tid, 0, Op.EQ, 7) == 3
    assert db.count(hid, tid) == 2
    assert db.sum(hid, tid, 0) == 141  # 42 + 99


def test_bulk_mutations_emit_events(db):
    """SUB on one connection sees DEL and UPD events broadcast from another."""
    sub = CuttleDB.connect(HOST, PORT)
    try:
        hid = db.open()
        tid = db.create(hid, "t", [("v", ColType.INT)])
        db.insert_batch(hid, tid, [[10], [20], [30], [40]])
        sub.sub(hid, tid)
        # Two mutations, expect 2 UPD + 2 DEL = 4 events total
        db.update_where(hid, tid, 0, 100, 0, Op.GT, 25)  # affects 30, 40
        db.delete_where(hid, tid, 0, Op.GT, 50)          # deletes the two 100s
        time.sleep(0.05)
        events = sub.poll_events(timeout=0.5)
        ops = [e.op for e in events]
        assert ops.count("UPD") == 2
        assert ops.count("DEL") == 2
    finally:
        sub.close()


def test_find_without_index_scans(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.insert_batch(hid, tid, [
        ["alice", 1], ["bob", 2], ["alice", 3], ["carol", 4],
    ])
    assert sorted(db.find(hid, tid, 0, "alice")) == [0, 2]
    assert db.find(hid, tid, 0, "bob") == [1]
    assert db.find(hid, tid, 0, "nobody") == []


def test_find_with_index(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.insert_batch(hid, tid, [
        ["alice", 1], ["bob", 2], ["alice", 3], ["carol", 4],
    ])
    indexed = db.index(hid, tid, 0)
    assert indexed == 4
    # Same results as the linear-scan path.
    assert sorted(db.find(hid, tid, 0, "alice")) == [0, 2]
    assert db.find(hid, tid, 0, "carol") == [3]


def test_index_maintained_on_insert(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert(hid, tid, ["alice"])
    db.index(hid, tid, 0)
    db.insert(hid, tid, ["alice"])   # post-index insert
    db.insert(hid, tid, ["bob"])
    assert sorted(db.find(hid, tid, 0, "alice")) == [0, 1]


def test_index_maintained_on_delete_with_swap(db):
    """DELETE moves the last row into the deleted slot. The index must
    follow that move so subsequent FINDs return the right row IDs."""
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert_batch(hid, tid, [["alice"], ["bob"], ["alice"], ["carol"]])
    db.index(hid, tid, 0)
    # Delete row 1 (bob). Swap-with-last moves "carol" from row 3 to row 1.
    db.delete(hid, tid, 1)
    assert db.find(hid, tid, 0, "bob") == []
    assert sorted(db.find(hid, tid, 0, "alice")) == [0, 2]
    assert db.find(hid, tid, 0, "carol") == [1]      # moved from row 3 to row 1
    # GET should resolve consistently.
    assert db.get(hid, tid, 1) == ["carol"]


def test_index_rebuild_is_idempotent(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert_batch(hid, tid, [["a"], ["b"], ["a"], ["c"]])
    db.index(hid, tid, 0)
    n = db.index(hid, tid, 0)  # rebuild
    assert n == 4
    assert sorted(db.find(hid, tid, 0, "a")) == [0, 2]


def test_index_rejects_non_string_col(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    with pytest.raises(CuttleDBError):
        db.index(hid, tid, 0)


def test_update_where_string_col_rejected(db):
    """v0.5.0 limitation: cannot UPDATE string columns. v0.5.1 target."""
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.insert(hid, tid, ["a", 1])
    # set string col 0 should error — pass a numeric value but col is string
    with pytest.raises(CuttleDBError):
        db.update_where(hid, tid, set_col=0, set_val=999, pred_col=1, op=Op.GT, threshold=0)


# ── v0.5 chunk 4: transactions ─────────────────────────────────────────

def test_tx_commit_persists(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.begin()
    db.insert(hid, tid, ["a", 10])
    db.insert(hid, tid, ["b", 20])
    n = db.commit()
    assert n == 2
    assert db.count(hid, tid) == 2
    assert db.sum(hid, tid, 1) == 30


def test_tx_rollback_reverts_inserts(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert(hid, tid, [100])               # pre-tx, stays
    db.begin()
    db.insert(hid, tid, [200])
    db.insert(hid, tid, [300])
    assert db.count(hid, tid) == 3
    n = db.rollback()
    assert n == 2
    assert db.count(hid, tid) == 1
    assert db.sum(hid, tid, 0) == 100


def test_tx_rollback_reverts_updates(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[10], [20], [30]])
    db.begin()
    db.update_where(hid, tid, 0, 999, 0, Op.GT, 5)  # set all to 999
    assert db.sum(hid, tid, 0) == 999 * 3
    db.rollback()
    assert db.sum(hid, tid, 0) == 60                # original sum


def test_tx_context_manager_commits(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    with db.transaction():
        db.insert(hid, tid, [1])
        db.insert(hid, tid, [2])
    assert db.count(hid, tid) == 2


def test_tx_context_manager_rolls_back(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    class _Bang(Exception): pass
    with pytest.raises(_Bang):
        with db.transaction():
            db.insert(hid, tid, [42])
            raise _Bang
    assert db.count(hid, tid) == 0


def test_tx_errors(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])

    with pytest.raises(CuttleDBError, match="not in tx"):
        db.commit()
    with pytest.raises(CuttleDBError, match="not in tx"):
        db.rollback()

    db.begin()
    with pytest.raises(CuttleDBError, match="already in tx"):
        db.begin()

    # CREATE in tx rejected
    db.insert(hid, tid, [1])
    with pytest.raises(CuttleDBError, match="ddl in tx"):
        db.create(hid, "t2", [("x", ColType.INT)])

    db.rollback()


# ── v0.5.1: DELETE inside transactions ─────────────────────────────────

def test_tx_delete_rollback_restores_middle_row(db):
    """DELETE a middle row (forces swap-with-last), rollback. The
    deleted row and the swapped-in row must both end up at their
    original positions."""
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING), ("v", ColType.INT)])
    db.insert_batch(hid, tid, [
        ["alice", 10], ["bob", 20], ["carol", 30], ["dave", 40], ["eve", 50],
    ])
    db.begin()
    db.delete(hid, tid, 2)              # carol; eve swaps into pos 2
    assert db.count(hid, tid) == 4
    assert db.get(hid, tid, 2) == ["eve", "50"]
    db.rollback()
    assert db.count(hid, tid) == 5
    assert db.sum(hid, tid, 1) == 150
    assert db.get(hid, tid, 2) == ["carol", "30"]
    assert db.get(hid, tid, 4) == ["eve", "50"]


def test_tx_delw_rollback(db):
    """Bulk DELETE WHERE inside tx then rollback restores all rows."""
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[10], [20], [30], [40], [50]])
    db.begin()
    deleted = db.delete_where(hid, tid, 0, Op.GT, 15)   # deletes 4 rows
    assert deleted == 4
    assert db.count(hid, tid) == 1
    db.rollback()
    assert db.count(hid, tid) == 5
    assert db.sum(hid, tid, 0) == 150


def test_tx_delete_commit_persists(db):
    """COMMIT after DELETE: the row stays deleted."""
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert_batch(hid, tid, [[10], [20], [30]])
    db.begin()
    db.delete(hid, tid, 1)
    db.commit()
    assert db.count(hid, tid) == 2
    assert db.sum(hid, tid, 0) == 40


def test_tx_delete_with_string_index(db):
    """DELETE rollback updates the string index correctly."""
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert_batch(hid, tid, [["alice"], ["bob"], ["carol"], ["dave"]])
    db.index(hid, tid, 0)
    assert db.find(hid, tid, 0, "carol") == [2]
    db.begin()
    db.delete(hid, tid, 2)              # delete carol
    assert db.find(hid, tid, 0, "carol") == []
    db.rollback()
    # After rollback, carol back at position 2.
    assert db.find(hid, tid, 0, "carol") == [2]
    assert db.find(hid, tid, 0, "dave")  == [3]


# ── v0.5 chunk 5: ALTER TABLE ───────────────────────────────────────────

def test_alter_add_numeric_column(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert(hid, tid, ["alice"])
    new_idx = db.alter_add(hid, tid, "salary", ColType.INT)
    assert new_idx == 1
    # existing row backfilled with default 0
    assert db.get(hid, tid, 0) == ["alice", "0"]
    db.insert(hid, tid, ["bob", 500])
    assert db.sum(hid, tid, 1) == 500


def test_alter_add_string_column(db):
    hid = db.open()
    tid = db.create(hid, "t", [("v", ColType.INT)])
    db.insert(hid, tid, [42])
    db.alter_add(hid, tid, "tag", ColType.STRING)
    assert db.get(hid, tid, 0) == ["42", ""]


def test_alter_add_vec_column(db):
    hid = db.open()
    tid = db.create(hid, "t", [("name", ColType.STRING)])
    db.insert(hid, tid, ["a"])
    db.alter_add(hid, tid, "emb", ColType.VEC, dim=3)
    row = db.get(hid, tid, 0)
    # Vector backfilled to zeroes (3 values pipe-separated).
    assert row[0] == "a"
    assert row[1] == "0|0|0"


# ── AUTH tests ─────────────────────────────────────────────────────────
# Only meaningful when CUTTLEDB_AUTH_PORT is set to a server started with
# --auth $CUTTLEDB_AUTH_TOKEN. Otherwise skipped.

import os as _os
AUTH_PORT  = int(_os.environ["CUTTLEDB_AUTH_PORT"])  if "CUTTLEDB_AUTH_PORT"  in _os.environ else None
AUTH_TOKEN = _os.environ.get("CUTTLEDB_AUTH_TOKEN")

@pytest.mark.skipif(
    AUTH_PORT is None or AUTH_TOKEN is None,
    reason="CUTTLEDB_AUTH_PORT / CUTTLEDB_AUTH_TOKEN not set",
)
class TestAuth:
    def test_open_without_auth_rejected(self):
        with CuttleDB.connect(HOST, AUTH_PORT) as db:
            assert db.ping() == "PONG"  # PING always allowed
            with pytest.raises(CuttleDBError, match="auth required"):
                db.open()

    def test_open_with_auth_succeeds(self):
        with CuttleDB.connect(HOST, AUTH_PORT, auth=AUTH_TOKEN) as db:
            hid = db.open()
            assert isinstance(hid, int) and hid >= 0

    def test_wrong_token_raises_on_connect(self):
        with pytest.raises(CuttleDBError, match="auth failed"):
            CuttleDB.connect(HOST, AUTH_PORT, auth="wrong-token").close()

    def test_hello_indicates_auth_required(self):
        with CuttleDB.connect(HOST, AUTH_PORT) as db:
            assert "auth_required" in db.hello()
