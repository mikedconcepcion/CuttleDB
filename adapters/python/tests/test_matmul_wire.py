"""Adapter-level tests for the ML wire verbs (MATMUL, MATMUL_B, FLASH_ATTN_B).

These verbs ship inside CuttleDB's wire surface. The dims_too_large guards,
the binary-framing tcph_read_bytes partial-recv path, and the boundary edges
(M=1, K=1, N=1, oversized) are tested here at the adapter layer.

Requires a running server on 127.0.0.1:7780. Pure-numerics tests use
NumPy as the reference when it's available; fall through to manual loops
otherwise.
"""
from __future__ import annotations

import os
import socket

import pytest

from cuttledb import CuttleDB, CuttleDBError


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


try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


@pytest.fixture
def db():
    with CuttleDB.connect(HOST, PORT) as d:
        yield d


# ───────────────────────────────────────────────────────────────────────────
# MATMUL (ASCII-framed; size-capped)
# ───────────────────────────────────────────────────────────────────────────

def _approx(actual, expected, tol=1e-4):
    """matmul_f32 returns numpy ndarray. Compare via element-wise tol."""
    if HAVE_NUMPY:
        np.testing.assert_allclose(actual, expected, rtol=tol, atol=tol)
        return
    # NumPy-free fallback: flat-compare with tolerance.
    flat_a = list(actual) if not isinstance(actual, list) else actual
    for r, row in enumerate(expected):
        for c, want in enumerate(row):
            got = flat_a[r][c]
            assert abs(got - want) < tol, f"({r},{c}): {got} vs {want}"


def test_matmul_ascii_small_2x2(db):
    """Smallest reasonable matmul — 2x2 @ 2x2."""
    A = [[1.0, 2.0], [3.0, 4.0]]
    B = [[5.0, 6.0], [7.0, 8.0]]
    C = db.matmul_f32(A, B)
    _approx(C, [[19.0, 22.0], [43.0, 50.0]])


def test_matmul_ascii_unit_dims(db):
    """M=1, K=1, N=1 edges — single scalar through the verb path."""
    C = db.matmul_f32([[2.0]], [[3.0]])
    _approx(C, [[6.0]])


def test_matmul_ascii_rect_shapes(db):
    """Non-square matmul (4x2) @ (2x3) — exercises the inner-product loop."""
    A = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]   # 4x2
    B = [[1.0, 0.0, 1.0], [0.0, 1.0, 1.0]]                  # 2x3
    C = db.matmul_f32(A, B)
    _approx(C, [
        [1.0, 2.0, 3.0],
        [3.0, 4.0, 7.0],
        [5.0, 6.0, 11.0],
        [7.0, 8.0, 15.0],
    ])


# ───────────────────────────────────────────────────────────────────────────
# MATMUL_B (binary-framed; large-payload path)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAVE_NUMPY, reason="NumPy required for binary matmul")
def test_matmul_b_64x64_parity_with_numpy(db):
    rng = np.random.default_rng(seed=42)
    A = rng.standard_normal((64, 32), dtype=np.float32)
    B = rng.standard_normal((32, 64), dtype=np.float32)
    C_server = db.matmul_f32_b(A, B)
    C_ref    = A @ B
    assert C_server.shape == C_ref.shape
    np.testing.assert_allclose(C_server, C_ref, rtol=1e-3, atol=1e-3)


@pytest.mark.skipif(not HAVE_NUMPY, reason="NumPy required for binary matmul")
def test_matmul_b_partial_recv_drains_correctly(db):
    """Larger payload forces tcph_read_bytes to span multiple recv() calls.
    Verifies the partial-recv drain helper that sits between the ASCII
    header and the FP32 binary payload."""
    rng = np.random.default_rng(seed=7)
    # 128x128 @ 128x128 = 128KB output; input ~128KB. Crosses any small
    # recv buffer comfortably. Stays under MATMUL_B_MAX_INPUT_F32 (4M).
    A = rng.standard_normal((128, 128), dtype=np.float32)
    B = rng.standard_normal((128, 128), dtype=np.float32)
    C_server = db.matmul_f32_b(A, B)
    np.testing.assert_allclose(C_server, A @ B, rtol=1e-3, atol=1e-3)


@pytest.mark.skipif(not HAVE_NUMPY, reason="NumPy required for binary matmul")
def test_matmul_b_oversized_input_rejected():
    """Anything past MATMUL_B_MAX_INPUT_F32 (4M floats per input) must
    return -ERR cleanly, not crash or hang the server.

    NB: we use a dedicated connection for the rejection attempt because
    when the server rejects the size header before reading the binary
    payload, the client still has bytes queued — that connection's
    socket is desync'd and not reusable. A fresh connection verifies
    server-wide health afterward.
    """
    rng = np.random.default_rng(seed=0)
    # 2049 * 2049 = 4_198_401 floats — just over the 4M cap.
    too_big_n = 2049
    A = rng.standard_normal((too_big_n, 8), dtype=np.float32)
    B = rng.standard_normal((8,  too_big_n),  dtype=np.float32)

    rejection_db = CuttleDB.connect(HOST, PORT)
    try:
        with pytest.raises((CuttleDBError, OSError, ConnectionError)):
            rejection_db.matmul_f32_b(A, B)
    finally:
        try:
            rejection_db.close()
        except Exception:
            pass

    # Server-wide health check with a fresh connection.
    with CuttleDB.connect(HOST, PORT) as fresh:
        assert fresh.ping() == "PONG"


# ───────────────────────────────────────────────────────────────────────────
# FLASH_ATTN_B (binary-framed attention; non-causal + causal)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAVE_NUMPY, reason="NumPy required for flash-attn")
def test_flash_attn_b_non_causal_parity(db):
    """Single-head, non-causal attention via the wire verb against the
    reference impl."""
    rng = np.random.default_rng(seed=11)
    seq_q, seq_kv, d = 8, 8, 16
    Q = rng.standard_normal((seq_q, d), dtype=np.float32)
    K = rng.standard_normal((seq_kv, d), dtype=np.float32)
    V = rng.standard_normal((seq_kv, d), dtype=np.float32)
    out = db.flash_attn_f32(Q, K, V, causal=False)

    # Reference
    scale = 1.0 / np.sqrt(d)
    scores = (Q @ K.T) * scale
    weights = np.exp(scores - scores.max(axis=-1, keepdims=True))
    weights /= weights.sum(axis=-1, keepdims=True)
    ref = weights @ V

    assert out.shape == ref.shape
    np.testing.assert_allclose(out, ref, rtol=1e-3, atol=1e-3)


@pytest.mark.skipif(not HAVE_NUMPY, reason="NumPy required for flash-attn")
def test_flash_attn_b_causal_mask_zeros_upper_triangle(db):
    """Causal attention: position i must only see keys 0..i. Verify by
    constructing Q where position 0 = position 1 — outputs must DIFFER
    because position 1 sees more keys than position 0."""
    seq, d = 4, 8
    Q = np.zeros((seq, d), dtype=np.float32)
    Q[0] = Q[1] = 1.0  # identical Q at rows 0,1
    K = np.random.default_rng(seed=42).standard_normal((seq, d), dtype=np.float32)
    V = np.random.default_rng(seed=99).standard_normal((seq, d), dtype=np.float32)

    out = db.flash_attn_f32(Q, K, V, causal=True)

    # Row 0 attends to K[0] only; row 1 attends to K[0..1]. Must differ.
    assert not np.allclose(out[0], out[1], atol=1e-4), \
        "causal mask appears to be ignored (row 0 and row 1 attention identical)"
