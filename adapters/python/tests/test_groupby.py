"""GROUP BY — aggregate by grouping column (Sub-stage B #2).

Live-server tests. Uses port 7799 (the unauth'd fresh-data server
the other Sub-stage B tests share).
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
def sales_table():
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    tid = db.create(hid, "sales", [
        ("region",   ColType.STRING),
        ("product",  ColType.STRING),
        ("amount",   ColType.INT),
        ("qty",      ColType.INT),
    ])
    # Realistic small dataset — three regions, two products.
    rows = [
        ["west",  "latte",   500, 5],
        ["west",  "latte",   400, 4],
        ["west",  "mocha",   600, 3],
        ["east",  "latte",   300, 3],
        ["east",  "mocha",   200, 2],
        ["east",  "mocha",   800, 4],
        ["north", "latte",   100, 1],
    ]
    db.insert_batch(hid, tid, rows)
    yield db, hid, tid
    db.close()


def test_group_by_count_returns_per_group_counts(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=0, agg="count"))
    # west=3 rows, east=3 rows, north=1 row
    assert groups == {"west": 3.0, "east": 3.0, "north": 1.0}


def test_group_by_sum_amount_per_region(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=0,
                                agg="sum", agg_col=2))
    # west: 500+400+600=1500, east: 300+200+800=1300, north: 100
    assert groups["west"]  == 1500.0
    assert groups["east"]  == 1300.0
    assert groups["north"] == 100.0


def test_group_by_max_amount_per_region(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=0,
                                agg="max", agg_col=2))
    assert groups == {"west": 600.0, "east": 800.0, "north": 100.0}


def test_group_by_min_amount_per_region(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=0,
                                agg="min", agg_col=2))
    assert groups == {"west": 400.0, "east": 200.0, "north": 100.0}


def test_group_by_avg_amount_per_region(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=0,
                                agg="avg", agg_col=2))
    # west avg = 1500/3 = 500
    # east avg = 1300/3 ≈ 433.333
    # north avg = 100
    assert groups["west"]  == pytest.approx(500.0)
    assert groups["east"]  == pytest.approx(1300.0 / 3, rel=1e-4)
    assert groups["north"] == 100.0


def test_group_by_product_string_key(sales_table):
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=1, agg="count"))
    # 4 latte rows, 3 mocha rows
    assert groups == {"latte": 4.0, "mocha": 3.0}


def test_group_by_numeric_key(sales_table):
    """Group by a numeric column (the qty col)."""
    db, hid, tid = sales_table
    groups = dict(db.group_by(hid, tid, group_col=3, agg="count"))
    # qty values: 5,4,3,3,2,4,1 → unique: {1, 2, 3, 4, 5}
    assert groups == {1: 1.0, 2: 1.0, 3: 2.0, 4: 2.0, 5: 1.0}


def test_group_by_rejects_vec_column(sales_table):
    """VEC columns can't be group keys."""
    db, hid, tid = sales_table
    # No VEC col in this table, but we can verify the error path with
    # a synthetic table that does have one.
    hid2 = db.open()
    tid2 = db.create(hid2, "embeddings", [
        ("label",  ColType.STRING),
        ("vec",    ColType.VEC, 4),
    ])
    with pytest.raises(CuttleDBError) as exc:
        db.group_by(hid2, tid2, group_col=1, agg="count")
    assert "VEC" in str(exc.value).upper()


def test_group_by_requires_numeric_agg_col(sales_table):
    """SUM/MIN/MAX/AVG over a STRING col is rejected."""
    db, hid, tid = sales_table
    with pytest.raises(CuttleDBError) as exc:
        db.group_by(hid, tid, group_col=0, agg="sum", agg_col=1)  # product is STRING
    assert "numeric" in str(exc.value).lower()


def test_group_by_sum_requires_agg_col_python_side():
    """Python SDK rejects missing agg_col for non-count aggregates."""
    db = CuttleDB.connect(HOST, PORT)
    try:
        with pytest.raises(ValueError, match="requires agg_col"):
            db.group_by(0, 0, group_col=0, agg="sum")
    finally:
        db.close()
