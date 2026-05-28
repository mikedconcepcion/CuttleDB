"""Regression: SELGT must work on a column that follows a VEC column.

v0.6.0 had a SELGT result-emitter that branched only on STRING vs everything-
else, then read ``cc->fdata[r]`` for non-STRING columns. VEC columns have
no fdata (they use vdata), so the emitter touched NULL and the server
closed the connection mid-response. Symptom from a client: a fresh
connection RESET on the first SELGT call against any table whose schema
includes a VEC column.

Caught by the soak harness on its first real run (see
``cuttledb-v07/`` workspace bug report). Fixed in cuttledb.c by mirroring
GET's full per-column rendering (STRING / VEC / DATETIME / numeric)
inside the SELGT row emitter.

This test exercises the four shapes that triangulated the bug. Use a
server that's already running (matches the existing test_smoke.py
pattern).
"""
from __future__ import annotations

import os
import socket
import pytest

from cuttledb import CuttleDB, ColType


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


def test_selgt_int_only(db):
    """Baseline — SELGT on the only column. Always worked."""
    hid = db.open()
    tid = db.create(hid, "sgt_t1", [("x", ColType.INT)])
    for i in range(10):
        db.insert(hid, tid, [i])
    rows = db.select_gt(hid, tid, 0, 5)
    assert {int(r[0]) for r in rows} == {6, 7, 8, 9}


def test_selgt_int_after_string(db):
    """STRING + INT — also fine pre-fix."""
    hid = db.open()
    tid = db.create(hid, "sgt_t2", [("name", ColType.STRING),
                                     ("score", ColType.INT)])
    for i in range(10):
        db.insert(hid, tid, [f"n{i}", i])
    rows = db.select_gt(hid, tid, 1, 5)
    scores = sorted(int(r[1]) for r in rows)
    assert scores == [6, 7, 8, 9]


def test_selgt_int_after_vec(db):
    """REGRESSION — VEC col 0, INT col 1, SELGT col 1.

    Before the fix this RESET the connection on first call.
    """
    hid = db.open()
    tid = db.create(hid, "sgt_t3", [("embed", ColType.VEC, 8),
                                     ("score", ColType.INT)])
    for i in range(10):
        db.insert(hid, tid, [[float(j) for j in range(8)], i])
    rows = db.select_gt(hid, tid, 1, 5)
    # We get back two columns; col 0 is the pipe-separated vec, col 1 the int.
    scores = sorted(int(r[1]) for r in rows)
    assert scores == [6, 7, 8, 9]
    # And the VEC column round-trips as |-separated f32 strings.
    for r in rows:
        parts = r[0].split("|")
        assert len(parts) == 8
        assert [float(p) for p in parts] == [float(j) for j in range(8)]


def test_selgt_soak_table_shape(db):
    """REGRESSION — full soak schema: STRING + VEC + INT, SELGT col 2."""
    hid = db.open()
    tid = db.create(hid, "sgt_t4", [
        ("text",  ColType.STRING),
        ("embed", ColType.VEC, 8),
        ("score", ColType.INT),
    ])
    for i in range(10):
        db.insert(hid, tid, [f"r{i}", [float(j) for j in range(8)], i])
    rows = db.select_gt(hid, tid, 2, 5)
    assert sorted(int(r[2]) for r in rows) == [6, 7, 8, 9]
    # STRING col, VEC col, INT col all round-trip.
    for r in rows:
        assert r[0].startswith("r")
        assert r[1].count("|") == 7  # 8 dims → 7 separators


def test_get_strings_with_wire_specials(db):
    """REGRESSION — single-row GET also round-trips wire-special bytes.

    GET already used _split_wire_row (escape-aware), but no test pinned
    GET with ';' specifically. Since wire_str_encode now escapes ';' too,
    GET output for ;-containing strings changed shape; this asserts
    decoded values still match input.
    """
    hid = db.open()
    tid = db.create(hid, "get_specials", [("name", ColType.STRING)])
    specials = ["plain", "co,mma", "se;mi", "back\\slash",
                "cr\rcr", "lf\nlf", "all,;\\\r\n!"]
    for i, s in enumerate(specials):
        db.insert(hid, tid, [s])
        row = db.get(hid, tid, i)
        assert row == [s], (
            f"GET round-trip failed at i={i}: "
            f"stored={s!r} got={row[0]!r}"
        )


def test_selgt_strings_with_wire_specials(db):
    """REGRESSION — STRING columns round-trip cleanly through select_gt
    even when they contain wire-special bytes: ``,`` (column separator),
    ``;`` (row separator), ``\\`` (escape lead-in), CR, LF.

    Pre-fix the server emitted raw bytes (silent column-misalignment),
    THEN my interim fix made the server escape ``,`` ``\\`` CR LF but
    forgot ``;``. Final fix: server escapes all five; decoder unescapes
    all five. This test pins all five.
    """
    hid = db.open()
    tid = db.create(hid, "sgt_specials", [
        ("name",  ColType.STRING),
        ("score", ColType.INT),
    ])
    rows_in = [
        ["alice",        10],   # no specials — sanity check
        ["bob,jr",       20],   # comma
        ["car;l",        30],   # semicolon
        ["back\\slash",  40],   # backslash
        ["with\rcr",     50],   # carriage return
        ["with\nlf",     60],   # line feed
        ["all,;\\\r\n!", 70],   # all five at once
    ]
    for r in rows_in:
        db.insert(hid, tid, r)

    # select_gt fires on score > 5 → returns all seven rows.
    out = db.select_gt(hid, tid, 1, 5)
    assert len(out) == len(rows_in), f"row-count split: got {len(out)} want {len(rows_in)}"
    for r in out:
        assert len(r) == 2, f"column-count split: got {len(r)} want 2 — row={r!r}"

    # Decoded values match the originals (sort by score for deterministic compare).
    out_by_score = {int(r[1]): r[0] for r in out}
    for original_name, score in rows_in:
        assert out_by_score[score] == original_name, (
            f"value mismatch at score={score}: "
            f"got {out_by_score[score]!r}, want {original_name!r}"
        )
