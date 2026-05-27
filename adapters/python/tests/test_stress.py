"""Stress tests for the CuttleDB kernel registry under load.

These tests are slow (seconds each) and exercise the kernel surface
under conditions normal tests don't: concurrent clients, sustained
throughput, large payloads, adversarial inputs at high rate.

They are not collected by the normal `pytest tests/` run because
they live outside the `test_*` files the project includes by default.
Run explicitly:

    pytest tests/test_stress.py -v -s

Each test asserts correctness (zero errors, all results valid) but
NEVER asserts absolute throughput numbers — those depend on the
machine. Throughput is printed for visibility.
"""
from __future__ import annotations

import os
import socket
import statistics
import threading
import time

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


pytestmark = [
    pytest.mark.skipif(
        not _server_up(),
        reason=f"CuttleDB server not reachable at {HOST}:{PORT}",
    ),
    pytest.mark.skipif(
        not os.environ.get("CUTTLEDB_STRESS"),
        reason="set CUTTLEDB_STRESS=1 to enable stress tests",
    ),
]


# ── Scenario 1: concurrent mixed load ──────────────────────────────────


def _mixed_worker(worker_id: int, n_iter: int, results: dict) -> None:
    """Each worker opens its own connection and runs n_iter calls across
    8 different kernels (round-robin). Records (completed, errors)."""
    errors = 0
    completed = 0
    db = CuttleDB.connect(HOST, PORT)
    try:
        for i in range(n_iter):
            try:
                k = i % 8
                if k == 0:
                    r = db.exec_kernel("vsum_f32", [1.0, 2.0, 3.0])
                    if r != 6.0:
                        errors += 1
                elif k == 1:
                    r = db.exec_str_kernel("str_upper", f"w{worker_id}_{i}")
                    if r != f"W{worker_id}_{i}":
                        errors += 1
                elif k == 2:
                    r = db.exec_str_kernel("sha256", f"engram{i}")
                    if len(r) != 64:
                        errors += 1
                elif k == 3:
                    r = db.exec_str_kernel("base64_encode", "Hello!")
                    if r != "SGVsbG8h":
                        errors += 1
                elif k == 4:
                    r = db.exec_str_kernel(
                        "json_get", "name", '{"name": "alice"}'
                    )
                    if r != '"alice"':
                        errors += 1
                elif k == 5:
                    r = db.exec_no_args("now_unix_ms")
                    if r <= 0:
                        errors += 1
                elif k == 6:
                    r = db.exec_no_args_str("uuid4")
                    if len(r) != 36 or r[14] != "4":
                        errors += 1
                else:  # k == 7
                    r = db.exec_str_kernel(
                        "hmac_sha256", "key", f"msg{i}"
                    )
                    if len(r) != 64:
                        errors += 1
                completed += 1
            except CuttleDBError:
                errors += 1
            except Exception:
                errors += 1
    finally:
        db.close()
    results[worker_id] = (completed, errors)


def test_stress_concurrent_mixed_load(capsys):
    """N concurrent clients × M iterations of round-robin kernel calls.
    Verifies no errors, no hangs, no result corruption under load."""
    n_workers = 16
    n_iter = 500
    results: dict = {}
    threads: list[threading.Thread] = []

    t0 = time.time()
    for w in range(n_workers):
        t = threading.Thread(target=_mixed_worker, args=(w, n_iter, results))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "worker hung — possible deadlock"
    elapsed = time.time() - t0

    total_completed = sum(c for c, _ in results.values())
    total_errors = sum(e for _, e in results.values())

    expected_total = n_workers * n_iter
    assert total_completed == expected_total, (
        f"expected {expected_total} completions, got {total_completed}"
    )
    assert total_errors == 0, f"errors during concurrent load: {total_errors}"

    rps = total_completed / elapsed
    with capsys.disabled():
        print(
            f"\n  [concurrent] {n_workers} clients × {n_iter} iter = "
            f"{total_completed} calls in {elapsed:.2f}s = {rps:.0f} req/s, "
            f"0 errors"
        )


# ── Scenario 2: latency percentiles per representative kernel ──────────


def _latency_percentiles(kernel_name: str, call_fn, n_samples: int) -> dict:
    """Time `n_samples` calls of one kernel, return latency stats in µs."""
    latencies = []
    for _ in range(n_samples):
        t0 = time.perf_counter_ns()
        call_fn()
        latencies.append((time.perf_counter_ns() - t0) / 1000.0)  # → µs
    latencies.sort()
    return {
        "kernel": kernel_name,
        "n": n_samples,
        "min_us": latencies[0],
        "p50_us": latencies[n_samples // 2],
        "p95_us": latencies[int(n_samples * 0.95)],
        "p99_us": latencies[int(n_samples * 0.99)],
        "max_us": latencies[-1],
        "mean_us": statistics.fmean(latencies),
    }


def test_stress_latency_per_kernel(capsys):
    """Single-client latency for representative kernels across signatures.
    Reports p50/p95/p99/max in µs. No threshold assertions — just
    visibility into where the kernel surface stands."""
    n = 1000
    db = CuttleDB.connect(HOST, PORT)
    try:
        stats = []
        stats.append(_latency_percentiles(
            "vsum_f32 (10 floats)",
            lambda: db.exec_kernel("vsum_f32", [1.0] * 10), n))
        stats.append(_latency_percentiles(
            "cosine_pair (128-dim)",
            lambda: db.exec_kernel(
                "cosine_pair_f32",
                [1.0 / 128] * 128, [1.0 / 128] * 128), n))
        stats.append(_latency_percentiles(
            "str_upper (10 chars)",
            lambda: db.exec_str_kernel("str_upper", "abcdefghij"), n))
        stats.append(_latency_percentiles(
            "sha256 (40-byte msg)",
            lambda: db.exec_str_kernel("sha256", "a" * 40), n))
        stats.append(_latency_percentiles(
            "hmac_sha256 (40-byte)",
            lambda: db.exec_str_kernel(
                "hmac_sha256", "key", "a" * 40), n))
        stats.append(_latency_percentiles(
            "json_get (small object)",
            lambda: db.exec_str_kernel(
                "json_get", "user.name",
                '{"user": {"name": "x"}}'), n))
        stats.append(_latency_percentiles(
            "uuid4",
            lambda: db.exec_no_args_str("uuid4"), n))
        stats.append(_latency_percentiles(
            "now_unix_ms",
            lambda: db.exec_no_args("now_unix_ms"), n))
        stats.append(_latency_percentiles(
            "format_iso",
            lambda: db.exec_int_to_str(
                "format_iso", 1715766000000), n))
    finally:
        db.close()

    with capsys.disabled():
        print()
        print(f"  [latency] {n} single-client calls per kernel (us)")
        print(f"  {'kernel':<28} {'p50':>7} {'p95':>7} {'p99':>7} "
              f"{'max':>8} {'mean':>7}")
        for s in stats:
            print(f"  {s['kernel']:<28} "
                  f"{s['p50_us']:>7.1f} "
                  f"{s['p95_us']:>7.1f} "
                  f"{s['p99_us']:>7.1f} "
                  f"{s['max_us']:>8.1f} "
                  f"{s['mean_us']:>7.1f}")


# ── Scenario 3: large-payload sustained throughput ─────────────────────


def test_stress_large_payload_sustained(capsys):
    """Run kernels at near-max input size repeatedly. Verifies the
    server handles large inputs without resource leaks or slow-path
    pathologies. KERNEL_STR_CAP is 16KB; we run at 12KB to leave
    headroom for the wire envelope."""
    n_iter = 500
    big_text = "x" * 12288  # 12KB
    big_json = '{"data":"' + ("y" * 12000) + '"}'
    db = CuttleDB.connect(HOST, PORT)
    try:
        t0 = time.time()
        for _ in range(n_iter):
            # SHA-256 on a 12KB blob
            r1 = db.exec_str_kernel("sha256", big_text)
            assert len(r1) == 64
            # base64 round-trip
            enc = db.exec_str_kernel("base64_encode", big_text[:8192])
            dec = db.exec_str_kernel("base64_decode", enc)
            assert dec == big_text[:8192]
            # JSON validate on 12KB document
            assert db.exec_str_kernel("json_validate", big_json) == 1
        elapsed = time.time() - t0
    finally:
        db.close()

    ops_per_iter = 4  # sha256 + base64_encode + base64_decode + json_validate
    total_ops = n_iter * ops_per_iter
    with capsys.disabled():
        print(
            f"\n  [large-payload] {n_iter} iter × {ops_per_iter} kernels "
            f"= {total_ops} calls on ~12KB inputs in {elapsed:.2f}s "
            f"= {total_ops / elapsed:.0f} req/s"
        )


# ── Scenario 4: adversarial-input flood ────────────────────────────────


def test_stress_adversarial_json_flood(capsys):
    """Send malformed/deeply-nested JSON at high rate. Server must
    return -ERR cleanly each time — no crashes, no hangs, no leaks."""
    n_iter = 1000
    # JSON nested 100 deep — under our 64 cap so it errors.
    deep_object = "{" * 100 + '"x":1' + "}" * 100
    # Garbage
    garbage = "not json at all {{{[[[..."
    # Truncated
    truncated = '{"a": [1, 2,'

    db = CuttleDB.connect(HOST, PORT)
    try:
        rejected = 0
        accepted = 0
        t0 = time.time()
        for i in range(n_iter):
            inputs = [deep_object, garbage, truncated]
            sample = inputs[i % 3]
            r = db.exec_str_kernel("json_validate", sample)
            if r == 0:
                rejected += 1
            else:
                accepted += 1
        elapsed = time.time() - t0
    finally:
        db.close()

    # All three inputs should be rejected — none are valid JSON.
    assert accepted == 0, (
        f"server accepted invalid input {accepted}× — "
        f"depth guard or strict-parse may be broken"
    )
    assert rejected == n_iter

    with capsys.disabled():
        print(
            f"\n  [adversarial] {n_iter} malformed JSON inputs rejected "
            f"in {elapsed:.2f}s = {n_iter / elapsed:.0f} req/s, 0 crashes"
        )


# ── Scenario 5: pipelined many-conn churn ──────────────────────────────


def test_stress_connection_churn(capsys):
    """Open/close many short-lived connections in quick succession.
    Verifies the connection-handling path (accept, handle, close) has
    no leaks or accept-queue exhaustion on the server side."""
    n_conns = 200
    errors = 0
    t0 = time.time()
    for _ in range(n_conns):
        try:
            db = CuttleDB.connect(HOST, PORT)
            r = db.exec_no_args("now_unix_ms")
            if r <= 0:
                errors += 1
            db.close()
        except Exception:
            errors += 1
    elapsed = time.time() - t0

    assert errors == 0, f"connection-churn errors: {errors}"

    with capsys.disabled():
        print(
            f"\n  [conn-churn] {n_conns} open/exec/close cycles in "
            f"{elapsed:.2f}s = {n_conns / elapsed:.0f} cycles/s, 0 errors"
        )


# ── Scenario 6: memory-growth audit ────────────────────────────────────


def test_stress_memory_growth(capsys):
    """Sustained kernel-only load — RSS must not grow. Kernels are
    stack-only (no malloc), so any drift signals a leak somewhere in
    the wire-protocol path (recv/send buffers, error-message paths,
    etc.). Requires psutil to measure server-side memory."""
    try:
        import psutil
    except ImportError:
        pytest.skip("psutil not installed (pip install psutil)")

    server_procs = [p for p in psutil.process_iter(["name"])
                    if "cuttledb" in (p.info.get("name") or "").lower()]
    if not server_procs:
        pytest.skip("cuttledb-server process not found via psutil")
    proc = server_procs[0]

    rss_before = proc.memory_info().rss

    db = CuttleDB.connect(HOST, PORT)
    try:
        # 10K mixed kernel calls touching every signature class.
        for i in range(2500):
            db.exec_str_kernel("sha256", f"item-{i}")
            db.exec_str_kernel("json_get", "name", '{"name": "alice"}')
            db.exec_no_args("now_unix_ms")
            db.exec_no_args_str("uuid4")
    finally:
        db.close()

    # Let any deferred cleanup settle.
    time.sleep(0.5)
    rss_after = proc.memory_info().rss
    growth = rss_after - rss_before

    with capsys.disabled():
        print(
            f"\n  [mem-growth] 10000 kernel calls "
            f"RSS {rss_before/(1024*1024):.1f}MB -> "
            f"{rss_after/(1024*1024):.1f}MB "
            f"(delta {growth/(1024*1024):+.2f}MB)"
        )

    # Allow up to 2MB drift — Windows working-set is noisy and can
    # spike on first-touch of any kernel's static data. A real leak
    # at this op count would dwarf 2MB.
    assert growth < 2 * 1024 * 1024, (
        f"server RSS grew by {growth/(1024*1024):.2f}MB during 10K "
        f"kernel-only calls — possible memory leak"
    )


# ── Scenario 7: slow-loris resistance ──────────────────────────────────


def test_stress_slow_loris_legitimate_client_unaffected(capsys):
    """Open 100 connections that NEVER send a complete command (no
    newline). Then verify a legitimate client can still connect and
    execute kernels. Slow-loris classic: starving connection slots
    with stalled clients. The v0.5.11 SO_RCVTIMEO defense kicks in
    over time, but immediate behavior should also be fine since
    MAX_LIVE_CONNS = 256.
    """
    n_stalled = 100
    stalled_sockets = []
    try:
        # Open stalled connections.
        for _ in range(n_stalled):
            try:
                s = socket.create_connection((HOST, PORT), timeout=1.0)
                # Send a partial command (no newline) so the server
                # blocks waiting for the rest.
                s.sendall(b"EXEC vsum_f32 1.0")
                stalled_sockets.append(s)
            except OSError:
                break

        # Legitimate client should still work cleanly.
        db = CuttleDB.connect(HOST, PORT)
        try:
            r = db.exec_no_args("now_unix_ms")
            assert r > 0
            for i in range(50):
                got = db.exec_str_kernel("sha256", f"test-{i}")
                assert len(got) == 64
        finally:
            db.close()

        with capsys.disabled():
            print(
                f"\n  [slow-loris] {len(stalled_sockets)} stalled "
                f"connections held open; legitimate client completed "
                f"51 EXEC calls cleanly"
            )
    finally:
        for s in stalled_sockets:
            try:
                s.close()
            except OSError:
                pass


# ── Scenario 8: rate-limit edges ───────────────────────────────────────


def test_stress_rate_limit_edges():
    """Rate-limit edges (v0.5.12). Requires the server to be started
    with --rate-limit set. Set CUTTLEDB_RATE_LIMIT=<N> to enable."""
    rl = int(os.environ.get("CUTTLEDB_RATE_LIMIT", "0"))
    if rl <= 0:
        pytest.skip(
            "set CUTTLEDB_RATE_LIMIT=<server's --rate-limit> to enable; "
            "requires server restart with the flag"
        )

    # 1. Burst exactly at the limit succeeds.
    db = CuttleDB.connect(HOST, PORT)
    try:
        # Fresh window. Run rl calls back-to-back — all should succeed.
        for i in range(rl):
            r = db.exec_no_args("now_unix_ms")
            assert r > 0, f"failed at call {i}/{rl}"

        # The next call (rl+1) should be rate-limited.
        with pytest.raises(CuttleDBError) as exc:
            db.exec_no_args("now_unix_ms")
        assert "rate limit" in str(exc.value).lower()
    finally:
        db.close()

    # 2. Per-connection isolation — a separate conn isn't impacted.
    db_a = CuttleDB.connect(HOST, PORT)
    db_b = CuttleDB.connect(HOST, PORT)
    try:
        # Burn through db_a's quota.
        for _ in range(rl):
            db_a.exec_no_args("now_unix_ms")
        # db_a is now at limit, but db_b should still be free.
        for _ in range(rl):
            r = db_b.exec_no_args("now_unix_ms")
            assert r > 0
    finally:
        db_a.close()
        db_b.close()

    # 3. Window reset — after 1s, a previously rate-limited conn can again.
    db = CuttleDB.connect(HOST, PORT)
    try:
        for _ in range(rl):
            db.exec_no_args("now_unix_ms")
        # Hit the limit.
        with pytest.raises(CuttleDBError):
            db.exec_no_args("now_unix_ms")
        # Wait for the 1s window to expire.
        time.sleep(1.1)
        r = db.exec_no_args("now_unix_ms")
        assert r > 0
    finally:
        db.close()


# ── Scenario 9: kernel fuzz pass ───────────────────────────────────────


def _safe_random_text(rng, max_len: int) -> str:
    """Generate a random printable ASCII string up to max_len. Avoids
    bytes that would confuse the wire protocol at the Python adapter
    level (newlines, embedded escapes); the wire-escape helper handles
    everything else."""
    import string
    alphabet = (string.ascii_letters + string.digits
                + " !@#$%^&*()-_=+[]{}|:.,<>?/~`'\"")
    n = rng.randint(0, max_len)
    return "".join(rng.choice(alphabet) for _ in range(n))


def test_stress_kernel_fuzz_pass(capsys):
    """Run pseudo-random inputs through every kernel. Asserts:
       - server doesn't crash (a subsequent call still works)
       - either a successful result OR a clean CuttleDBError
       - no silent corruption or hung connections
    Reproducible: seeded with 42."""
    import random
    rng = random.Random(42)

    # Mix of inputs:
    #   - empty
    #   - short random
    #   - long random (12KB — near KERNEL_STR_CAP)
    #   - structured malformed (broken JSON, bad base64, bad hex)
    short_inputs = [_safe_random_text(rng, 80) for _ in range(20)]
    long_inputs = [_safe_random_text(rng, 12000) for _ in range(5)]
    malformed = [
        "", " ",
        '{"a":', '[1,2', "true",
        "NOT-BASE64!", "==,==",
        "XYZ", "ZZZZ", "g",
        "%", "%X", "%XX",
        "\\", "\\\\\\",
        "{}{}{}{}{}",   # repeated; not nested
        "very deeply " * 100,
    ]

    str_to_str_kernels = [
        "str_upper", "str_lower", "str_trim", "str_reverse",
        "json_escape",
        "url_encode", "url_decode",
        "base64_encode", "base64_decode",
        "base64url_encode", "base64url_decode",
        "hex_encode", "hex_decode",
        "sha1", "sha256", "sha512", "md5",
    ]

    str_to_int_kernels = [
        "str_length", "json_validate", "parse_iso",
    ]

    crash_count = 0
    error_count = 0
    ok_count = 0
    binary_count = 0  # kernel returned non-UTF-8 bytes (known wire-protocol limit)

    db = CuttleDB.connect(HOST, PORT)
    try:
        for inp in short_inputs + long_inputs + malformed:
            for k in str_to_str_kernels:
                try:
                    r = db.exec_str_kernel(k, inp)
                    if isinstance(r, str):
                        ok_count += 1
                    else:
                        crash_count += 1
                except CuttleDBError:
                    # Server returned -ERR cleanly — expected for malformed
                    # inputs (bad base64, oversized expansion, etc.).
                    error_count += 1
                except UnicodeDecodeError:
                    # hex_decode / base64_decode can return raw binary
                    # bytes that the line-based wire protocol can't
                    # round-trip cleanly. Documented limitation — not
                    # a crash. The server didn't die; the *Python adapter*
                    # rejected the response.
                    binary_count += 1
                    # Reconnect — the response is mid-stream after the
                    # failed decode, so the socket state is undefined.
                    db.close()
                    db = CuttleDB.connect(HOST, PORT)
            for k in str_to_int_kernels:
                try:
                    r = db.exec_str_kernel(k, inp)
                    if isinstance(r, int):
                        ok_count += 1
                    else:
                        crash_count += 1
                except CuttleDBError:
                    error_count += 1

        # Survival check: server must still be responsive.
        r = db.exec_no_args("now_unix_ms")
        assert r > 0
    finally:
        db.close()

    total = ok_count + error_count + binary_count
    with capsys.disabled():
        print(
            f"\n  [fuzz] {total} kernel calls on random + malformed inputs: "
            f"{ok_count} ok, {error_count} -ERR, {binary_count} binary-resp "
            f"(adapter limit, not a server crash), {crash_count} server crashes"
        )

    assert crash_count == 0, (
        f"fuzz pass surfaced {crash_count} crashes — kernel must always "
        f"return a value or -ERR, never lose the connection"
    )


def test_stress_slow_loris_idle_timeout_kills_stalled_conns():
    """With a short --idle-timeout-ms, the server must drop stalled
    connections. This test requires a server started with a small
    idle timeout. We skip otherwise.

    To run: start the server with --idle-timeout-ms 1500 then set
    CUTTLEDB_IDLE_TEST_MS=1500 in the environment."""
    test_timeout = int(os.environ.get("CUTTLEDB_IDLE_TEST_MS", "0"))
    if test_timeout <= 0:
        pytest.skip(
            "set CUTTLEDB_IDLE_TEST_MS=<server's --idle-timeout-ms> "
            "to enable; requires server restart with small timeout"
        )

    s = socket.create_connection((HOST, PORT), timeout=test_timeout / 1000 + 5)
    # Send a partial command so the server waits for newline.
    s.sendall(b"EXEC vsum_f32 1.0")

    # Wait for the timeout + slack, then verify the server closed us.
    time.sleep(test_timeout / 1000 + 1.0)

    s.settimeout(2.0)
    try:
        data = s.recv(4096)
        # Server should have closed → recv returns empty bytes.
        assert data == b"", (
            f"expected EOF after idle timeout, got {data!r}"
        )
    except (ConnectionResetError, ConnectionAbortedError):
        # Either is fine — server killed the connection.
        pass
    finally:
        s.close()
