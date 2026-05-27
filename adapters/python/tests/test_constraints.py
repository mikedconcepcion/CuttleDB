"""Body-discipline constraint tests.

Server-side enforcement of:
- MAX <n>: cap on STR value length
- PREFIX p1|p2|...: STR value must start with one of the allowed prefixes

Both checks happen at INSERT (and INS_BATCH / op_insert). Constraints survive
SAVE/LOAD snapshots and WAL replay (because WAL replays the original CREATE
line through the new parser).

Requires a running server on 127.0.0.1:7780 (same as test_smoke.py).
"""
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


# ── MAX <n> ──────────────────────────────────────────────────────────────

def test_max_bytes_accepts_short_values(db):
    hid = db.open()
    tid = db.create(hid, "tight", [
        Column("body", int(ColType.STRING), max_bytes=32),
    ])
    rid = db.insert(hid, tid, ["hello"])
    assert rid == 0
    assert db.count(hid, tid) == 1


def test_max_bytes_rejects_long_values(db):
    hid = db.open()
    tid = db.create(hid, "tight", [
        Column("body", int(ColType.STRING), max_bytes=8),
    ])
    db.insert(hid, tid, ["short"])
    with pytest.raises(CuttleDBError) as exc:
        db.insert(hid, tid, ["this-string-exceeds-eight-bytes"])
    assert "MAX" in str(exc.value) or "exceeds" in str(exc.value)
    # The failed insert must NOT have committed.
    assert db.count(hid, tid) == 1


def test_max_bytes_boundary_exact(db):
    """A value of exactly max_bytes length is accepted; one byte more is not."""
    hid = db.open()
    tid = db.create(hid, "tight", [
        Column("body", int(ColType.STRING), max_bytes=5),
    ])
    db.insert(hid, tid, ["abcde"])     # exactly 5 — OK
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["abcdef"])  # 6 — rejected
    assert db.count(hid, tid) == 1


# ── PREFIX <p1>|<p2>|... ────────────────────────────────────────────────

def test_prefix_accepts_matching_value(db):
    hid = db.open()
    tid = db.create(hid, "engrams", [
        Column("body", int(ColType.STRING),
               prefixes=("kernel://", "ref://", "text:")),
    ])
    db.insert(hid, tid, ["kernel://cosine_topk"])
    db.insert(hid, tid, ["ref:///path/to/doc.md"])
    db.insert(hid, tid, ["text:Use datetime.fromisoformat"])
    assert db.count(hid, tid) == 3


def test_prefix_rejects_unmatched_value(db):
    hid = db.open()
    tid = db.create(hid, "engrams", [
        Column("body", int(ColType.STRING),
               prefixes=("kernel://", "ref://", "text:")),
    ])
    with pytest.raises(CuttleDBError) as exc:
        db.insert(hid, tid, ["bare-string-no-prefix"])
    assert "prefix" in str(exc.value).lower()
    assert db.count(hid, tid) == 0


def test_prefix_with_colon_in_value(db):
    """Prefix values that contain ':' (the wire-protocol modifier separator)
    must round-trip cleanly. Regression guard for the parser change."""
    hid = db.open()
    tid = db.create(hid, "engrams", [
        Column("body", int(ColType.STRING),
               prefixes=("text:",)),
    ])
    db.insert(hid, tid, ["text:hello world"])
    assert db.count(hid, tid) == 1


# ── Combined MAX + PREFIX ────────────────────────────────────────────────

def test_max_and_prefix_combined(db):
    hid = db.open()
    tid = db.create(hid, "skills", [
        Column("name", int(ColType.STRING), max_bytes=64),
        Column("body", int(ColType.STRING), max_bytes=2048,
               prefixes=("kernel://", "ref://", "text:")),
        Column("uses", int(ColType.INT)),
    ])
    db.insert(hid, tid, ["parse_iso_date", "text:Use datetime.fromisoformat()", 0])
    # Body OK but name too long → reject
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["x" * 65, "text:body", 0])
    # Name OK but body lacks prefix → reject
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["short_name", "no prefix here", 0])
    # Name OK but body too long → reject
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["short_name", "text:" + "x" * 2050, 0])
    assert db.count(hid, tid) == 1


# ── Backward compatibility ──────────────────────────────────────────────

def test_unconstrained_string_accepts_anything(db):
    """Columns without MAX or PREFIX must still accept any string value —
    no regression for legacy tables."""
    hid = db.open()
    tid = db.create(hid, "loose", [
        Column("name", int(ColType.STRING)),
    ])
    db.insert(hid, tid, ["short"])
    db.insert(hid, tid, ["x" * 10_000])
    db.insert(hid, tid, ["weird://stuff::with:::colons"])
    assert db.count(hid, tid) == 3


# ── ALTER ADD with constraints ──────────────────────────────────────────

def test_alter_add_max_enforced(db):
    hid = db.open()
    tid = db.create(hid, "evolving", [
        Column("name", int(ColType.STRING)),
    ])
    db.insert(hid, tid, ["original"])
    # Add a constrained body column; existing row backfills with empty.
    db.alter_add(hid, tid, "body", int(ColType.STRING), max_bytes=16)
    # New inserts go through the validator.
    db.insert(hid, tid, ["evo1", "short body"])
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["evo2", "way-too-long-body-exceeds-cap"])
    assert db.count(hid, tid) == 2


def test_alter_add_prefix_enforced(db):
    hid = db.open()
    tid = db.create(hid, "evolving", [
        Column("name", int(ColType.STRING)),
    ])
    db.alter_add(hid, tid, "body", int(ColType.STRING),
                 prefixes=["kernel://", "text:"])
    db.insert(hid, tid, ["n1", "kernel://m1"])
    with pytest.raises(CuttleDBError):
        db.insert(hid, tid, ["n2", "plain text"])
    assert db.count(hid, tid) == 1
