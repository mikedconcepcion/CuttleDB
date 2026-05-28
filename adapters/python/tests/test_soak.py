"""Soak test — long-running mixed workload + memory plateau check.

Default runs 60 seconds for fast CI signal. Override via env:

    CUTTLEDB_SOAK_MINUTES=15 pytest -k soak

Asserts the server's RSS plateaus after a warm-up window. A leak shows up
as monotonic growth past the plateau threshold; a healthy server stays
flat. Sampling cadence is one sample every 5 seconds (configurable via
``CUTTLEDB_SOAK_SAMPLE_S``).

Skipped automatically unless ``CUTTLEDB_SERVER_BIN`` points at a binary.
psutil is required for RSS sampling; the test is skipped without it
rather than half-running.
"""
from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

from cuttledb import CuttleDB, ColType, Op


BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")
DURATION_S = int(float(os.environ.get("CUTTLEDB_SOAK_MINUTES", "1")) * 60)
SAMPLE_S = int(os.environ.get("CUTTLEDB_SOAK_SAMPLE_S", "5"))
WARMUP_S = min(30, DURATION_S // 4)  # discard first quarter or 30s, whichever shorter

# Plateau threshold: post-warmup RSS may oscillate but its peak-to-trough
# delta must be under this. Tunable for longer runs (more headroom is fine).
PLATEAU_DELTA_MB = float(os.environ.get("CUTTLEDB_SOAK_PLATEAU_MB", "30"))

psutil = pytest.importorskip("psutil")

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
def soak_server():
    """Start a server with a temp WAL dir; tear down on exit."""
    wal = tempfile.mkdtemp(prefix="cuttledb-soak-wal-")
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port), "--wal-dir", wal],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        if not _wait_listening(port, timeout=5.0):
            proc.kill()
            pytest.fail(f"server did not start listening on port {port}")
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        for _ in range(5):
            try:
                shutil.rmtree(wal)
                break
            except OSError:
                time.sleep(0.1)


# Column layout shared by the soak table. Indices matter for the wire
# verbs (UPDATE/DELETE/KNN/GROUPBY all take integer column ids).
COL_TEXT, COL_EMBED, COL_SCORE = 0, 1, 2
VEC_DIM = 64


def _mixed_workload_step(db, hid, tid, rng):
    """One iteration of the mixed workload.

    Heavy on reads (KNN + SELECT), regular writes (INSERT), occasional
    mutations (UPDATE/DELETE), periodic aggregate. Each branch is short to
    keep sampling stable.
    """
    op = rng.random()
    try:
        if op < 0.40:
            # KNN over embed column
            q = [rng.random() for _ in range(VEC_DIM)]
            db.knn(hid, tid, COL_EMBED, 5, q)
        elif op < 0.65:
            # INSERT — fresh row, fresh random embed
            text = f"row-{rng.randint(0, 10_000_000)}"
            emb = [rng.random() for _ in range(VEC_DIM)]
            db.insert(hid, tid, [text, emb, rng.randint(0, 100)])
        elif op < 0.75:
            # SELECT WHERE score > X
            db.select_gt(hid, tid, COL_SCORE, rng.randint(0, 90))
        elif op < 0.85:
            # UPDATE WHERE — bump score on rows matching a threshold
            db.update_where(
                hid, tid,
                set_col=COL_SCORE, set_val=rng.randint(0, 100),
                pred_col=COL_SCORE, op=Op.EQ, threshold=rng.randint(0, 100),
            )
        elif op < 0.92:
            # DELETE WHERE — by score (keeps table size bounded)
            db.delete_where(hid, tid,
                            pred_col=COL_SCORE, op=Op.EQ,
                            threshold=rng.randint(0, 100))
        else:
            # GROUPBY count by score (small fanout, <256 groups)
            db.group_by(hid, tid, group_col=COL_SCORE, agg="count")
    except Exception:
        # Soak: keep going on individual op failures (deleted-row hits,
        # transient predicate misses). A real bug shows up as RSS growth
        # or as the connection dying, both caught downstream.
        pass


def test_soak_plateau(soak_server, tmp_path):
    """Run mixed workload for DURATION_S; verify RSS plateaus post-warmup."""
    port, proc = soak_server
    p = psutil.Process(proc.pid)

    rng = random.Random(0xC0FFEE)
    samples = []  # list[(elapsed_s, rss_mb)]
    sample_log = tmp_path / "soak_samples.tsv"

    start = time.time()
    next_sample = start + SAMPLE_S

    with CuttleDB.connect("127.0.0.1", port) as db:
        hid = db.open()
        tid = db.create(hid, "soak", [
            ("text",  ColType.STRING),
            ("embed", ColType.VEC, VEC_DIM),
            ("score", ColType.INT),
        ])

        # Pre-seed so KNN has something to hit and DELETE/UPDATE aren't no-ops.
        for i in range(300):
            db.insert(hid, tid, [
                f"seed-{i}",
                [rng.random() for _ in range(VEC_DIM)],
                rng.randint(0, 100),
            ])

        # Baseline sample at t=0.
        samples.append((0.0, p.memory_info().rss / (1024 * 1024)))

        steps = 0
        while time.time() - start < DURATION_S:
            _mixed_workload_step(db, hid, tid, rng)
            steps += 1
            if time.time() >= next_sample:
                elapsed = time.time() - start
                # Bail out cleanly if the server died — surface it as a
                # test failure with context rather than a psutil traceback.
                if proc.poll() is not None:
                    out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                    pytest.fail(
                        f"server exited mid-soak after {steps} ops at "
                        f"t={elapsed:.1f}s (rc={proc.returncode})\n"
                        f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
                    )
                rss_mb = p.memory_info().rss / (1024 * 1024)
                samples.append((elapsed, rss_mb))
                next_sample += SAMPLE_S

    # Persist samples so soak runs leave a trail (collected as CI artifact).
    with sample_log.open("w") as f:
        f.write("elapsed_s\trss_mb\n")
        for t, mb in samples:
            f.write(f"{t:.1f}\t{mb:.2f}\n")
    print(f"\n[soak] {len(samples)} samples logged to {sample_log}")
    for t, mb in samples:
        print(f"  t={t:6.1f}s  rss={mb:8.2f} MB")

    # Plateau check: look at the LAST third of samples. The server has
    # legitimate one-time allocations (lazy pools, inverted-index reserves,
    # HNSW build at threshold) that show up as step-functions early in
    # the run — those are NOT leaks. A real leak shows up as continued
    # growth in the steady-state tail. Asserting on the tail isolates
    # that signal cleanly.
    assert len(samples) >= 6, (
        f"not enough samples ({len(samples)}); raise DURATION_S"
    )
    tail_start = max(WARMUP_S, samples[-(len(samples) // 3)][0])
    tail = [(t, mb) for t, mb in samples if t >= tail_start]
    assert len(tail) >= 3, (
        f"too few tail samples ({len(tail)}); raise DURATION_S"
    )

    rss_tail = [mb for _, mb in tail]
    peak, trough = max(rss_tail), min(rss_tail)
    delta = peak - trough
    print(
        f"[soak] tail (t>={tail_start:.0f}s) RSS  "
        f"trough={trough:.2f} MB  peak={peak:.2f} MB  delta={delta:.2f} MB  "
        f"threshold={PLATEAU_DELTA_MB:.0f} MB  ({len(tail)} samples)"
    )

    assert delta < PLATEAU_DELTA_MB, (
        f"RSS grew by {delta:.2f} MB in steady-state tail "
        f"(samples: {rss_tail}); threshold is {PLATEAU_DELTA_MB:.0f} MB. "
        f"Possible leak. Full trace: {[(round(t,1), round(mb,2)) for t,mb in samples]}"
    )
