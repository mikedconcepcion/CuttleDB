"""Structured slow-query log (v1.0.4).

Verifies the new --slow-log-file <dir> flag writes NDJSON one-per-line
with the expected schema. Spins up its own server like test_wal.py
because it needs custom flags.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cuttledb import CuttleDB, ColType


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")

pytestmark = pytest.mark.skipif(
    not BINARY or not os.path.isfile(BINARY),
    reason="set CUTTLEDB_SERVER_BIN to point at a cuttledb-server binary",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


@pytest.fixture
def slow_log_server():
    d = tempfile.mkdtemp(prefix="cuttledb-slowlog-")
    port = _free_port()
    # threshold=1ms — any command taking >=1ms gets logged. SAVE is
    # easily slow enough; PING usually isn't.
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port),
         "--slow-log-ms", "1",
         "--slow-log-file", d],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if not _wait_listening(port):
        stderr = proc.stderr.read().decode("utf-8", "replace")
        proc.kill()
        proc.wait()
        pytest.fail(f"server did not start: {stderr!r}")
    try:
        yield (port, d)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Best-effort cleanup
        for _ in range(5):
            try:
                shutil.rmtree(d)
                break
            except OSError:
                time.sleep(0.1)


def _ndjson_lines_in(dir_path: str) -> list[dict]:
    """Read every line from every slow-*.ndjson file in `dir_path` and
    return them as parsed dicts. Helps tests assert against the set of
    events regardless of how many day-rotation files were created."""
    out = []
    for fn in sorted(os.listdir(dir_path)):
        if not fn.startswith("slow-") or not fn.endswith(".ndjson"):
            continue
        with open(os.path.join(dir_path, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def test_slow_log_file_is_created_and_named(slow_log_server):
    """Trigger a slow command (SAVE writes a snapshot); verify the
    file is created with the expected naming pattern."""
    port, log_dir = slow_log_server
    with CuttleDB.connect("127.0.0.1", port) as db:
        hid = db.open()
        tid = db.create(hid, "t", [("name", ColType.STRING)])
        db.insert(hid, tid, ["alice"])
        # SAVE writes to disk — definitely >1ms.
        path = os.path.join(tempfile.mkdtemp(prefix="cuttledb-snap-"),
                             "snap.cuttledb")
        try:
            db.save(hid, path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    # Give the server a beat to flush.
    time.sleep(0.05)

    files = [f for f in os.listdir(log_dir)
             if f.startswith("slow-") and f.endswith(".ndjson")]
    assert files, f"no slow-*.ndjson written in {log_dir}"
    # Name pattern: slow-YYYY-MM-DD.ndjson
    import re
    assert re.fullmatch(r"slow-\d{4}-\d{2}-\d{2}\.ndjson", files[0]), (
        f"unexpected filename: {files[0]}"
    )


def test_slow_log_file_contents_have_expected_schema(slow_log_server):
    """Each NDJSON line must contain ts, verb, elapsed_ms, fd, tok.

    Populates the handle with enough rows that SAVE definitely takes
    >=1ms on any non-trivial machine — small SAVEs may finish in
    sub-millisecond time and the >=1ms threshold would skip them.
    """
    port, log_dir = slow_log_server
    with CuttleDB.connect("127.0.0.1", port) as db:
        hid = db.open()
        tid = db.create(hid, "t", [
            ("name",   ColType.STRING),
            ("value",  ColType.INT),
        ])
        # 2000 inserts via bulk → snapshot will be substantial, SAVE
        # well over 1ms. Plus the INSERT_BATCH itself is likely to
        # cross 1ms and produce events.
        rows = [[f"row_{i}", i] for i in range(2000)]
        db.insert_batch(hid, tid, rows)
        path = os.path.join(tempfile.mkdtemp(prefix="cuttledb-snap-"),
                             "snap.cuttledb")
        try:
            db.save(hid, path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    time.sleep(0.1)

    events = _ndjson_lines_in(log_dir)
    assert events, "no slow-query events written despite 2000-row INSERT_BATCH + SAVE"
    # We don't insist on a specific verb because timing varies; just
    # confirm the schema on every event we DID see.
    for e in events:
        assert isinstance(e.get("ts"), int) and e["ts"] > 0
        assert isinstance(e.get("verb"), str) and e["verb"]
        assert isinstance(e.get("elapsed_ms"), int) and e["elapsed_ms"] >= 1
        assert isinstance(e.get("fd"), int)
        assert isinstance(e.get("tok"), str)


def test_slow_log_file_no_file_when_flag_unset(tmp_path):
    """Without --slow-log-file, slow queries still log to stderr (legacy
    behavior) — but no NDJSON files appear anywhere."""
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port), "--slow-log-ms", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port)
        with CuttleDB.connect("127.0.0.1", port) as db:
            hid = db.open()
            tid = db.create(hid, "t", [("name", ColType.STRING)])
            for _ in range(50):
                db.insert(hid, tid, [f"row_{_}"])
        # No NDJSON files in cwd or anywhere related.
        assert not any(f.startswith("slow-") and f.endswith(".ndjson")
                       for f in os.listdir(".")), "stray slow-log files in cwd"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
