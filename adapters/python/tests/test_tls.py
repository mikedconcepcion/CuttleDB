"""TLS round-trip — server with --tls-cert/--tls-key, client transport="tls".

Generates a self-signed RSA cert + key in a tempdir using `openssl`,
starts a fresh cuttledb-server process (must be built with CUTTLEDB_WITH_TLS=1),
connects with the Python SDK in TLS mode, and round-trips real wire
commands. Also verifies the security-relevant cases:
- A plain-TCP client cannot speak to a TLS-only listener.
- A TLS client succeeds.

Skipped unless:
- The cuttledb-server binary exists AND was built with CUTTLEDB_WITH_TLS=1, AND
- `openssl` is on PATH (for self-signed cert generation).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cuttledb import CuttleDB, CuttleDBError, ColType


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")


def _binary_has_tls() -> bool:
    """Probe the binary with --tls-cert pointing at a nonexistent path.
    A TLS-enabled build gets past flag parse, attempts to read the
    cert, and prints `cannot read cert file`. A non-TLS build prints
    `TLS not compiled in`. Uses port 7900 so we don't collide with a
    common dev port — server exits quickly on the missing cert path.
    """
    if not os.path.exists(BINARY):
        return False
    try:
        out = subprocess.run(
            [BINARY, "--cuttledb", "7900",
             "--tls-cert", "/nonexistent/cert.pem",
             "--tls-key",  "/nonexistent/key.pem"],
            capture_output=True, timeout=3,
        )
        stderr = (out.stderr or b"").decode("utf-8", errors="replace")
        return "cannot read cert file" in stderr
    except subprocess.TimeoutExpired:
        return False


pytestmark = pytest.mark.skipif(
    not _binary_has_tls() or shutil.which("openssl") is None,
    reason=("requires CUTTLEDB_BIN built with CUTTLEDB_WITH_TLS=1 + openssl on PATH "
            f"(BINARY={BINARY})"),
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


@pytest.fixture
def self_signed_cert():
    """Generate a self-signed RSA-2048 cert + key in a tempdir.

    `openssl req -x509 ...` is the well-known portable recipe; available
    on Linux, macOS, and Windows (MSYS2 / Git Bash include it). Returns
    (cert_path, key_path) and cleans up on teardown.
    """
    d = tempfile.mkdtemp(prefix="cuttledb-tls-test-")
    cert_path = os.path.join(d, "cert.pem")
    key_path  = os.path.join(d, "key.pem")
    # On Git Bash / MSYS, a leading '/' in -subj is misread as a Unix path
    # and translated (e.g. '/CN=localhost' becomes 'C:/Program Files/Git/CN=...').
    # The MSYS_NO_PATHCONV env var disables that path-translation pass.
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    result = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", key_path, "-out", cert_path,
         "-days", "1", "-nodes",
         "-subj", "/CN=localhost"],
        env=env, capture_output=True,
    )
    if result.returncode != 0 or not os.path.exists(cert_path):
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("openssl req failed: " +
                     result.stderr.decode("utf-8", "replace")[-500:])
    yield (cert_path, key_path)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tls_server(self_signed_cert):
    cert_path, key_path = self_signed_cert
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port),
         "--tls-cert", cert_path,
         "--tls-key",  key_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if not _wait_listening(port):
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        proc.kill()
        proc.wait()
        pytest.fail(f"TLS server did not start: {stderr!r}")
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_tls_client_can_ping(tls_server):
    """Happy path: TLS client connects to TLS server, runs PING."""
    port = tls_server
    with CuttleDB.connect("127.0.0.1", port, transport="tls",
                          tls_verify=False) as db:
        assert db.ping() == "PONG"


def test_tls_round_trip_real_workload(tls_server):
    """End-to-end wire commands through the TLS tunnel."""
    port = tls_server
    with CuttleDB.connect("127.0.0.1", port, transport="tls",
                          tls_verify=False) as db:
        hid = db.open()
        tid = db.create(hid, "secrets", [
            ("name",  ColType.STRING),
            ("value", ColType.INT),
        ])
        db.insert(hid, tid, ["alice", 100])
        db.insert(hid, tid, ["bob",   250])
        assert db.count(hid, tid) == 2
        assert db.sum(hid, tid, 1) == 350
        # Read back
        assert db.get(hid, tid, 0) == ["alice", "100"]


def test_plain_tcp_client_rejected_by_tls_server(tls_server):
    """Negative case: a plain-TCP client trying to speak the line
    protocol against a TLS-only listener must fail. Either the
    connection appears dead (TLS server waits for handshake bytes),
    or PING gets no parseable response."""
    port = tls_server
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    s.settimeout(1.0)
    try:
        s.sendall(b"PING\r\n")
        # Either server closes the connection (because PING isn't valid
        # TLS ClientHello data) or we time out waiting for a +OK that
        # never arrives. Anything but a clean +OK PONG\r\n is acceptable.
        try:
            data = s.recv(64)
        except (socket.timeout, ConnectionResetError, OSError):
            data = b""
        assert b"+OK PONG" not in data, (
            "TLS server responded to plain-TCP PING — TLS handshake "
            "guard is broken"
        )
    finally:
        s.close()


def test_tls_client_against_plain_tcp_server_handshake_fails(tmp_path):
    """Negative case: TLS client pointed at a plain-TCP (non-TLS)
    server should fail during the TLS handshake — the plain server
    speaks the line protocol, not TLS records."""
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port)
        with pytest.raises(Exception):  # ssl.SSLError, OSError, EOFError, etc.
            CuttleDB.connect("127.0.0.1", port, transport="tls",
                            tls_verify=False, timeout=2.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
