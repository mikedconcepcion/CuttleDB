"""TLS hardening suite (v0.9.0): EC keys, cipher allow-list, mTLS, hot-reload.

Extends test_tls.py (basic RSA round-trip) to the v0.9.0 transport-security
work:

  * #12 EC private key — server loads a P-256 cert/key and serves TLS.
  * #13 cipher allow-list — server started with --tls-ciphers still handshakes
        with a normal client whose offered suites overlap the list.
  * #14 mTLS — --tls-client-ca makes a client certificate mandatory: a cert
        signed by the trusted CA gets in, no cert / an untrusted cert do not.
  * #15 hot-reload — rotating the cert file on disk makes new connections
        serve the new certificate (mtime-polled in the accept loop).

The Python SDK's TLS client can't present a client certificate, so the mTLS
cases use a raw ``ssl`` socket speaking the line protocol directly. Certs are
minted in-process with ``cryptography`` (already required for the crypto
tests), so no openssl multi-step CA dance.

Skipped unless the binary was built with CUTTLEDB_WITH_TLS=1 and
``cryptography`` is importable.
"""
from __future__ import annotations

import datetime as _dt
import os
import socket
import ssl
import subprocess
import time

import pytest

from cuttledb import CuttleDB, ColType

BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, ec
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False


def _binary_has_tls() -> bool:
    if not BINARY or not os.path.exists(BINARY):
        return False
    try:
        out = subprocess.run(
            [BINARY, "--cuttledb", "7901",
             "--tls-cert", "/nonexistent/cert.pem",
             "--tls-key",  "/nonexistent/key.pem"],
            capture_output=True, timeout=3,
        )
        return "cannot read cert file" in (out.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return False


pytestmark = pytest.mark.skipif(
    not _HAVE_CRYPTO or not _binary_has_tls(),
    reason="needs CUTTLEDB_WITH_TLS=1 binary + the 'cryptography' package",
)


# ── In-process PKI (cryptography) ──────────────────────────────────────────
_PEM = serialization.Encoding.PEM if _HAVE_CRYPTO else None


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _base(subject, issuer, pubkey):
    now = _dt.datetime.now(_dt.timezone.utc)
    return (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(pubkey).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=1)))


def _make_self_signed(cn, key=None):
    key = key or _rsa()
    subj = _name(cn)
    cert = _base(subj, subj, key.public_key()).sign(key, hashes.SHA256())
    return key, cert


def _make_ca(cn):
    key = _rsa()
    subj = _name(cn)
    cert = (_base(subj, subj, key.public_key())
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _make_leaf(cn, ca_key, ca_cert, key=None):
    key = key or _rsa()
    cert = _base(_name(cn), ca_cert.subject, key.public_key()).sign(
        ca_key, hashes.SHA256())
    return key, cert


def _write_cert(path, cert):
    path.write_bytes(cert.public_bytes(_PEM))


def _write_key(path, key):
    path.write_bytes(key.private_bytes(
        _PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


# ── Server launch ──────────────────────────────────────────────────────────
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
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def _start(cert, key, *extra):
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port),
         "--tls-cert", str(cert), "--tls-key", str(key), *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if not _wait_listening(port):
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.kill(); proc.wait()
        pytest.fail(f"TLS server did not start: {err!r}")
    return port, proc


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.wait()


# ── Raw TLS client (for mTLS — the SDK can't present a client cert) ─────────
def _raw_tls_ping(port, *, client_cert=None, client_key=None, timeout=3.0):
    """Open a raw TLS connection (no server verification), send PING, return
    the decoded response. Optionally present a client certificate (mTLS)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if client_cert and client_key:
        ctx.load_cert_chain(str(client_cert), str(client_key))
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    raw.settimeout(timeout)
    ssock = ctx.wrap_socket(raw, server_hostname="localhost")
    try:
        ssock.sendall(b"PING\r\n")
        return ssock.recv(64).decode("utf-8", "replace")
    finally:
        ssock.close()


def _peer_cn(port, timeout=3.0):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    raw.settimeout(timeout)
    ssock = ctx.wrap_socket(raw, server_hostname="localhost")
    try:
        der = ssock.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    finally:
        ssock.close()


# ── #12 EC private key ──────────────────────────────────────────────────────
def test_ec_cert_round_trip(tmp_path):
    key, cert = _make_self_signed("localhost",
                                  key=ec.generate_private_key(ec.SECP256R1()))
    cert_p, key_p = tmp_path / "ec_cert.pem", tmp_path / "ec_key.pem"
    _write_cert(cert_p, cert); _write_key(key_p, key)

    port, proc = _start(cert_p, key_p)
    try:
        with CuttleDB.connect("127.0.0.1", port, transport="tls",
                              tls_verify=False) as db:
            assert db.ping() == "PONG"
            hid = db.open()
            tid = db.create(hid, "t", [("v", ColType.INT)])
            db.insert(hid, tid, [7])
            assert db.count(hid, tid) == 1
    finally:
        _stop(proc)


# ── #13 cipher allow-list ───────────────────────────────────────────────────
def test_cipher_allowlist_handshake(tmp_path):
    key, cert = _make_self_signed("localhost")
    cert_p, key_p = tmp_path / "cert.pem", tmp_path / "key.pem"
    _write_cert(cert_p, cert); _write_key(key_p, key)

    # RSA server cert → restrict to the ECDHE-RSA AES-GCM suites. A modern
    # Python ssl client offers these, so the handshake must still complete.
    port, proc = _start(cert_p, key_p, "--tls-ciphers",
                        "ECDHE-RSA-AES256-GCM-SHA384,ECDHE-RSA-AES128-GCM-SHA256")
    try:
        with CuttleDB.connect("127.0.0.1", port, transport="tls",
                              tls_verify=False) as db:
            assert db.ping() == "PONG"
    finally:
        _stop(proc)


# ── #14 mTLS ────────────────────────────────────────────────────────────────
@pytest.fixture
def mtls_pki(tmp_path):
    """Server identity + a client CA + one trusted and one untrusted client
    cert, all written to PEM files. Returns a dict of paths."""
    skey, scert = _make_self_signed("localhost")
    ca_key, ca_cert = _make_ca("CuttleDB Test Client CA")
    ckey, ccert = _make_leaf("trusted-client", ca_key, ca_cert)
    bad_ca_key, bad_ca_cert = _make_ca("Rogue CA")
    bkey, bcert = _make_leaf("rogue-client", bad_ca_key, bad_ca_cert)

    p = {
        "server_cert": tmp_path / "server_cert.pem",
        "server_key":  tmp_path / "server_key.pem",
        "client_ca":   tmp_path / "client_ca.pem",
        "client_cert": tmp_path / "client_cert.pem",
        "client_key":  tmp_path / "client_key.pem",
        "rogue_cert":  tmp_path / "rogue_cert.pem",
        "rogue_key":   tmp_path / "rogue_key.pem",
    }
    _write_cert(p["server_cert"], scert); _write_key(p["server_key"], skey)
    _write_cert(p["client_ca"], ca_cert)
    _write_cert(p["client_cert"], ccert); _write_key(p["client_key"], ckey)
    _write_cert(p["rogue_cert"], bcert); _write_key(p["rogue_key"], bkey)
    return p


def test_mtls_valid_client_cert(mtls_pki):
    port, proc = _start(mtls_pki["server_cert"], mtls_pki["server_key"],
                        "--tls-client-ca", str(mtls_pki["client_ca"]))
    try:
        resp = _raw_tls_ping(port,
                             client_cert=mtls_pki["client_cert"],
                             client_key=mtls_pki["client_key"])
        assert "PONG" in resp, f"expected PONG, got {resp!r}"
    finally:
        _stop(proc)


def test_mtls_missing_client_cert_rejected(mtls_pki):
    port, proc = _start(mtls_pki["server_cert"], mtls_pki["server_key"],
                        "--tls-client-ca", str(mtls_pki["client_ca"]))
    try:
        # No client cert: BearSSL aborts the handshake (client auth mandatory).
        # Either the handshake raises or PING never gets a PONG.
        try:
            resp = _raw_tls_ping(port)
        except (ssl.SSLError, OSError, ConnectionError, socket.timeout):
            resp = ""
        assert "PONG" not in resp, "server answered without a client cert — mTLS off"
    finally:
        _stop(proc)


def test_mtls_untrusted_client_cert_rejected(mtls_pki):
    port, proc = _start(mtls_pki["server_cert"], mtls_pki["server_key"],
                        "--tls-client-ca", str(mtls_pki["client_ca"]))
    try:
        try:
            resp = _raw_tls_ping(port,
                                 client_cert=mtls_pki["rogue_cert"],
                                 client_key=mtls_pki["rogue_key"])
        except (ssl.SSLError, OSError, ConnectionError, socket.timeout):
            resp = ""
        assert "PONG" not in resp, "server trusted a cert from an unknown CA"
    finally:
        _stop(proc)


# ── #15 cert hot-reload ─────────────────────────────────────────────────────
def test_cert_hot_reload(tmp_path):
    cert_p, key_p = tmp_path / "live_cert.pem", tmp_path / "live_key.pem"
    k1, c1 = _make_self_signed("reload-first")
    _write_cert(cert_p, c1); _write_key(key_p, k1)

    port, proc = _start(cert_p, key_p)
    try:
        assert _peer_cn(port) == "reload-first"

        # Rotate the cert/key on disk and bump mtime well past the old value
        # so the accept-loop mtime poll definitely fires on the next connect.
        k2, c2 = _make_self_signed("reload-second")
        _write_cert(cert_p, c2); _write_key(key_p, k2)
        future = time.time() + 10
        os.utime(cert_p, (future, future))
        os.utime(key_p, (future, future))

        # New connection → accept loop reloads → serves the new certificate.
        assert _peer_cn(port) == "reload-second"
    finally:
        _stop(proc)
