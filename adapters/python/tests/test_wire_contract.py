"""Wire-format escape contract — pin every escape character end-to-end.

The wire format defines a small set of bytes the server must escape on
output and the adapter must escape on input. Pre-2026-05-28 those rules
were duplicated across three implementations (server `wire_str_encode`,
Python `_encode_value` + `_split_wire_row`, JS `encodeValue` +
`splitWireRow`) with no contract test. Three bugs in that batch:

- JS `encodeValue` never escaped outbound (pre-existing latent —
  shipped from v0.6.0 like that, no test ever inserted a
  comma-containing string from JS so nobody noticed).
- Python `_parse_rowlist` split rows naively (would have produced
  silent column misalignment on any STRING containing a comma).
- JS `parseRowlist` / `get` split rows naively — same shape.

This file is the durable fix. It defines ONE canonical character list
that the server must round-trip cleanly through both INSERT→GET and
INSERT→SELECT_GT for STRING columns. The JS suite mirrors it
verbatim. If we add a new escape character to the wire format in the
future, we add it here and it must pass on every adapter, or someone
shipped a half-fix.

The current escape contract (server `wire_str_encode`):
    backslash    \\  -> emit  \\\\
    comma        ,   -> emit  \\,
    semicolon    ;   -> emit  \\;
    carriage     \\r -> emit  \\r  (literal backslash + r)
    line feed    \\n -> emit  \\n  (literal backslash + n)
"""
from __future__ import annotations

import os
import socket
import pytest

from cuttledb import CuttleDB, ColType, Op


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


# ── Canonical escape contract ────────────────────────────────────────
# Add a new row here when the wire format grows a new escape. Every
# adapter test (Python + JS) tests against this same list. Failing one
# adapter while passing another = encode/decode drift.
WIRE_CONTRACT_STRINGS = [
    ("plain-ascii",          "alice"),
    ("contains-comma",       "bob,jr"),
    ("contains-semicolon",   "car;l"),
    ("contains-backslash",   "back\\slash"),
    ("contains-cr",          "with\rcr"),
    ("contains-lf",          "with\nlf"),
    ("contains-all-five",    "all,;\\\r\n!"),
    ("starts-with-special",  ",starts-comma"),
    ("ends-with-special",    "ends-comma,"),
    ("only-specials",        ",;\\,;\\"),
    ("repeated-escape",      "\\\\\\\\"),
    ("escape-then-char",     "\\;\\,\\\\!"),
    ("crlf-pair",            "line1\r\nline2"),
    ("empty-string",         ""),
]


@pytest.fixture(scope="module")
def db():
    with CuttleDB.connect(HOST, PORT) as d:
        yield d


@pytest.fixture(scope="module")
def t(db):
    """One STRING + INT table; each test uses unique scores so we can
    fetch rows back by score predicate without cross-test interference."""
    hid = db.open()
    tid = db.create(hid, "wire_contract", [
        ("payload", ColType.STRING),
        ("idx",     ColType.INT),
    ])
    return hid, tid


@pytest.mark.parametrize("label,val", WIRE_CONTRACT_STRINGS,
                         ids=[c[0] for c in WIRE_CONTRACT_STRINGS])
def test_get_round_trip(db, t, label, val):
    """INSERT a wire-special string, GET it back, assert equal."""
    hid, tid = t
    rid = db.insert(hid, tid, [val, hash(label) & 0xffff])
    got = db.get(hid, tid, rid)
    assert got[0] == val, (
        f"GET round-trip failed for {label!r}:\n"
        f"  stored: {val!r}\n"
        f"  got:    {got[0]!r}"
    )


def test_selgt_round_trip_all_at_once(db):
    """INSERT every contract string, SELECT_GT them all back, assert
    each round-trips by matching on a unique idx column.

    Single test (not parametrized) so we exercise the rowlist
    encoder/decoder on a real multi-row response — exactly the shape
    that broke pre-fix.
    """
    hid = db.open()
    tid = db.create(hid, "wire_contract_bulk", [
        ("payload", ColType.STRING),
        ("idx",     ColType.INT),
    ])
    expected = {}  # idx -> payload
    for i, (_, val) in enumerate(WIRE_CONTRACT_STRINGS):
        idx = 1000 + i
        db.insert(hid, tid, [val, idx])
        expected[idx] = val

    rows = db.select_gt(hid, tid, 1, 999)  # all idx > 999
    assert len(rows) == len(expected), (
        f"row count mismatch: got {len(rows)} want {len(expected)}\n"
        f"received: {rows}"
    )
    for r in rows:
        assert len(r) == 2, f"column count split: row={r!r}"
        idx, payload = int(r[1]), r[0]
        assert payload == expected[idx], (
            f"select_gt round-trip failed for idx={idx}:\n"
            f"  stored: {expected[idx]!r}\n"
            f"  got:    {payload!r}"
        )
