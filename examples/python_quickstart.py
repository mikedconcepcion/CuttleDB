#!/usr/bin/env python3
"""CuttleDB Python quickstart — assumes server running on 127.0.0.1:7780.

Start the server:
    cuttledb-server --port 7780

Run this:
    python examples/python_quickstart.py
"""
from cuttledb import CuttleDB, ColType

with CuttleDB.connect("127.0.0.1", 7780) as db:
    hid = db.open()
    tid = db.create(hid, "txn", [
        ("customer", ColType.STRING),
        ("type",     ColType.STRING),
        ("amount",   ColType.INT),
    ])

    db.insert_batch(hid, tid, [
        ["alice", "purchase", 100],
        ["bob",   "purchase", 250],
        ["alice", "refund",   -50],
    ])

    print(f"rows:         {db.count(hid, tid)}")
    print(f"sum amount:   {db.sum(hid, tid, 2)}")
    print(f"min amount:   {db.min(hid, tid, 2)}")
    print(f"max amount:   {db.max(hid, tid, 2)}")
    print(f"rows > 100:   {db.select_gt(hid, tid, 2, 100)}")
