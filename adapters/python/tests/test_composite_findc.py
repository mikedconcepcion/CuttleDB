"""End-to-end test for composite secondary indexes (INDEX multi-col + FINDC).

Builds a composite index over two+ columns (numeric and string may both
participate), then verifies FINDC returns exactly the rows where every
``col == value``. Covers the indexed O(1) path, the unindexed linear-scan
fallback, incremental INSERT/DELETE maintenance, numeric canonicalization
(2018 vs 2018.0 collapse to one key), and snapshot v5 SAVE/LOAD round-trip.

Mirrors the fitment query shape that drives the CuttleSearch store:
(make, model, year) → product rows.
"""
import os
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


def _fitment_table(db):
    """A (year:int, make:str, model:str) table seeded with a few vehicles."""
    hid = db.open()
    tid = db.create(hid, "fitment", [
        ("year", ColType.INT),
        ("make", ColType.STRING),
        ("model", ColType.STRING),
    ])
    rows = [
        (2018, "HONDA", "CIVIC"),    # 0
        (2018, "HONDA", "CIVIC"),    # 1  (dup → two rows, same key)
        (2018, "HONDA", "ACCORD"),   # 2
        (2019, "HONDA", "CIVIC"),    # 3
        (2018, "TOYOTA", "COROLLA"), # 4
        (2020, "FORD", "F-150"),     # 5
    ]
    for r in rows:
        db.insert(hid, tid, list(r))
    return hid, tid


def test_findc_indexed_exact_match(db):
    """Composite index over (make, model, year) → FINDC returns the dup rows."""
    hid, tid = _fitment_table(db)
    n = db.index(hid, tid, 1, 2, 0)  # make, model, year
    assert n == 6

    rows = db.findc(hid, tid, [1, 2, 0], ["HONDA", "CIVIC", 2018])
    assert sorted(rows) == [0, 1]


def test_findc_single_match(db):
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2, 0)
    rows = db.findc(hid, tid, [1, 2, 0], ["TOYOTA", "COROLLA", 2018])
    assert rows == [4]


def test_findc_no_match_returns_empty(db):
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2, 0)
    rows = db.findc(hid, tid, [1, 2, 0], ["HONDA", "CIVIC", 1999])
    assert rows == []


def test_findc_linear_scan_without_index(db):
    """FINDC with no matching composite index falls back to an O(N) scan and
    still returns correct rows."""
    hid, tid = _fitment_table(db)
    # No index built — linear scan path.
    rows = db.findc(hid, tid, [1, 2, 0], ["HONDA", "CIVIC", 2018])
    assert sorted(rows) == [0, 1]


def test_findc_two_column_index(db):
    """A 2-column (make, model) composite collapses the year dimension."""
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2)  # make, model
    rows = db.findc(hid, tid, [1, 2], ["HONDA", "CIVIC"])
    assert sorted(rows) == [0, 1, 3]  # 2018x2 + 2019


def test_findc_numeric_canonicalization(db):
    """2018 and 2018.0 must map to the same composite key."""
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 0, 1)  # year, make
    a = db.findc(hid, tid, [0, 1], [2018, "HONDA"])
    b = db.findc(hid, tid, [0, 1], [2018.0, "HONDA"])
    assert sorted(a) == sorted(b) == [0, 1, 2]


def test_findc_incremental_insert(db):
    """INSERT after INDEX keeps the composite index live."""
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2, 0)
    new_id = db.insert(hid, tid, [2018, "HONDA", "CIVIC"])
    rows = db.findc(hid, tid, [1, 2, 0], ["HONDA", "CIVIC", 2018])
    assert new_id in rows
    assert sorted(rows) == [0, 1, new_id]


def test_findc_incremental_delete(db):
    """DELETE maintains the composite index (swap-with-last fixup)."""
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2, 0)
    # Delete row 0 (a HONDA CIVIC 2018). Row 5 (FORD) swaps into slot 0.
    assert db.delete(hid, tid, 0) is True

    civic = db.findc(hid, tid, [1, 2, 0], ["HONDA", "CIVIC", 2018])
    assert civic == [1]  # only the surviving dup remains

    # The swapped-in FORD must be findable at its new slot (0).
    ford = db.findc(hid, tid, [1, 2, 0], ["FORD", "F-150", 2020])
    assert ford == [0]


def test_findc_persists_through_save_load(db):
    """SAVE/LOAD round-trips the composite index defs (snapshot v5); FINDC
    works immediately on the loaded handle with no rebuild call."""
    hid, tid = _fitment_table(db)
    db.index(hid, tid, 1, 2, 0)

    snap = os.path.join(tempfile.gettempdir(), "cuttledb_cidx_snap.cuttledb")
    snap = snap.replace("\\", "/")
    db.save(hid, snap)

    new_hid = db.load(snap)
    assert new_hid >= 0
    rows = db.findc(new_hid, 0, [1, 2, 0], ["HONDA", "CIVIC", 2018])
    assert sorted(rows) == [0, 1]

    try:
        os.remove(snap)
    except OSError:
        pass


def test_findc_rejects_vec_column(db):
    """Composite index over a VEC column must fail (INDEX → -ERR)."""
    hid = db.open()
    tid = db.create(hid, "withvec", [
        ("v", ColType.VEC, 8),
        ("tag", ColType.STRING),
    ])
    db.insert(hid, tid, [[1.0] * 8, "x"])
    from cuttledb import CuttleDBError
    with pytest.raises(CuttleDBError):
        db.index(hid, tid, 0, 1)
