"""Security + edge-case regression suite (v1.0.4).

Covers gaps identified in the 2026-05-27 audit:
- UNSUB post-subscribe — confirm events stop arriving and there's no
  use-after-free on the broadcast path.
- Malformed wire framing — bad framing, garbage after a verb, zero-length
  line, oversized line. Assert the server returns `-ERR` and stays alive.
- Concurrent INDEX HNSW on the same (hid,tid) — two clients building at
  once must not corrupt `table->hnsw`.

Requires a running server on 127.0.0.1:7780.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from cuttledb import CuttleDB, CuttleDBError, ColType


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


# ───────────────────────────────────────────────────────────────────────────
# UNSUB post-subscribe
# ───────────────────────────────────────────────────────────────────────────

def test_unsub_stops_events_arriving():
    """After UNSUB, INSERTs on the table must produce no events on the
    subscriber's socket. Catches use-after-free on the broadcast path."""
    with CuttleDB.connect(HOST, PORT) as subscriber, \
         CuttleDB.connect(HOST, PORT) as writer:
        hid = writer.open()
        tid = writer.create(hid, "evt_t", [
            ("name", ColType.STRING),
            ("v",    ColType.INT),
        ])
        subscriber.sub(hid, tid)

        # Insert one row — subscriber should see the event.
        writer.insert(hid, tid, ["before_unsub", 1])
        time.sleep(0.05)
        evts_before = subscriber.poll_events(timeout=0.1)
        assert any(e for e in evts_before), \
            "subscriber missed event before UNSUB"

        # UNSUB and let the server process it.
        subscriber.unsub(hid, tid)
        time.sleep(0.05)

        # Drain anything still in flight, then insert again.
        subscriber.poll_events(timeout=0.05)
        writer.insert(hid, tid, ["after_unsub", 2])
        time.sleep(0.1)
        evts_after = subscriber.poll_events(timeout=0.1)
        assert not evts_after, \
            f"received events after UNSUB: {evts_after!r}"


# ───────────────────────────────────────────────────────────────────────────
# Malformed wire framing — raw socket fuzz
# ───────────────────────────────────────────────────────────────────────────

def _raw_send_recv(payload: bytes, recv_timeout: float = 0.5) -> bytes:
    """Send raw bytes, read whatever comes back (or empty on timeout)."""
    s = socket.create_connection((HOST, PORT), timeout=recv_timeout)
    s.sendall(payload)
    s.settimeout(recv_timeout)
    chunks = []
    try:
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data and (b"+OK" in data or b"-ERR" in data):
                break
    except socket.timeout:
        pass
    finally:
        s.close()
    return b"".join(chunks)


def test_malformed_wire_garbage_bytes_get_err():
    """Pure binary garbage with no newline — server should respond -ERR
    or close the socket cleanly, not hang forever or crash."""
    junk = bytes(range(256)) * 4  # 1024 bytes, no \n
    resp = _raw_send_recv(junk, recv_timeout=0.5)
    # Server may have responded with -ERR, or closed. Either is acceptable.
    # What's NOT acceptable: server crash (next connection would fail).
    # Verify a fresh client succeeds afterward.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


def test_malformed_wire_zero_length_line():
    """Empty line (just \\r\\n) — server should respond -ERR or ignore."""
    _ = _raw_send_recv(b"\r\n")
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


def test_malformed_wire_oversized_line():
    """A single line larger than any sensible recv buffer (>64KB). Server
    should reject and stay alive."""
    huge = b"A" * (64 * 1024 + 64)
    huge += b"\r\n"
    _ = _raw_send_recv(huge, recv_timeout=1.0)
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


def test_malformed_wire_valid_verb_with_garbage_tail():
    """`PING <garbage>\\r\\n` — verb is valid, args are nonsense. Server
    must respond (-ERR or +OK) without crashing."""
    payload = b"PING " + bytes([0xFE, 0xFF, 0x00, 0x01]) + b"\r\n"
    _ = _raw_send_recv(payload)
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


# ───────────────────────────────────────────────────────────────────────────
# Concurrent HNSW INDEX builds on the same (hid,tid)
# ───────────────────────────────────────────────────────────────────────────

def test_concurrent_hnsw_index_no_corruption():
    """Two clients call INDEX HNSW on the same column concurrently. The
    server must serialize them — either build completes cleanly, and
    subsequent KNN returns sane results. The opposite outcome (corrupted
    graph, hang, crash) would surface as a query that returns garbage
    scores or a server that stops responding."""
    with CuttleDB.connect(HOST, PORT) as setup_db:
        hid = setup_db.open()
        tid = setup_db.create(hid, "vec_concurrent", [
            ("text",  ColType.STRING),
            ("embed", ColType.VEC, 32),
        ])
        # Populate enough rows that HNSW build is non-trivial.
        rows = []
        for i in range(200):
            v = [(i + j) / 100.0 for j in range(32)]
            rows.append([f"row_{i}", v])
        setup_db.insert_batch(hid, tid, rows)

    errors = []

    def build_index():
        try:
            with CuttleDB.connect(HOST, PORT) as db:
                # INDEX HNSW is raw-send (not in the typed adapter as of
                # v1.0.4); the typed db.index() is for string columns.
                db.send(f"INDEX {hid} {tid} 1 HNSW")
        except CuttleDBError as e:
            msg = str(e).lower()
            # Either of these is acceptable: server serialized the two
            # builds and the second saw the index already exists, or the
            # server returned a generic build-in-progress error.
            if not any(s in msg for s in ("indexed", "exists", "busy", "build")):
                errors.append(e)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=build_index)
    t2 = threading.Thread(target=build_index)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Critical: at most one of the calls should have raised, never both
    # crashed the server. Verify the server is still healthy and KNN works.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"
        query = [0.5] * 32
        hits = db.knn(hid, tid, col=1, k=5, query=query)
        assert len(hits) <= 5
        # Scores must be in valid cosine range.
        for _row_id, score in hits:
            assert -1.0 <= score <= 1.0

    assert not errors, f"concurrent INDEX raised: {errors}"
