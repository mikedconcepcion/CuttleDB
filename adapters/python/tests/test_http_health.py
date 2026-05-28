"""HTTP /health endpoint — k8s liveness/readiness target (v1.0.4).

Verifies the GET /health path returns 200 OK on the same port as the
line protocol + WebSocket, without requiring authentication. This is
the canonical k8s probe target.

Requires a running server on 127.0.0.1:7780.
"""
from __future__ import annotations

import os
import socket

import pytest

from cuttledb import CuttleDB


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


def _http_get(path: str, timeout: float = 1.0) -> tuple[int, dict[str, str], bytes]:
    """Minimal HTTP/1.1 client — sends GET, reads till close. Returns
    (status_code, headers_dict, body_bytes)."""
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    s.sendall(req)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    except socket.timeout:
        pass
    finally:
        s.close()
    raw = b"".join(chunks)
    if not raw:
        return (0, {}, b"")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("ascii", errors="replace")
    # "HTTP/1.1 200 OK"
    parts = status_line.split(" ", 2)
    code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    headers = {}
    for h in lines[1:]:
        k, _c, v = h.decode("ascii", errors="replace").partition(":")
        headers[k.strip().lower()] = v.strip()
    return (code, headers, body)


def test_health_returns_200_ok():
    code, headers, body = _http_get("/health")
    assert code == 200, f"got status {code}, body={body!r}"
    assert headers.get("content-type", "").startswith("text/plain")
    assert headers.get("connection") == "close"
    # Body is short and reflects healthy state.
    assert b"OK" in body


def test_health_works_with_auth_disabled_path():
    """Health probe must succeed even when normal commands would require
    AUTH. (Server here runs without --auth, so we just verify the
    pre-auth contract holds: PING also works pre-AUTH; /health uses the
    same routing point.)"""
    # First make sure /health works as plain HTTP.
    code, _, _ = _http_get("/health")
    assert code == 200

    # And the line protocol still functions on the same port.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


def test_health_doesnt_open_websocket():
    """A /health probe must NOT leave a WebSocket upgrade lingering.
    Verify by hammering with 5 probes back-to-back — the server should
    handle each as a fresh request, not get stuck in some half-upgraded
    state that exhausts threads."""
    for _ in range(5):
        code, _, _ = _http_get("/health")
        assert code == 200

    # Line protocol still responsive.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"


def test_unknown_http_path_closes_cleanly():
    """`GET /random` is NOT /health and lacks Sec-WebSocket-Key, so the
    WS handshake fails. Server must close cleanly, not crash."""
    code, _, body = _http_get("/random")
    # WS handshake fails → server closes; raw body empty (no HTTP/1.1
    # 200 response from /health, no 101 from WS).
    assert code == 0 or code >= 400
    # Server-wide health unaffected.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"
