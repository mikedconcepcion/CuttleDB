"""UPDRS / UPDATES wire-verb tests (v0.8.0 string-column UPDATE).

The string siblings of UPDR / UPDATE:
- UPDRS sets one STRING cell by physical row_id.
- UPDATES sets a STRING column on every row matching a numeric predicate.

Both keep secondary string indexes, composite indexes and BM25 consistent,
escape commas/newlines on the wire, and participate in transactions.
Requires a running server on 127.0.0.1:7780 (same as test_smoke.py)."""
from __future__ import annotations

import os
import socket
import pytest

from cuttledb import CuttleDB, CuttleDBError, Column, ColType, Op


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


def _setup(db):
    hid = db.open()
    tid = db.create(hid, "people", [
        Column("name",  int(ColType.STRING)),
        Column("score", int(ColType.INT)),
    ])
    db.insert(hid, tid, ["alice", 10])
    db.insert(hid, tid, ["bob",   20])
    db.insert(hid, tid, ["carol", 30])
    return hid, tid


def test_updrs_sets_string_cell(db):
    hid, tid = _setup(db)
    assert db.update_row_str(hid, tid, 0, 0, "alice cooper") == 1
    assert db.get(hid, tid, 0)[0] == "alice cooper"
    assert db.get(hid, tid, 1)[0] == "bob"


def test_updrs_value_with_comma_roundtrips(db):
    hid, tid = _setup(db)
    db.update_row_str(hid, tid, 1, 0, "smith, john")
    assert db.get(hid, tid, 1)[0] == "smith, john"


def test_updrs_rejects_numeric_col(db):
    hid, tid = _setup(db)
    with pytest.raises(CuttleDBError):
        db.update_row_str(hid, tid, 0, 1, "nope")  # col 1 = score (INT)


def test_updates_predicate(db):
    hid, tid = _setup(db)
    n = db.update_where_str(hid, tid, 0, "HI", 1, Op.GT, 15)  # score>15
    assert n == 2
    assert db.get(hid, tid, 0)[0] == "alice"   # untouched
    assert db.get(hid, tid, 1)[0] == "HI"
    assert db.get(hid, tid, 2)[0] == "HI"


def test_updates_rejects_numeric_set_col(db):
    hid, tid = _setup(db)
    with pytest.raises(CuttleDBError):
        db.update_where_str(hid, tid, 1, "x", 1, Op.GT, 0)  # set_col 1 = INT


def test_index_consistency_after_updrs(db):
    hid, tid = _setup(db)
    db.update_where_str(hid, tid, 0, "HI", 1, Op.GT, 15)
    db.index(hid, tid, 0)
    assert sorted(db.find(hid, tid, 0, "HI")) == [1, 2]
    db.update_row_str(hid, tid, 1, 0, "changed")
    assert db.find(hid, tid, 0, "HI") == [2]
    assert db.find(hid, tid, 0, "changed") == [1]


def test_tx_rollback_restores_string(db):
    hid, tid = _setup(db)
    db.index(hid, tid, 0)
    db.begin()
    db.update_row_str(hid, tid, 2, 0, "temp")
    assert db.get(hid, tid, 2)[0] == "temp"
    db.rollback()
    assert db.get(hid, tid, 2)[0] == "carol"
    assert db.find(hid, tid, 0, "carol") == [2]


def test_tx_commit_persists(db):
    hid, tid = _setup(db)
    db.index(hid, tid, 0)
    db.begin()
    db.update_where_str(hid, tid, 0, "committed", 1, Op.GT, 25)  # score>25 -> carol
    db.commit()
    assert db.get(hid, tid, 2)[0] == "committed"
    assert db.find(hid, tid, 0, "committed") == [2]
