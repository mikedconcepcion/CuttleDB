"""End-to-end encrypted-column tests (v0.9.0).

Proves the core guarantee of client-side field encryption: the *server* only
ever sees ciphertext. We launch a real CuttleDB server, insert with
``insert_enc`` / ``insert_batch_enc``, then check two things:

  * a raw ``GET`` returns an ``enc:v1:`` token in the encrypted column (the DB
    stored ciphertext, never plaintext), while plaintext columns are intact;
  * ``get_dec`` with the same key + columns returns the original plaintext.

Needs both a server binary (``CUTTLEDB_SERVER_BIN``) and the optional
``cryptography`` package; skips cleanly if either is missing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import pytest

from cuttledb import CuttleDB, Column, ColType

try:
    from cuttledb.crypto import FieldCipher
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False

BINARY = os.environ.get("CUTTLEDB_SERVER_BIN", "")

pytestmark = pytest.mark.skipif(
    not _HAVE_CRYPTO or not BINARY or not os.path.isfile(BINARY),
    reason="needs CUTTLEDB_SERVER_BIN and the 'cryptography' package",
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
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


@pytest.fixture
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "--cuttledb", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert _wait_listening(port), f"server did not start: {proc.stderr.read()!r}"
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def db(server):
    with CuttleDB.connect("127.0.0.1", server) as d:
        yield d


def _table(db):
    hid = db.open()
    # col0 = secret (STRING, encrypted), col1 = label (STRING, plaintext),
    # col2 = score (INT, plaintext).
    tid = db.create(hid, "vault", [
        Column("secret", int(ColType.STRING)),
        Column("label",  int(ColType.STRING)),
        Column("score",  int(ColType.INT)),
    ])
    return hid, tid


def test_server_stores_ciphertext_only(db):
    hid, tid = _table(db)
    cipher = FieldCipher(FieldCipher.generate_key())

    rid = db.insert_enc(hid, tid, ["top-secret", "public-label", 42],
                        cipher, enc_cols=[0])

    raw = db.get(hid, tid, rid)
    # The encrypted column is opaque on the server; the others are untouched.
    assert FieldCipher.is_encrypted(raw[0]), f"col0 not encrypted: {raw[0]!r}"
    assert "top-secret" not in raw[0]
    assert raw[1] == "public-label"
    assert raw[2] == "42"

    # Decrypting client-side restores the plaintext.
    dec = db.get_dec(hid, tid, rid, cipher, enc_cols=[0])
    assert dec == ["top-secret", "public-label", "42"]


def test_multiple_encrypted_columns(db):
    hid, tid = _table(db)
    cipher = FieldCipher(FieldCipher.generate_key())

    rid = db.insert_enc(hid, tid, ["secret-a", "secret-b", 7],
                        cipher, enc_cols=[0, 1])
    raw = db.get(hid, tid, rid)
    assert FieldCipher.is_encrypted(raw[0])
    assert FieldCipher.is_encrypted(raw[1])
    dec = db.get_dec(hid, tid, rid, cipher, enc_cols=[0, 1])
    assert dec[:2] == ["secret-a", "secret-b"]


def test_batch_encrypted(db):
    hid, tid = _table(db)
    cipher = FieldCipher(FieldCipher.generate_key())

    rows = [["alpha", f"label{i}", i] for i in range(5)]
    rids = db.insert_batch_enc(hid, tid, rows, cipher, enc_cols=[0])
    assert len(rids) == 5
    for i, rid in enumerate(rids):
        raw = db.get(hid, tid, rid)
        assert FieldCipher.is_encrypted(raw[0])
        assert raw[1] == f"label{i}"
        assert db.get_dec(hid, tid, rid, cipher, enc_cols=[0])[0] == "alpha"


def test_specials_and_unicode_roundtrip(db):
    hid, tid = _table(db)
    cipher = FieldCipher(FieldCipher.generate_key())
    payloads = ["with,comma;semi", "back\\slash\r\nlf", "café ☕ 日本語", ""]
    for pt in payloads:
        rid = db.insert_enc(hid, tid, [pt, "lbl", 0], cipher, enc_cols=[0])
        assert db.get_dec(hid, tid, rid, cipher, enc_cols=[0])[0] == pt


def test_wrong_key_cannot_decrypt(db):
    hid, tid = _table(db)
    cipher = FieldCipher(FieldCipher.generate_key())
    other = FieldCipher(FieldCipher.generate_key())
    rid = db.insert_enc(hid, tid, ["classified", "lbl", 0], cipher, enc_cols=[0])
    # A different key fails the GCM tag check — confidentiality holds even if
    # the ciphertext leaks.
    with pytest.raises(Exception):
        db.get_dec(hid, tid, rid, other, enc_cols=[0])
