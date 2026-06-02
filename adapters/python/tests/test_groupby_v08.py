"""GROUP BY v0.8.0 clauses — multi-column BY, HAVING, ORDER, LIMIT, and
the removal of the 256-group cap.

These exercise the generic CuttleDB GROUPBY surface (the same API
CuttleSearch piggybacks on). Live-server tests; share the unauth'd
fresh-data server on port 7799 like the other Sub-stage B suites.
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
def sales_table():
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    tid = db.create(hid, "sales_v08", [
        ("region",  ColType.STRING),
        ("product", ColType.STRING),
        ("amount",  ColType.INT),
    ])
    rows = [
        ["west",  "latte", 500],
        ["west",  "latte", 400],
        ["west",  "mocha", 600],
        ["east",  "latte", 300],
        ["east",  "mocha", 200],
        ["east",  "mocha", 800],
        ["north", "latte", 100],
    ]
    db.insert_batch(hid, tid, rows)
    yield db, hid, tid
    db.close()


def test_backcompat_single_col_count(sales_table):
    db, hid, tid = sales_table
    assert dict(db.group_by(hid, tid, 0)) == {
        "west": 3.0, "east": 3.0, "north": 1.0,
    }


def test_multi_col_by_returns_tuple_keys(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, 0, agg="count", by=[1]))
    # (region, product) composite groups.
    assert groups[("west", "latte")] == 2.0
    assert groups[("west", "mocha")] == 1.0
    assert groups[("east", "latte")] == 1.0
    assert groups[("east", "mocha")] == 2.0
    assert groups[("north", "latte")] == 1.0
    assert all(isinstance(k, tuple) and len(k) == 2 for k, _ in
               db.group_by(hid, tid, 0, by=[1]))


def test_multi_col_by_sum(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, 0, agg="sum", agg_col=2, by=[1]))
    assert groups[("west", "latte")] == 900.0   # 500 + 400
    assert groups[("east", "mocha")] == 1000.0  # 200 + 800


def test_having_filters_aggregate(sales_table):
    db, hid, tid = sales_table
    # Keep only regions whose row-count > 1 (drops north=1).
    groups = dict(db.group_by(hid, tid, 0, agg="count", having=(Op.GT, 1)))
    assert groups == {"west": 3.0, "east": 3.0}


def test_having_eq(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, 0, agg="count", having=(Op.EQ, 1)))
    assert groups == {"north": 1.0}


def test_order_value_desc(sales_table):
    db, hid, tid = sales_table
    res = db.group_by(hid, tid, 0, agg="sum", agg_col=2,
                      order=("value", "desc"))
    values = [v for _, v in res]
    assert values == sorted(values, reverse=True)
    assert res[0][0] == "west"  # 1500 is the largest


def test_order_value_asc(sales_table):
    db, hid, tid = sales_table
    res = db.group_by(hid, tid, 0, agg="sum", agg_col=2,
                      order=("value", "asc"))
    values = [v for _, v in res]
    assert values == sorted(values)
    assert res[0][0] == "north"  # 100 is the smallest


def test_order_key_asc(sales_table):
    db, hid, tid = sales_table
    res = db.group_by(hid, tid, 0, agg="count", order=("key", "asc"))
    keys = [k for k, _ in res]
    assert keys == sorted(keys)


def test_limit_after_order(sales_table):
    db, hid, tid = sales_table
    res = db.group_by(hid, tid, 0, agg="sum", agg_col=2,
                      order=("value", "desc"), limit=2)
    assert len(res) == 2
    assert [k for k, _ in res] == ["west", "east"]


def test_more_than_256_groups(sales_table):
    """v1 capped at 256 distinct groups; v0.8.0 removes the cap."""
    db, hid, tid = sales_table
    big = db.create(hid, "big_groups", [
        ("k", ColType.STRING),
        ("v", ColType.INT),
    ])
    rows = []
    for i in range(500):
        rows.append([f"g{i}", i])
        rows.append([f"g{i}", i])
    db.insert_batch(hid, big, rows)
    res = db.group_by(hid, big, 0, agg="count")
    assert len(res) == 500
    assert all(v == 2.0 for _, v in res)
