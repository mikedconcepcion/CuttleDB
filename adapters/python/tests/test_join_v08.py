"""JOIN v0.8.0 — outer joins (left/right/full), non-equi predicates
(GT/LT), and hash-join scale (equi-join past the old 100M cartesian cap).

Generic CuttleDB JOIN surface (the same API CuttleSearch piggybacks on).
Live-server tests; share the unauth'd fresh-data server on port 7799.
"""
from __future__ import annotations

import os
import socket

import pytest

from cuttledb import ColType, CuttleDB, Op


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
def emp_dept():
    """employees.dept → departments.id. dept 3 has no department row;
    department id 4 ("ops") has no employee — so outer joins have
    unmatched rows on both sides."""
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    emp = db.create(hid, "employees", [
        ("name", ColType.STRING),
        ("dept", ColType.INT),
    ])
    db.insert_batch(hid, emp, [
        ["alice", 1], ["bob", 2], ["carol", 1], ["dave", 3],
    ])
    dep = db.create(hid, "departments", [
        ("id",    ColType.INT),
        ("label", ColType.STRING),
    ])
    db.insert_batch(hid, dep, [
        [1, "eng"], [2, "sales"], [4, "ops"],
    ])
    yield db, hid, emp, dep
    db.close()


def test_inner_equi_backcompat(emp_dept):
    db, hid, emp, dep = emp_dept
    assert db.join(hid, emp, 1, hid, dep, 0) == [(0, 0), (1, 1), (2, 0)]


def test_left_outer_keeps_unmatched_left(emp_dept):
    db, hid, emp, dep = emp_dept
    # dave (row 3, dept 3) has no department → pairs with -1.
    assert db.join(hid, emp, 1, hid, dep, 0, how="left") == [
        (0, 0), (1, 1), (2, 0), (3, -1),
    ]


def test_right_outer_keeps_unmatched_right(emp_dept):
    db, hid, emp, dep = emp_dept
    # department row 2 (id 4, "ops") has no employee → (-1, 2).
    assert db.join(hid, emp, 1, hid, dep, 0, how="right") == [
        (0, 0), (1, 1), (2, 0), (-1, 2),
    ]


def test_full_outer_keeps_both(emp_dept):
    db, hid, emp, dep = emp_dept
    assert db.join(hid, emp, 1, hid, dep, 0, how="full") == [
        (0, 0), (1, 1), (2, 0), (3, -1), (-1, 2),
    ]


def test_non_equi_gt(emp_dept):
    db, hid, emp, dep = emp_dept
    # employees.dept > departments.id
    assert db.join(hid, emp, 1, hid, dep, 0, op=Op.GT) == [
        (1, 0), (3, 0), (3, 1),
    ]


def test_non_equi_lt(emp_dept):
    db, hid, emp, dep = emp_dept
    # employees.dept < departments.id
    assert db.join(hid, emp, 1, hid, dep, 0, op=Op.LT) == [
        (0, 1), (0, 2), (1, 2), (2, 1), (2, 2), (3, 2),
    ]


def test_string_equi_join(emp_dept):
    db, hid, _, _ = emp_dept
    a = db.create(hid, "a_tbl", [("k", ColType.STRING)])
    b = db.create(hid, "b_tbl", [("k", ColType.STRING)])
    db.insert_batch(hid, a, [["x"], ["y"], ["z"]])
    db.insert_batch(hid, b, [["y"], ["z"], ["w"]])
    assert db.join(hid, a, 0, hid, b, 0) == [(1, 0), (2, 1)]


def test_hash_join_beats_old_cartesian_cap(emp_dept):
    """20K x 20K = 400M pairs would exceed the old 100M nested-loop cap;
    the equi-join hash path handles it without a cap."""
    db, hid, _, _ = emp_dept
    n = 20000
    lt = db.create(hid, "scale_l", [("k", ColType.INT)])
    rt = db.create(hid, "scale_r", [("k", ColType.INT)])
    db.insert_batch(hid, lt, [[i] for i in range(n)])
    db.insert_batch(hid, rt, [[i] for i in range(n)])
    res = db.join(hid, lt, 0, hid, rt, 0)
    assert len(res) == n
    assert res[0] == (0, 0)
    assert res[-1] == (n - 1, n - 1)


def test_unknown_how_raises(emp_dept):
    db, hid, emp, dep = emp_dept
    with pytest.raises(ValueError):
        db.join(hid, emp, 1, hid, dep, 0, how="cross")
