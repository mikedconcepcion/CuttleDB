"""Client-side field encryption for CuttleDB string columns (v0.9.0).

The server never sees plaintext or keys: a value is encrypted *before* it
goes on the wire and decrypted *after* it comes back, so the database stores
only opaque ciphertext in an ordinary STRING column. This is at-rest privacy
that does not depend on disk encryption or trusting the server host.

This module is the only part of the SDK that needs a real cipher, so it is
gated behind the optional ``cuttledb[crypto]`` extra (the ``cryptography``
package). The base ``cuttledb`` package stays zero-dependency — importing
this module without ``cryptography`` installed raises a clear error.

Token format (stable wire contract, identical to the JS adapter so a value
encrypted in one language decrypts in the other)::

    enc:v1:<base64( nonce[12] || ciphertext || tag[16] )>

AES-256-GCM, 12-byte random nonce per encryption, 16-byte auth tag. No
associated data, so the two language implementations are byte-identical.
"""
from __future__ import annotations

import base64
import os
from typing import Union

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - exercised via packaging
    raise ImportError(
        "cuttledb field encryption requires the 'cryptography' package. "
        "Install it with:  pip install 'cuttledb[crypto]'"
    ) from exc

__all__ = ["FieldCipher", "ENC_PREFIX"]

ENC_PREFIX = "enc:v1:"
_NONCE_LEN = 12


class FieldCipher:
    """AES-256-GCM cipher for individual CuttleDB cell values.

    ``key`` must be exactly 32 bytes. Use :meth:`generate_key` for a fresh
    random key, or supply your own from a KMS / key file. Key management is
    the caller's responsibility — losing the key means losing the data.
    """

    __slots__ = ("_aes",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("key must be bytes")
        if len(key) != 32:
            raise ValueError(
                f"key must be 32 bytes for AES-256, got {len(key)}"
            )
        self._aes = AESGCM(bytes(key))

    @staticmethod
    def generate_key() -> bytes:
        """Return a fresh random 32-byte AES-256 key."""
        return os.urandom(32)

    def encrypt(self, plaintext: Union[str, bytes]) -> str:
        """Encrypt a value, returning an ``enc:v1:…`` token (a ``str`` safe to
        store in a STRING column and to send on the wire)."""
        if isinstance(plaintext, str):
            data = plaintext.encode("utf-8")
        elif isinstance(plaintext, (bytes, bytearray)):
            data = bytes(plaintext)
        else:
            raise TypeError("plaintext must be str or bytes")
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aes.encrypt(nonce, data, None)  # ct includes the 16B tag
        return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt an ``enc:v1:…`` token back to its UTF-8 string.

        If ``token`` is not an encryption token it is returned unchanged, so a
        column holding a mix of legacy plaintext and encrypted values is safe
        to read through this method."""
        if not self.is_encrypted(token):
            return token
        raw = base64.b64decode(token[len(ENC_PREFIX):])
        if len(raw) < _NONCE_LEN + 16:
            raise ValueError("ciphertext token too short")
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return self._aes.decrypt(nonce, ct, None).decode("utf-8")

    @staticmethod
    def is_encrypted(token: object) -> bool:
        """True if ``token`` looks like an ``enc:v1:`` ciphertext token."""
        return isinstance(token, str) and token.startswith(ENC_PREFIX)
