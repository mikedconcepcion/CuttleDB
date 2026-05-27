"""Prometheus /metrics endpoint — v1.0.4 observability.

Verifies the metrics surface returns the expected counters + gauges
in valid Prometheus exposition format. Pre-auth, same port as the line
protocol + WebSocket + /health.
"""
from __future__ import annotations

import os
import re
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


def _http_get(path: str, timeout: float = 1.0):
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
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode("ascii", "replace"), body.decode("utf-8", "replace")


def _parse_counter(text: str, name: str) -> int:
    """Pull `<name> <value>` from Prometheus exposition text."""
    m = re.search(rf"^{re.escape(name)}\s+(\d+)\s*$", text, re.M)
    assert m, f"counter {name!r} not found in:\n{text}"
    return int(m.group(1))


def test_metrics_returns_200_with_prometheus_content_type():
    head, body = _http_get("/metrics")
    assert "200 OK" in head
    assert "Content-Type: text/plain" in head
    assert "version=0.0.4" in head, f"Prometheus content-type missing: {head}"


def test_metrics_includes_all_expected_series():
    _head, body = _http_get("/metrics")
    expected = [
        "cuttledb_uptime_seconds",
        "cuttledb_connections_total",
        "cuttledb_connections_active",
        "cuttledb_commands_total",
        "cuttledb_command_errors_total",
        "cuttledb_max_conn_rejects_total",
        "cuttledb_handles_active",
        "cuttledb_tables_total",
        "cuttledb_subscribers_active",
    ]
    for name in expected:
        assert f"# TYPE {name} " in body, f"missing TYPE for {name}"
        # Each metric must have a numeric value line.
        _parse_counter(body, name)


def test_metrics_commands_total_increments_with_activity():
    """Issue a few commands, verify commands_total goes up by at least
    that much (other concurrent test traffic may add more)."""
    _h, before = _http_get("/metrics")
    n_before = _parse_counter(before, "cuttledb_commands_total")

    with CuttleDB.connect(HOST, PORT) as db:
        for _ in range(5):
            db.ping()

    _h, after = _http_get("/metrics")
    n_after = _parse_counter(after, "cuttledb_commands_total")
    # At least 5 PINGs + the metrics scrape itself (no — scrape isn't a
    # wire command). Strict bound: + 5.
    assert n_after - n_before >= 5, (
        f"commands_total didn't increase by >= 5: before={n_before} after={n_after}"
    )


def test_metrics_connections_total_increments_per_connect():
    _h, before = _http_get("/metrics")
    n_before = _parse_counter(before, "cuttledb_connections_total")

    for _ in range(3):
        with CuttleDB.connect(HOST, PORT) as db:
            db.ping()

    _h, after = _http_get("/metrics")
    n_after = _parse_counter(after, "cuttledb_connections_total")
    # Each `with CuttleDB.connect`: 1 connection. Plus the /metrics
    # scrape connections (2). Strict bound: at least 3 new.
    assert n_after - n_before >= 3, (
        f"connections_total didn't increase by >= 3: before={n_before} after={n_after}"
    )


def test_metrics_no_authentication_required():
    """The /metrics scrape must work even when normal commands would
    require AUTH. (Server here runs without --auth, so verify the
    pre-auth contract holds at the routing level: /metrics responds
    200 just like /health does.)"""
    head, _body = _http_get("/metrics")
    assert "200 OK" in head


def test_metrics_unknown_subpath_closes_cleanly():
    """`GET /metrics/foo` is not an exact match. Falls through to WS
    handshake which fails on missing Sec-WebSocket-Key. Connection
    closes; server stays healthy."""
    _head, _body = _http_get("/metrics/foo")
    # Server still responsive on the line protocol.
    with CuttleDB.connect(HOST, PORT) as db:
        assert db.ping() == "PONG"
