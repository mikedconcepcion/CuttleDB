"""Signal-handling tests — clean shutdown on SIGINT / SIGTERM.

These tests verify the server unwinds cleanly when asked nicely:
- Exits within a short deadline after a graceful signal
- Returns a clean exit code (0 or 130-for-SIGINT)
- Doesn't leak port binding (next start on the same port works)

Crash recovery via WAL replay is covered separately by ``test_wal_mid_tx.py``.
That suite kills with SIGKILL / TerminateProcess; this one is the
counterpart for the graceful path.

Cross-platform note:
    SIGINT/SIGTERM are POSIX. Windows has CTRL_BREAK_EVENT as the closest
    analogue and only when the child is in its own process group.
    Linux/macOS get the full set; Windows gets the CTRL_BREAK probe.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")
IS_WINDOWS = sys.platform.startswith("win")
SHUTDOWN_DEADLINE_S = 5.0

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


def _wait_listening(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def _start_server(port: int, wal_dir: str, *, own_group: bool = False):
    """Spawn a server. ``own_group`` puts the child in its own process
    group so Windows can send CTRL_BREAK_EVENT to it without nuking the
    test runner."""
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if own_group:
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
    return subprocess.Popen(
        [BINARY, "--cuttledb", str(port), "--wal-dir", wal_dir],
        **kwargs,
    )


@pytest.fixture
def wal_dir():
    d = tempfile.mkdtemp(prefix="cuttledb-sig-test-")
    yield d
    for _ in range(5):
        try:
            shutil.rmtree(d)
            break
        except OSError:
            time.sleep(0.1)


def _assert_clean_exit(proc, deadline_s: float = SHUTDOWN_DEADLINE_S,
                       allowed_codes: tuple = (0, 130, -2, -15)):
    """Wait for the process to exit; assert it did so within the deadline
    with one of the expected codes."""
    try:
        rc = proc.wait(timeout=deadline_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
        pytest.fail(
            f"server did not exit within {deadline_s}s of graceful signal"
        )
    assert rc in allowed_codes, (
        f"server exited with unexpected code {rc!r} "
        f"(allowed: {allowed_codes})"
    )


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX SIGTERM only")
def test_sigterm_clean_shutdown(wal_dir):
    """SIGTERM (proc.terminate on POSIX) → clean exit, port released."""
    port = _free_port()
    proc = _start_server(port, wal_dir)
    assert _wait_listening(port), "server did not start"

    proc.terminate()
    _assert_clean_exit(proc)

    # Port must be free immediately — no TIME_WAIT noise on shutdown path.
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        pytest.fail(f"port {port} not released after clean shutdown: {e}")
    finally:
        s.close()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX SIGINT only")
def test_sigint_clean_shutdown(wal_dir):
    """SIGINT (Ctrl-C) → clean exit, no orphaned listener."""
    port = _free_port()
    proc = _start_server(port, wal_dir)
    assert _wait_listening(port), "server did not start"

    proc.send_signal(signal.SIGINT)
    _assert_clean_exit(proc)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows CTRL_BREAK only")
def test_ctrl_break_clean_shutdown(wal_dir):
    """Windows: CTRL_BREAK_EVENT → clean exit."""
    port = _free_port()
    # Must spawn in its own group so CTRL_BREAK doesn't propagate to pytest.
    proc = _start_server(port, wal_dir, own_group=True)
    assert _wait_listening(port), "server did not start"

    proc.send_signal(signal.CTRL_BREAK_EVENT)
    # Windows handlers may return 0 or 1 depending on which handler ran;
    # also tolerate the (negative) SIGBREAK code on Python's representation.
    _assert_clean_exit(proc, allowed_codes=(0, 1, 3221225786, -1073741510))


# Durability across restart is comprehensively covered by ``test_wal.py``
# (basic recovery, mid-transaction kill, checkpoint replay, committed-tx
# replay, rolled-back-tx non-replay). Not duplicated here.
