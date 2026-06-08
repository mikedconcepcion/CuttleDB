"""Client-side field-encryption tests (v0.9.0 cuttledb[crypto]).

Two layers:
  * Pure-unit tests of FieldCipher (round-trip, passthrough, tamper, key len).
  * A tiny CLI (``python test_field_crypto.py enc|dec <keyhex> <value>``) so the
    JS suite can shell out and prove the ``enc:v1:`` token format is identical
    across languages. The format is the wire contract; if either side drifts
    the cross-language round-trip in field_crypto.mjs fails first.

Needs the optional ``cryptography`` package (``pip install 'cuttledb[crypto]'``);
the whole module skips cleanly without it so the base zero-dep install stays
green.
"""
from __future__ import annotations

import sys

import pytest

try:
    from cuttledb.crypto import FieldCipher, ENC_PREFIX
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False

pytestmark = pytest.mark.skipif(
    not _HAVE_CRYPTO,
    reason="cryptography not installed (pip install 'cuttledb[crypto]')",
)

# Fixed key so the cross-language CLI is deterministic about the key, not the
# ciphertext (GCM nonce is random per call — tokens differ every time).
_KEY = bytes(range(32))  # 00 01 02 ... 1f


def test_round_trip():
    c = FieldCipher(_KEY)
    for pt in ["", "alice", "hello world", "unicode: café ☕ 日本語",
               "with,comma;semi\\back\r\n", "x" * 5000]:
        tok = c.encrypt(pt)
        assert tok.startswith(ENC_PREFIX)
        assert c.decrypt(tok) == pt


def test_nonce_is_random():
    c = FieldCipher(_KEY)
    assert c.encrypt("same") != c.encrypt("same")  # fresh nonce each time


def test_passthrough_non_token():
    c = FieldCipher(_KEY)
    # A plain (unencrypted) value reads back unchanged — mixed columns are safe.
    assert c.decrypt("not-encrypted") == "not-encrypted"
    assert c.decrypt("") == ""


def test_is_encrypted():
    c = FieldCipher(_KEY)
    assert FieldCipher.is_encrypted(c.encrypt("x"))
    assert not FieldCipher.is_encrypted("plain")
    assert not FieldCipher.is_encrypted(123)
    assert not FieldCipher.is_encrypted(None)


def test_tamper_detection():
    c = FieldCipher(_KEY)
    tok = c.encrypt("secret")
    # Flip a character in the base64 body → GCM tag must reject it.
    body = tok[len(ENC_PREFIX):]
    flipped = ("A" if body[-2] != "A" else "B") + body[-1]
    bad = ENC_PREFIX + body[:-2] + flipped
    with pytest.raises(Exception):
        c.decrypt(bad)


def test_wrong_key_rejected():
    c1 = FieldCipher(_KEY)
    c2 = FieldCipher(bytes(range(1, 33)))
    tok = c1.encrypt("secret")
    with pytest.raises(Exception):
        c2.decrypt(tok)


def test_key_length_validation():
    with pytest.raises(ValueError):
        FieldCipher(b"too short")
    with pytest.raises(ValueError):
        FieldCipher(bytes(31))
    with pytest.raises(ValueError):
        FieldCipher(bytes(33))


def test_generate_key():
    k = FieldCipher.generate_key()
    assert isinstance(k, bytes) and len(k) == 32
    assert FieldCipher.generate_key() != FieldCipher.generate_key()


# ── CLI for the cross-language round-trip (driven by field_crypto.mjs) ──────
def _main(argv):
    if len(argv) != 4 or argv[1] not in ("enc", "dec"):
        sys.stderr.write("usage: test_field_crypto.py enc|dec <keyhex> <value>\n")
        return 2
    cipher = FieldCipher(bytes.fromhex(argv[2]))
    out = cipher.encrypt(argv[3]) if argv[1] == "enc" else cipher.decrypt(argv[3])
    # Write raw UTF-8 bytes — the default Windows console codec (cp1252) can't
    # encode non-Latin output (e.g. emoji) and would crash on otherwise-correct
    # plaintext. The JS driver reads stdout as UTF-8.
    sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
