"""JOIN — 2-way inner equi-join (Sub-stage B #3)."""
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
def orders_customers():
    """Classic orders + customers schema. orders.customer_id → customers.id."""
    db = CuttleDB.connect(HOST, PORT)
    hid = db.open()
    cust_tid = db.create(hid, "customers", [
        ("id",    ColType.INT),
        ("name",  ColType.STRING),
        ("city",  ColType.STRING),
    ])
    db.insert_batch(hid, cust_tid, [
        [1, "alice", "NYC"],
        [2, "bob",   "SF"],
        [3, "carol", "LA"],
    ])
    order_tid = db.create(hid, "orders", [
        ("order_id",     ColType.INT),
        ("customer_id",  ColType.INT),
        ("amount",       ColType.INT),
    ])
    db.insert_batch(hid, order_tid, [
        [101, 1, 50],
        [102, 1, 75],
        [103, 2, 30],
        [104, 3, 100],
        [105, 4, 25],  # customer_id=4 doesn't exist → no match
    ])
    yield db, hid, cust_tid, order_tid
    db.close()


def test_inner_join_returns_matching_pairs(orders_customers):
    db, hid, cust_tid, order_tid = orders_customers
    # Join customers.id (col 0) to orders.customer_id (col 1).
    pairs = db.join(hid, cust_tid, 0, hid, order_tid, 1)
    # Expected: customer 1 → orders 101 & 102 (rows 0 & 1)
    #           customer 2 → order 103 (row 2)
    #           customer 3 → order 104 (row 3)
    #           customer 4 doesn't exist → no match for order 105 (row 4)
    # customer row indices: alice=0, bob=1, carol=2
    # order row indices:    101=0, 102=1, 103=2, 104=3, 105=4
    expected = {(0, 0), (0, 1), (1, 2), (2, 3)}
    assert set(pairs) == expected


def test_join_on_string_keys(orders_customers):
    """STRING ↔ STRING equi-join."""
    db, hid, cust_tid, order_tid = orders_customers
    # Make a tiny lookup table that maps city name to a region.
    region_tid = db.create(hid, "regions", [
        ("city",   ColType.STRING),
        ("region", ColType.STRING),
    ])
    db.insert_batch(hid, region_tid, [
        ["NYC", "east"],
        ["SF",  "west"],
        ["LA",  "west"],
    ])
    pairs = db.join(hid, cust_tid, 2, hid, region_tid, 0)
    # Each customer matches their city's region row.
    # cust rows: alice/NYC=0, bob/SF=1, carol/LA=2
    # region rows: NYC=0, SF=1, LA=2
    expected = {(0, 0), (1, 1), (2, 2)}
    assert set(pairs) == expected


def test_join_no_matches_returns_empty(orders_customers):
    """When no rows match, result is empty list."""
    db, hid, cust_tid, order_tid = orders_customers
    # Empty fresh table.
    empty_tid = db.create(hid, "empty", [
        ("id", ColType.INT),
    ])
    pairs = db.join(hid, cust_tid, 0, hid, empty_tid, 0)
    assert pairs == []


def test_join_type_mismatch_rejected(orders_customers):
    """STRING ↔ INT mismatch returns -ERR."""
    db, hid, cust_tid, order_tid = orders_customers
    with pytest.raises(CuttleDBError) as exc:
        db.join(hid, cust_tid, 1, hid, order_tid, 0)  # name (STR) vs order_id (INT)
    assert "mismatch" in str(exc.value).lower()


def test_join_vec_rejected(orders_customers):
    """VEC columns can't be joined."""
    db, hid, cust_tid, order_tid = orders_customers
    vec_tid = db.create(hid, "vecs", [("v", ColType.VEC, 4)])
    with pytest.raises(CuttleDBError) as exc:
        db.join(hid, cust_tid, 0, hid, vec_tid, 0)
    assert "vec" in str(exc.value).lower()


def test_join_numeric_compat_int_and_datetime(orders_customers):
    """DATETIME ↔ INT equi-join works (both stored as f64)."""
    db, hid, cust_tid, order_tid = orders_customers
    # Build a tiny events table with DATETIME column.
    events_tid = db.create(hid, "events", [
        ("ts",      ColType.DATETIME),
        ("kind",    ColType.STRING),
    ])
    # DATETIME stored as int64 epoch ms. We use specific values that
    # match orders.amount values for the test.
    db.insert_batch(hid, events_tid, [
        [50, "low"],     # matches order amount=50 (row 0 = order 101)
        [100, "high"],   # matches order amount=100 (row 3 = order 104)
        [999, "no-match"],
    ])
    # Join orders.amount (col 2) to events.ts (col 0). Both numeric.
    pairs = db.join(hid, order_tid, 2, hid, events_tid, 0)
    # order rows: 101=0 (amount 50), 102=1 (75), 103=2 (30),
    #             104=3 (100), 105=4 (25)
    # event rows: 50=0, 100=1, 999=2
    # Expected matches:
    #   order row 0 (50) ↔ event row 0 (50)
    #   order row 3 (100) ↔ event row 1 (100)
    expected = {(0, 0), (3, 1)}
    assert set(pairs) == expected
