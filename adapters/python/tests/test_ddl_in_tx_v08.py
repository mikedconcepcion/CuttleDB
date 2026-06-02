"""DDL inside transactions — v0.8.0.

CREATE / ALTER / INDEX are now transactional: they apply with read-your-writes
inside the tx, persist on COMMIT, and revert cleanly on ROLLBACK. These
exercise the generic CuttleDB transaction surface (the same API CuttleSearch
piggybacks on). Live-server tests; share the unauth'd fresh-data server on
port 7799 like the other v0.8.0 suites.
"""
from __future__ import annotations

import os
import socket

import pytest

from cuttledb import ColType, CuttleDB, CuttleDBError


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7799"))


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
    c = CuttleDB.connect(HOST, PORT)
    yield c
    c.close()


# ── CREATE in tx ────────────────────────────────────────────────────────

def test_create_in_tx_commit_persists(db):
    hid = db.open()
    db.begin()
    tid = db.create(hid, "ddltx_create_commit", [
        ("a", ColType.INT), ("b", ColType.INT),
    ])
    # read-your-writes: insert into the freshly-created table inside the tx
    db.insert(hid, tid, [7, 42])
    db.commit()
    # table and row survive the commit
    assert db.count(hid, tid) == 1
    assert db.sum(hid, tid, 1) == 42


def test_create_in_tx_rollback_removes_table(db):
    hid = db.open()
    db.begin()
    tid = db.create(hid, "ddltx_create_rollback", [("x", ColType.INT)])
    db.rollback()
    # the table id must no longer resolve — SUM errors on a missing table
    with pytest.raises(CuttleDBError):
        db.sum(hid, tid, 0)


# ── ALTER ADD in tx ──────────────────────────────────────────────────────

def test_alter_add_in_tx_commit_persists(db):
    hid = db.open()
    tid = db.create(hid, "ddltx_alter_commit", [
        ("n", ColType.STRING), ("v", ColType.INT),
    ])
    db.insert(hid, tid, ["alpha", 11])
    db.insert(hid, tid, ["beta", 22])
    db.begin()
    wcol = db.alter_add(hid, tid, "w", int(ColType.INT))
    # backfilled to 0; set row 0's new column inside the tx
    db.update_row(hid, tid, 0, wcol, 99)
    db.commit()
    # new column persists with the value
    assert db.sum(hid, tid, wcol) == 99
    row0 = db.get(hid, tid, 0)
    assert "99" in row0


def test_alter_add_in_tx_rollback_removes_column(db):
    hid = db.open()
    tid = db.create(hid, "ddltx_alter_rollback", [
        ("n", ColType.STRING), ("v", ColType.INT),
    ])
    db.insert(hid, tid, ["alpha", 11])
    db.begin()
    gcol = db.alter_add(hid, tid, "ghost", int(ColType.INT))
    # the ghost column is usable inside the tx (read-your-writes)
    assert db.sum(hid, tid, gcol) == 0
    db.rollback()
    # ghost column is gone — SUM on the now out-of-range column errors
    with pytest.raises(CuttleDBError):
        db.sum(hid, tid, gcol)
    # the pre-tx columns survive untouched
    assert db.sum(hid, tid, 1) == 11


# ── INDEX in tx ──────────────────────────────────────────────────────────

def test_index_in_tx_commit_persists(db):
    hid = db.open()
    tid = db.create(hid, "ddltx_index_commit", [("name", ColType.STRING)])
    db.insert(hid, tid, ["apple"])
    db.insert(hid, tid, ["banana"])
    db.insert(hid, tid, ["apple"])
    db.begin()
    db.index(hid, tid, 0)
    # FIND through the just-built index inside the tx
    assert sorted(db.find(hid, tid, 0, "apple")) == [0, 2]
    db.commit()
    assert sorted(db.find(hid, tid, 0, "apple")) == [0, 2]


def test_index_in_tx_rollback_reverts(db):
    hid = db.open()
    tid = db.create(hid, "ddltx_index_rollback", [("k", ColType.STRING)])
    db.insert(hid, tid, ["x"])
    db.insert(hid, tid, ["y"])
    db.begin()
    db.index(hid, tid, 0)
    db.rollback()
    # FIND still works via linear-scan fallback (no leaked/corrupt index)
    assert db.find(hid, tid, 0, "x") == [0]
    # re-INDEX outside the tx still succeeds
    db.index(hid, tid, 0)
    assert db.find(hid, tid, 0, "y") == [1]


def test_composite_index_in_tx_rollback_reverts(db):
    hid = db.open()
    tid = db.create(hid, "ddltx_cidx_rollback", [
        ("region", ColType.STRING), ("sku", ColType.INT),
    ])
    db.insert(hid, tid, ["west", 1])
    db.insert(hid, tid, ["east", 2])
    db.begin()
    db.index(hid, tid, 0, 1)  # composite over (region, sku)
    db.rollback()
    # composite lookup falls back to scan and is still correct after rollback
    assert db.findc(hid, tid, [0, 1], ["west", 1]) == [0]
    # rebuilding the composite index outside the tx still works
    db.index(hid, tid, 0, 1)
    assert db.findc(hid, tid, [0, 1], ["east", 2]) == [1]
