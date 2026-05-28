"""UPDR wire-verb tests.

UPDATE WHERE matches by predicate, which is ambiguous if multiple rows share
the same old value. UPDR addresses one row by its physical row_id — precise.
Each updated row emits the same change-feed event as UPDATE WHERE."""
from __future__ import annotations

import os
import socket
import pytest

from cuttledb import CuttleDB, CuttleDBError, Column, ColType


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


def _setup_counters(db):
    hid = db.open()
    tid = db.create(hid, "counters", [
        Column("name",  int(ColType.STRING)),
        Column("uses",  int(ColType.INT)),
    ])
    db.insert(hid, tid, ["a", 0])
    db.insert(hid, tid, ["b", 0])
    db.insert(hid, tid, ["c", 0])
    return hid, tid


def test_updr_bumps_one_row(db):
    hid, tid = _setup_counters(db)
    db.update_row(hid, tid, 1, 1, 5)   # row_id=1 (b), col=1 (uses), val=5
    # db.get() returns string values uniformly — no type coercion.
    assert db.get(hid, tid, 0)[1] == "0"
    assert db.get(hid, tid, 1)[1] == "5"
    assert db.get(hid, tid, 2)[1] == "0"


def test_updr_does_not_collide_with_shared_old_value(db):
    """The whole point of UPDR vs UPDATE WHERE: when two rows share the
    same old value, UPDR only touches the one we name. UPDATE WHERE EQ
    would touch both."""
    hid, tid = _setup_counters(db)
    # All three rows have uses=0.
    db.update_row(hid, tid, 1, 1, 7)
    # Only row 1 changed; rows 0 and 2 still at 0.
    assert db.get(hid, tid, 0)[1] == "0"
    assert db.get(hid, tid, 1)[1] == "7"
    assert db.get(hid, tid, 2)[1] == "0"


def test_updr_rejects_string_col(db):
    hid, tid = _setup_counters(db)
    with pytest.raises(CuttleDBError):
        db.update_row(hid, tid, 0, 0, 99)  # col 0 = name (STR)


def test_updr_rejects_out_of_range_row(db):
    hid, tid = _setup_counters(db)
    with pytest.raises(CuttleDBError):
        db.update_row(hid, tid, 999, 1, 1)


def test_updr_updates_cached_sum(db):
    """The numeric column's cached SUM must stay in sync with UPDR writes."""
    hid, tid = _setup_counters(db)
    db.insert(hid, tid, ["d", 10])
    db.insert(hid, tid, ["e", 20])
    assert db.sum(hid, tid, 1) == 30
    db.update_row(hid, tid, 3, 1, 100)  # row 3 (d): 10 → 100
    assert db.sum(hid, tid, 1) == 120
    db.update_row(hid, tid, 4, 1, 0)    # row 4 (e): 20 → 0
    assert db.sum(hid, tid, 1) == 100
