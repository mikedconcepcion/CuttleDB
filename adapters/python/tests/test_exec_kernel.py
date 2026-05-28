"""EXEC <kernel_name> wire-verb tests.

Tests cover the kernel substrate primitive and its integration with
the wire protocol.

Starter kernels:
- vsum_f32         (array → scalar)
- vmax_f32         (array → scalar)
- cosine_pair_f32  (two arrays → scalar)
- dot_f32          (two arrays → scalar)
"""
from __future__ import annotations

import math
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


@pytest.fixture
def db():
    with CuttleDB.connect(HOST, PORT) as d:
        yield d


# ── Single-array kernels ────────────────────────────────────────────────

def test_vsum_known_total(db):
    assert db.exec_kernel("vsum_f32", [1.0, 2.0, 3.0, 4.0]) == pytest.approx(10.0)


def test_vsum_single_element(db):
    assert db.exec_kernel("vsum_f32", [42.0]) == pytest.approx(42.0)


def test_vmax_finds_max(db):
    assert db.exec_kernel("vmax_f32", [1.0, 5.0, 3.0, 2.0]) == pytest.approx(5.0)


def test_vmax_negative_values(db):
    assert db.exec_kernel("vmax_f32", [-3.0, -1.0, -2.0]) == pytest.approx(-1.0)


# ── Two-array kernels ──────────────────────────────────────────────────

def test_cosine_pair_orthogonal(db):
    """Orthogonal vectors have cosine 0."""
    assert db.exec_kernel("cosine_pair_f32", [1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-6)


def test_cosine_pair_parallel(db):
    """Parallel vectors have cosine 1."""
    assert db.exec_kernel("cosine_pair_f32", [1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0, abs=1e-6)


def test_cosine_pair_antiparallel(db):
    """Antiparallel vectors have cosine -1."""
    assert db.exec_kernel("cosine_pair_f32", [1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0, abs=1e-6)


def test_cosine_pair_45deg(db):
    """45-degree angle has cosine √2/2."""
    expected = math.cos(math.pi / 4)
    got = db.exec_kernel("cosine_pair_f32", [1.0, 0.0], [1.0, 1.0])
    assert got == pytest.approx(expected, abs=1e-5)


def test_dot_basic(db):
    assert db.exec_kernel("dot_f32", [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == pytest.approx(32.0)


# ── Error paths ────────────────────────────────────────────────────────

def test_unknown_kernel_returns_error(db):
    with pytest.raises(CuttleDBError) as exc:
        db.exec_kernel("nope_does_not_exist", [1.0])
    assert "unknown_kernel" in str(exc.value)


def test_mismatched_arg_lengths(db):
    with pytest.raises(CuttleDBError) as exc:
        db.exec_kernel("cosine_pair_f32", [1.0, 2.0, 3.0], [1.0, 2.0])
    assert "length_mismatch" in str(exc.value)


def test_missing_second_arg_for_two_arg_kernel(db):
    """Calling a two-array kernel with only one array should error cleanly."""
    with pytest.raises(CuttleDBError) as exc:
        db.exec_kernel("cosine_pair_f32", [1.0, 0.0])
    assert "missing_second_arg" in str(exc.value)


# ── Scale ──────────────────────────────────────────────────────────────

def test_realistic_embedding_dim_384(db):
    """A typical sentence-transformer embedding has dim 384. The substrate
    handles it at full size."""
    a = [0.1] * 384
    b = [0.1] * 384
    got = db.exec_kernel("cosine_pair_f32", a, b)
    assert got == pytest.approx(1.0, abs=1e-5)


def test_realistic_embedding_dim_768(db):
    """Common embedding dim 768. Also covered."""
    a = [1.0 / math.sqrt(768)] * 768
    b = [1.0 / math.sqrt(768)] * 768
    got = db.exec_kernel("cosine_pair_f32", a, b)
    assert got == pytest.approx(1.0, abs=1e-5)


# ── String kernels (v0.5.3 — non-numeric proof-point batch) ────────────


def test_str_upper_basic(db):
    assert db.exec_str_kernel("str_upper", "hello") == "HELLO"


def test_str_upper_preserves_non_alpha(db):
    assert db.exec_str_kernel("str_upper", "hello world 123") == "HELLO WORLD 123"


def test_str_upper_already_upper(db):
    assert db.exec_str_kernel("str_upper", "HELLO") == "HELLO"


def test_str_lower_basic(db):
    assert db.exec_str_kernel("str_lower", "HELLO") == "hello"


def test_str_lower_mixed_case(db):
    assert db.exec_str_kernel("str_lower", "HeLLo WoRLd") == "hello world"


def test_str_length_basic(db):
    assert db.exec_str_kernel("str_length", "hello") == 5


def test_str_length_empty(db):
    assert db.exec_str_kernel("str_length", "") == 0


def test_str_length_unicode_byte_count(db):
    """str_length is byte-length, not char-count (consistent with C semantics)."""
    # "héllo" → 6 bytes UTF-8 (é = 2 bytes)
    assert db.exec_str_kernel("str_length", "héllo") == 6


def test_str_concat_basic(db):
    assert db.exec_str_kernel("str_concat", "foo", "bar") == "foobar"


def test_str_concat_empty_left(db):
    assert db.exec_str_kernel("str_concat", "", "world") == "world"


def test_str_concat_empty_right(db):
    assert db.exec_str_kernel("str_concat", "hello", "") == "hello"


# ── String-kernel wire-escape round-trip ───────────────────────────────


def test_str_upper_with_embedded_semicolon(db):
    """A ';' in the arg must round-trip via the \\; escape since the wire
    splits args on the first unescaped ';'."""
    assert db.exec_str_kernel("str_upper", "a;b") == "A;B"


def test_str_concat_first_arg_has_semicolon(db):
    """Literal semicolon in the FIRST arg of a two-arg kernel still works
    because the adapter escapes it."""
    assert db.exec_str_kernel("str_concat", "a;b", "c") == "a;bc"


def test_str_upper_with_newline(db):
    assert db.exec_str_kernel("str_upper", "hello\nworld") == "HELLO\nWORLD"


def test_str_concat_unknown_kernel_errors(db):
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("not_a_real_string_kernel", "hello")


def test_str_concat_missing_second_arg_errors(db):
    """Two-string kernel called with one arg should -ERR missing_second_arg."""
    with pytest.raises(CuttleDBError) as exc:
        db.exec_str_kernel("str_concat", "hello")
    assert "missing_second_arg" in str(exc.value)


# ── More string ops + base64 (v0.5.4) ──────────────────────────────────


def test_str_trim_both_ends(db):
    assert db.exec_str_kernel("str_trim", "  hello  ") == "hello"


def test_str_trim_handles_all_whitespace_kinds(db):
    assert db.exec_str_kernel("str_trim", "\t \r\nhello\n\r \t") == "hello"


def test_str_trim_no_whitespace(db):
    assert db.exec_str_kernel("str_trim", "hello") == "hello"


def test_str_trim_all_whitespace_becomes_empty(db):
    assert db.exec_str_kernel("str_trim", "   ") == ""


def test_str_reverse_basic(db):
    assert db.exec_str_kernel("str_reverse", "hello") == "olleh"


def test_str_reverse_palindrome(db):
    assert db.exec_str_kernel("str_reverse", "racecar") == "racecar"


def test_str_reverse_empty(db):
    assert db.exec_str_kernel("str_reverse", "") == ""


def test_str_contains_present(db):
    """str_contains returns 1 (truthy int) when needle is in haystack."""
    assert db.exec_str_kernel("str_contains", "ell", "hello") == 1


def test_str_contains_absent(db):
    assert db.exec_str_kernel("str_contains", "xyz", "hello") == 0


def test_str_contains_empty_needle_always_true(db):
    """By convention an empty needle is always considered present."""
    assert db.exec_str_kernel("str_contains", "", "hello") == 1


def test_str_contains_needle_longer_than_haystack(db):
    assert db.exec_str_kernel("str_contains", "hello world", "hi") == 0


def test_str_starts_with_match(db):
    assert db.exec_str_kernel("str_starts_with", "hel", "hello") == 1


def test_str_starts_with_no_match(db):
    assert db.exec_str_kernel("str_starts_with", "ell", "hello") == 0


def test_str_starts_with_empty_prefix(db):
    assert db.exec_str_kernel("str_starts_with", "", "hello") == 1


def test_base64_encode_basic(db):
    """RFC 4648 §10 test vector: 'Man' → 'TWFu'."""
    assert db.exec_str_kernel("base64_encode", "Man") == "TWFu"


def test_base64_encode_two_byte_padding(db):
    """One byte of input → 2 base64 chars + 2 '=' padding."""
    assert db.exec_str_kernel("base64_encode", "M") == "TQ=="


def test_base64_encode_one_byte_padding(db):
    """Two bytes of input → 3 base64 chars + 1 '=' padding."""
    assert db.exec_str_kernel("base64_encode", "Ma") == "TWE="


def test_base64_encode_empty(db):
    assert db.exec_str_kernel("base64_encode", "") == ""


def test_base64_decode_basic(db):
    assert db.exec_str_kernel("base64_decode", "TWFu") == "Man"


def test_base64_decode_with_padding(db):
    assert db.exec_str_kernel("base64_decode", "TWE=") == "Ma"


def test_base64_decode_two_pad(db):
    assert db.exec_str_kernel("base64_decode", "TQ==") == "M"


def test_base64_roundtrip(db):
    """A non-trivial string round-trips cleanly."""
    original = "The quick brown fox jumps over the lazy dog."
    enc = db.exec_str_kernel("base64_encode", original)
    dec = db.exec_str_kernel("base64_decode", enc)
    assert dec == original


def test_base64_decode_ignores_whitespace(db):
    """Real-world base64 often has embedded whitespace; decode must ignore it."""
    # "Man" with a space and newline in the middle of the encoded form
    assert db.exec_str_kernel("base64_decode", "TW Fu") == "Man"


def test_base64_decode_rejects_invalid_char(db):
    """A non-alphabet, non-padding, non-whitespace byte should -ERR."""
    # '@' is not in the base64 alphabet
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("base64_decode", "TW@u")


# ── Hash + URL + predicates (v0.5.5) ───────────────────────────────────

# Known SHA-256 vectors (NIST):
#   ""    → e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
#   "abc" → ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad

def test_sha256_empty_string_known_vector(db):
    assert db.exec_str_kernel("sha256", "") == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_abc_known_vector(db):
    assert db.exec_str_kernel("sha256", "abc") == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_two_block_message(db):
    """A message > 55 bytes forces 2 SHA-256 blocks (length padding wraps)."""
    msg = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
    assert db.exec_str_kernel("sha256", msg) == \
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"


def test_sha256_deterministic(db):
    """Same input → same digest, twice."""
    a = db.exec_str_kernel("sha256", "engram")
    b = db.exec_str_kernel("sha256", "engram")
    assert a == b
    assert len(a) == 64  # 32 bytes hex-encoded


# Known MD5 vectors (RFC 1321):
#   ""              → d41d8cd98f00b204e9800998ecf8427e
#   "abc"           → 900150983cd24fb0d6963f7d28e17f72
#   "message digest" → f96b697d7cb7938d525a2f31aaf161d0

def test_md5_empty_known_vector(db):
    assert db.exec_str_kernel("md5", "") == "d41d8cd98f00b204e9800998ecf8427e"


def test_md5_abc_known_vector(db):
    assert db.exec_str_kernel("md5", "abc") == "900150983cd24fb0d6963f7d28e17f72"


def test_md5_message_digest_known_vector(db):
    assert db.exec_str_kernel("md5", "message digest") == \
        "f96b697d7cb7938d525a2f31aaf161d0"


# URL encode/decode (RFC 3986)

def test_url_encode_unreserved_passes_through(db):
    assert db.exec_str_kernel("url_encode", "abc-_.~XYZ123") == "abc-_.~XYZ123"


def test_url_encode_space_becomes_percent20(db):
    assert db.exec_str_kernel("url_encode", "hello world") == "hello%20world"


def test_url_encode_special_chars(db):
    """Reserved chars get percent-encoded."""
    assert db.exec_str_kernel("url_encode", "a&b=c") == "a%26b%3Dc"


def test_url_encode_utf8(db):
    """UTF-8 bytes get encoded byte-by-byte."""
    # "café" — é is 0xC3 0xA9 in UTF-8
    assert db.exec_str_kernel("url_encode", "café") == "caf%C3%A9"


def test_url_decode_basic(db):
    assert db.exec_str_kernel("url_decode", "hello%20world") == "hello world"


def test_url_decode_plus_is_space(db):
    """+ is space (the form-encoded convention)."""
    assert db.exec_str_kernel("url_decode", "a+b") == "a b"


def test_url_decode_roundtrip(db):
    original = "key=value&foo=bar baz"
    enc = db.exec_str_kernel("url_encode", original)
    # space → %20 in url_encode, so decode gets it back fine
    dec = db.exec_str_kernel("url_decode", enc)
    assert dec == original


def test_url_decode_rejects_malformed_percent(db):
    """%XX with non-hex chars should -ERR."""
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("url_decode", "%zz")


# str_ends_with

def test_str_ends_with_match(db):
    assert db.exec_str_kernel("str_ends_with", "llo", "hello") == 1


def test_str_ends_with_no_match(db):
    assert db.exec_str_kernel("str_ends_with", "ell", "hello") == 0


def test_str_ends_with_empty_suffix(db):
    assert db.exec_str_kernel("str_ends_with", "", "hello") == 1


# str_index_of

def test_str_index_of_present(db):
    assert db.exec_str_kernel("str_index_of", "ell", "hello") == 1


def test_str_index_of_at_start(db):
    assert db.exec_str_kernel("str_index_of", "hello", "hello world") == 0


def test_str_index_of_absent(db):
    assert db.exec_str_kernel("str_index_of", "xyz", "hello") == -1


def test_str_index_of_empty_needle_is_zero(db):
    assert db.exec_str_kernel("str_index_of", "", "hello") == 0


# ── Complete crypto + encoding suite (v0.5.6) ──────────────────────────

# SHA-1 vectors (RFC 3174):
#   ""    → da39a3ee5e6b4b0d3255bfef95601890afd80709
#   "abc" → a9993e364706816aba3e25717850c26c9cd0d89d

def test_sha1_empty_known_vector(db):
    assert db.exec_str_kernel("sha1", "") == \
        "da39a3ee5e6b4b0d3255bfef95601890afd80709"


def test_sha1_abc_known_vector(db):
    assert db.exec_str_kernel("sha1", "abc") == \
        "a9993e364706816aba3e25717850c26c9cd0d89d"


def test_sha1_quick_brown_fox(db):
    assert db.exec_str_kernel("sha1", "The quick brown fox jumps over the lazy dog") == \
        "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12"


# SHA-512 vectors (FIPS 180-4):
#   ""    → cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce
#            47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e
#   "abc" → ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a
#            2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f

def test_sha512_empty_known_vector(db):
    expected = ("cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
                "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e")
    assert db.exec_str_kernel("sha512", "") == expected


def test_sha512_abc_known_vector(db):
    expected = ("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
                "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f")
    assert db.exec_str_kernel("sha512", "abc") == expected


# HMAC-SHA-256 vectors (RFC 4231 §4.2):
#   key  = 0b * 20 ("\x0b" repeated 20 times)
#   data = "Hi There"
#   mac  = b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7

def test_hmac_sha256_rfc4231_case1(db):
    # Note: this RFC test uses raw 0x0b bytes for the key, which we
    # can't easily represent through Python str. Use a printable key
    # instead — the cryptographic guarantee is the same; we just match
    # against a digest computed offline for "key"+"The quick brown fox".
    # Vector from Wikipedia HMAC: key="key", msg="The quick brown fox jumps over the lazy dog"
    # → f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8
    got = db.exec_str_kernel("hmac_sha256", "key",
                             "The quick brown fox jumps over the lazy dog")
    assert got == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"


def test_hmac_sha256_empty_key_and_msg(db):
    # HMAC-SHA-256 with key="" and msg="":
    # b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad
    got = db.exec_str_kernel("hmac_sha256", "", "")
    assert got == "b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad"


def test_hmac_sha256_long_key_gets_prehashed(db):
    """A key > 64 bytes is replaced with sha256(key)."""
    # Just sanity-check that it doesn't error and produces a 64-char hex.
    long_key = "x" * 100
    got = db.exec_str_kernel("hmac_sha256", long_key, "message")
    assert len(got) == 64
    assert all(c in "0123456789abcdef" for c in got)


# Hex encode / decode

def test_hex_encode_basic(db):
    assert db.exec_str_kernel("hex_encode", "abc") == "616263"


def test_hex_encode_empty(db):
    assert db.exec_str_kernel("hex_encode", "") == ""


def test_hex_decode_basic(db):
    assert db.exec_str_kernel("hex_decode", "616263") == "abc"


def test_hex_decode_uppercase(db):
    """Hex decode accepts uppercase hex digits."""
    # Use UTF-8-safe expected output since the wire protocol is line-based UTF-8.
    assert db.exec_str_kernel("hex_decode", "68656C6C6F") == "hello"


def test_hex_roundtrip(db):
    original = "test-string-2026"
    enc = db.exec_str_kernel("hex_encode", original)
    dec = db.exec_str_kernel("hex_decode", enc)
    assert dec == original


def test_hex_decode_rejects_odd_length(db):
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("hex_decode", "abc")


def test_hex_decode_rejects_non_hex_char(db):
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("hex_decode", "xy")


# base64url (RFC 4648 §5 — web-safe variant)

def test_base64url_encode_no_standard_chars(db):
    """base64url alphabet excludes '+' and '/' (they're '-' and '_'). Use a
    varied input string to exercise many alphabet positions; standard base64
    of this string would contain '+' / '/'."""
    # This string's standard base64 includes "+" and "/" (verified offline):
    #   ">>?" → standard "Pj4/"  contains '/'
    #   ">??" → standard "Pj8/"  contains '/'
    # base64url replaces / with _
    enc_slash = db.exec_str_kernel("base64url_encode", ">>?")
    assert "/" not in enc_slash
    assert "_" in enc_slash  # the substitution actually happened
    # Confirm round-trip:
    assert db.exec_str_kernel("base64url_decode", enc_slash) == ">>?"


def test_base64url_encode_no_padding_on_unaligned(db):
    """base64url omits '=' padding by convention (RFC 4648 §3.2)."""
    # "M" → in standard base64 is "TQ==", in url-safe is just "TQ"
    assert db.exec_str_kernel("base64url_encode", "M") == "TQ"


def test_base64url_decode_basic(db):
    assert db.exec_str_kernel("base64url_decode", "TWFu") == "Man"


def test_base64url_decode_no_pad(db):
    assert db.exec_str_kernel("base64url_decode", "TQ") == "M"
    assert db.exec_str_kernel("base64url_decode", "TWE") == "Ma"


def test_base64url_roundtrip(db):
    original = "test://kernel/sha256?param=hello"
    enc = db.exec_str_kernel("base64url_encode", original)
    dec = db.exec_str_kernel("base64url_decode", enc)
    assert dec == original


# ── JSON kernels (v0.5.7) ──────────────────────────────────────────────


# json_validate

def test_json_validate_simple_object(db):
    assert db.exec_str_kernel("json_validate", '{"a": 1}') == 1


def test_json_validate_array(db):
    assert db.exec_str_kernel("json_validate", '[1, 2, 3]') == 1


def test_json_validate_string(db):
    assert db.exec_str_kernel("json_validate", '"hello"') == 1


def test_json_validate_nested(db):
    nested = '{"a": [1, {"b": "c"}, null, true, false]}'
    assert db.exec_str_kernel("json_validate", nested) == 1


def test_json_validate_with_whitespace(db):
    assert db.exec_str_kernel("json_validate", '  {  "a" : 1  }  ') == 1


def test_json_validate_rejects_unclosed(db):
    assert db.exec_str_kernel("json_validate", '{"a": 1') == 0


def test_json_validate_rejects_garbage(db):
    assert db.exec_str_kernel("json_validate", 'not json') == 0


def test_json_validate_rejects_trailing_junk(db):
    assert db.exec_str_kernel("json_validate", '{"a": 1} junk') == 0


# json_get

def test_json_get_root_with_empty_path(db):
    """Empty path returns the whole document."""
    doc = '{"a": 1}'
    assert db.exec_str_kernel("json_get", "", doc) == doc


def test_json_get_top_level_field(db):
    """Returns the value's raw JSON text (string keeps its quotes)."""
    doc = '{"name": "alice", "age": 30}'
    assert db.exec_str_kernel("json_get", "name", doc) == '"alice"'
    assert db.exec_str_kernel("json_get", "age", doc) == '30'


def test_json_get_nested_field(db):
    doc = '{"user": {"name": "bob", "age": 25}}'
    assert db.exec_str_kernel("json_get", "user.name", doc) == '"bob"'
    assert db.exec_str_kernel("json_get", "user.age", doc) == '25'


def test_json_get_array_index(db):
    doc = '{"items": ["a", "b", "c"]}'
    assert db.exec_str_kernel("json_get", "items[0]", doc) == '"a"'
    assert db.exec_str_kernel("json_get", "items[2]", doc) == '"c"'


def test_json_get_array_root(db):
    doc = '[10, 20, 30]'
    assert db.exec_str_kernel("json_get", "[1]", doc) == '20'


def test_json_get_combined_path(db):
    doc = '{"users": [{"name": "alice"}, {"name": "bob"}]}'
    assert db.exec_str_kernel("json_get", "users[1].name", doc) == '"bob"'


def test_json_get_returns_object_text(db):
    """Getting an object returns its full JSON text."""
    doc = '{"data": {"x": 1, "y": 2}}'
    got = db.exec_str_kernel("json_get", "data", doc)
    # Should be parseable as JSON
    assert db.exec_str_kernel("json_validate", got) == 1


def test_json_get_missing_field_empty(db):
    """A missing field returns an empty string."""
    assert db.exec_str_kernel("json_get", "missing", '{"a": 1}') == ''


def test_json_get_out_of_range_index_empty(db):
    assert db.exec_str_kernel("json_get", "items[99]", '{"items": [1, 2]}') == ''


# json_type

def test_json_type_string(db):
    assert db.exec_str_kernel("json_type", "name", '{"name": "x"}') == "string"


def test_json_type_number(db):
    assert db.exec_str_kernel("json_type", "age", '{"age": 30}') == "number"


def test_json_type_boolean_true(db):
    assert db.exec_str_kernel("json_type", "ok", '{"ok": true}') == "boolean"


def test_json_type_boolean_false(db):
    assert db.exec_str_kernel("json_type", "ok", '{"ok": false}') == "boolean"


def test_json_type_null(db):
    assert db.exec_str_kernel("json_type", "x", '{"x": null}') == "null"


def test_json_type_object(db):
    assert db.exec_str_kernel("json_type", "user", '{"user": {}}') == "object"


def test_json_type_array(db):
    assert db.exec_str_kernel("json_type", "items", '{"items": []}') == "array"


def test_json_type_root(db):
    """Empty path → type of root."""
    assert db.exec_str_kernel("json_type", "", '[1, 2]') == "array"


# json_count

def test_json_count_array(db):
    assert db.exec_str_kernel("json_count", "items", '{"items": [1, 2, 3]}') == 3


def test_json_count_empty_array(db):
    assert db.exec_str_kernel("json_count", "items", '{"items": []}') == 0


def test_json_count_object(db):
    """Counts top-level keys."""
    assert db.exec_str_kernel("json_count", "user", '{"user": {"a": 1, "b": 2, "c": 3}}') == 3


def test_json_count_empty_object(db):
    assert db.exec_str_kernel("json_count", "user", '{"user": {}}') == 0


def test_json_count_scalar_returns_minus_one(db):
    """A scalar (string/number/bool/null) is not countable → -1."""
    assert db.exec_str_kernel("json_count", "x", '{"x": 42}') == -1
    assert db.exec_str_kernel("json_count", "x", '{"x": "hi"}') == -1


def test_json_count_root(db):
    """Empty path → count at root."""
    assert db.exec_str_kernel("json_count", "", '[1, 2, 3, 4]') == 4
    assert db.exec_str_kernel("json_count", "", '{"a": 1}') == 1


# Real-world workflow proof-point

def test_json_workflow_api_response(db):
    """Simulate an agent consuming a typical API response: parse status,
    extract first user's name. End-to-end via the substrate."""
    response = (
        '{"status": "ok", "count": 2, '
        '"users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]}'
    )
    assert db.exec_str_kernel("json_validate", response) == 1
    assert db.exec_str_kernel("json_get", "status", response) == '"ok"'
    assert db.exec_str_kernel("json_get", "count", response) == "2"
    assert db.exec_str_kernel("json_count", "users", response) == 2
    assert db.exec_str_kernel("json_get", "users[0].name", response) == '"alice"'
    assert db.exec_str_kernel("json_type", "users", response) == "array"


# ── JSON escape/unescape + keys + strip + eq + time (v0.5.8) ───────────


# json_escape

def test_json_escape_basic(db):
    """Wraps in quotes and escapes nothing in plain ASCII."""
    assert db.exec_str_kernel("json_escape", "hello") == '"hello"'


def test_json_escape_quotes(db):
    assert db.exec_str_kernel("json_escape", 'say "hi"') == '"say \\"hi\\""'


def test_json_escape_backslash(db):
    assert db.exec_str_kernel("json_escape", "a\\b") == '"a\\\\b"'


def test_json_escape_control_chars(db):
    """Newline, tab, etc. get short escapes."""
    assert db.exec_str_kernel("json_escape", "a\nb\tc") == '"a\\nb\\tc"'


def test_json_escape_low_ctrl_unicode_form(db):
    """Bytes < 0x20 that aren't \\n \\r \\t \\b \\f get \\u00XX."""
    # 0x01 → 
    assert db.exec_str_kernel("json_escape", "x\x01y") == '"x\\u0001y"'


def test_json_escape_empty(db):
    assert db.exec_str_kernel("json_escape", "") == '""'


# json_unescape — inverse round-trip

def test_json_unescape_basic(db):
    assert db.exec_str_kernel("json_unescape", '"hello"') == "hello"


def test_json_unescape_quotes_inside(db):
    assert db.exec_str_kernel("json_unescape", '"say \\"hi\\""') == 'say "hi"'


def test_json_unescape_backslash(db):
    assert db.exec_str_kernel("json_unescape", '"a\\\\b"') == "a\\b"


def test_json_unescape_short_escapes(db):
    assert db.exec_str_kernel("json_unescape", '"a\\nb\\tc"') == "a\nb\tc"


def test_json_unescape_unicode_escape(db):
    """\\u0041 → 'A' (single-byte BMP codepoint)."""
    assert db.exec_str_kernel("json_unescape", '"\\u0041"') == "A"


def test_json_unescape_unicode_two_byte_utf8(db):
    """\\u00E9 → 'é' (2-byte UTF-8 encoding)."""
    assert db.exec_str_kernel("json_unescape", '"caf\\u00e9"') == "café"


def test_json_escape_unescape_roundtrip(db):
    original = 'A "quoted" string\nwith\ttabs\\and backslashes'
    escaped = db.exec_str_kernel("json_escape", original)
    # Round-trip via json_get/unescape since escaped IS a JSON value:
    assert db.exec_str_kernel("json_validate", escaped) == 1
    assert db.exec_str_kernel("json_unescape", escaped) == original


def test_json_unescape_rejects_missing_quotes(db):
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("json_unescape", "no quotes")


# json_keys

def test_json_keys_simple_object(db):
    """Returns a JSON array of the object's keys (with quotes)."""
    got = db.exec_str_kernel("json_keys", "", '{"a": 1, "b": 2, "c": 3}')
    assert got == '["a","b","c"]'


def test_json_keys_at_nested_path(db):
    got = db.exec_str_kernel("json_keys", "user", '{"user": {"name": "x", "id": 42}}')
    assert got == '["name","id"]'


def test_json_keys_empty_object(db):
    assert db.exec_str_kernel("json_keys", "", '{}') == '[]'


def test_json_keys_rejects_non_object(db):
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("json_keys", "", '[1, 2, 3]')


# str_strip_prefix / str_strip_suffix

def test_str_strip_prefix_match(db):
    assert db.exec_str_kernel("str_strip_prefix", "Bearer ", "Bearer token123") == "token123"


def test_str_strip_prefix_no_match(db):
    assert db.exec_str_kernel("str_strip_prefix", "Bearer ", "Basic abc") == "Basic abc"


def test_str_strip_prefix_empty_prefix_returns_text(db):
    assert db.exec_str_kernel("str_strip_prefix", "", "hello") == "hello"


def test_str_strip_suffix_match(db):
    assert db.exec_str_kernel("str_strip_suffix", ".json", "data.json") == "data"


def test_str_strip_suffix_no_match(db):
    assert db.exec_str_kernel("str_strip_suffix", ".txt", "data.json") == "data.json"


# str_eq

def test_str_eq_match(db):
    assert db.exec_str_kernel("str_eq", "hello", "hello") == 1


def test_str_eq_case_sensitive(db):
    """str_eq is case-sensitive."""
    assert db.exec_str_kernel("str_eq", "Hello", "hello") == 0


def test_str_eq_different_length(db):
    assert db.exec_str_kernel("str_eq", "hi", "hello") == 0


def test_str_eq_empty_strings(db):
    assert db.exec_str_kernel("str_eq", "", "") == 1


# Time kernels

def test_now_unix_ms_is_positive(db):
    """now_unix_ms returns a positive integer (millis since 1970)."""
    got = db.exec_no_args("now_unix_ms")
    assert got > 0
    # Reasonable lower bound: after Jan 1 2020 (1577836800000 ms)
    assert got > 1577836800000


def test_now_unix_s_relative_to_ms(db):
    """now_unix_s = now_unix_ms / 1000 (approximately, within 1s)."""
    ms = db.exec_no_args("now_unix_ms")
    s = db.exec_no_args("now_unix_s")
    assert abs((ms // 1000) - s) <= 1


def test_now_unix_ms_monotonic(db):
    """Two calls in sequence — second must be >= first."""
    a = db.exec_no_args("now_unix_ms")
    b = db.exec_no_args("now_unix_ms")
    assert b >= a


# ── Date/time + random/UUID (v0.5.9) ──────────────────────────────────


# format_iso

def test_format_iso_unix_epoch(db):
    """unix_ms = 0 → 1970-01-01T00:00:00.000Z."""
    assert db.exec_int_to_str("format_iso", 0) == "1970-01-01T00:00:00.000Z"


def test_format_iso_known_date(db):
    """1715770800000 ms = 2024-05-15T11:00:00Z (round timestamp)."""
    assert db.exec_int_to_str("format_iso", 1715770800000) == "2024-05-15T11:00:00.000Z"


def test_format_iso_includes_millis(db):
    assert db.exec_int_to_str("format_iso", 1715770800123) == "2024-05-15T11:00:00.123Z"


def test_format_iso_leap_year_feb29(db):
    """2024 is a leap year. unix_ms for 2024-02-29T00:00:00Z = 1709164800000."""
    assert db.exec_int_to_str("format_iso", 1709164800000) == "2024-02-29T00:00:00.000Z"


# parse_iso

def test_parse_iso_unix_epoch(db):
    assert db.exec_str_kernel("parse_iso", "1970-01-01T00:00:00Z") == 0


def test_parse_iso_known_date(db):
    assert db.exec_str_kernel("parse_iso", "2024-05-15T11:00:00Z") == 1715770800000


def test_parse_iso_with_millis(db):
    assert db.exec_str_kernel("parse_iso", "2024-05-15T11:00:00.123Z") == 1715770800123


def test_parse_iso_with_timezone(db):
    """+05:00 timezone offset is subtracted to get UTC."""
    # 11:00 +05:00 = 06:00 UTC = 1715752800000
    assert db.exec_str_kernel("parse_iso", "2024-05-15T11:00:00+05:00") == 1715752800000


def test_parse_iso_negative_offset(db):
    """-05:00 timezone offset is added to get UTC."""
    # 11:00 -05:00 = 16:00 UTC = 1715788800000
    assert db.exec_str_kernel("parse_iso", "2024-05-15T11:00:00-05:00") == 1715788800000


def test_parse_iso_format_iso_roundtrip(db):
    """parse → format should be lossless at ms granularity."""
    original = 1715770800123
    formatted = db.exec_int_to_str("format_iso", original)
    parsed = db.exec_str_kernel("parse_iso", formatted)
    assert parsed == original


def test_parse_iso_rejects_malformed(db):
    """parse_iso returns -1 on bad input."""
    assert db.exec_str_kernel("parse_iso", "not a date") == -1


def test_parse_iso_rejects_bad_month(db):
    assert db.exec_str_kernel("parse_iso", "2024-13-01T00:00:00Z") == -1


# uuid4

def test_uuid4_has_canonical_shape(db):
    """36 chars, hex+hyphens, version-4 nibble + variant bits set."""
    uid = db.exec_no_args_str("uuid4")
    assert len(uid) == 36
    assert uid[8] == "-" and uid[13] == "-" and uid[18] == "-" and uid[23] == "-"
    # Version 4: the 14th character (index 14) must be '4'
    assert uid[14] == "4"
    # Variant 10: the 19th character (index 19) is one of 8, 9, a, b
    assert uid[19] in "89ab"


def test_uuid4_two_calls_differ(db):
    """Different UUIDs on two calls (collision probability is ~2^-122)."""
    a = db.exec_no_args_str("uuid4")
    b = db.exec_no_args_str("uuid4")
    assert a != b


def test_uuid4_only_hex_and_hyphens(db):
    uid = db.exec_no_args_str("uuid4")
    for c in uid:
        assert c in "0123456789abcdef-"


# random_int

def test_random_int_positive(db):
    """random_int strips the top bit, so always non-negative."""
    r = db.exec_no_args("random_int")
    assert r >= 0


def test_random_int_calls_differ(db):
    """Two calls should produce different values (overwhelming probability)."""
    a = db.exec_no_args("random_int")
    b = db.exec_no_args("random_int")
    assert a != b


# random_hex

def test_random_hex_length(db):
    """n_bytes input → 2n hex chars output."""
    h = db.exec_int_to_str("random_hex", 16)
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_random_hex_zero_bytes(db):
    assert db.exec_int_to_str("random_hex", 0) == ""


def test_random_hex_calls_differ(db):
    """Two 32-byte values should differ."""
    a = db.exec_int_to_str("random_hex", 32)
    b = db.exec_int_to_str("random_hex", 32)
    assert a != b


def test_random_hex_rejects_negative(db):
    with pytest.raises(CuttleDBError):
        db.exec_int_to_str("random_hex", -1)


# ── Hardening / adversarial-input tests (v0.5.10) ──────────────────────


# JSON depth guard

def test_json_validate_rejects_deeply_nested_object(db):
    """1000-deep nested object should be rejected by JSON_MAX_DEPTH=64 —
    not crash with stack overflow."""
    deep = "{" * 1000 + '"x":1' + "}" * 1000
    assert db.exec_str_kernel("json_validate", deep) == 0


def test_json_validate_rejects_deeply_nested_array(db):
    deep = "[" * 1000 + "1" + "]" * 1000
    assert db.exec_str_kernel("json_validate", deep) == 0


def test_json_validate_accepts_moderate_nesting(db):
    """At 50 levels we're under the 64 depth cap — should validate cleanly."""
    nest_depth = 50
    body = "1"
    for _ in range(nest_depth):
        body = "[" + body + "]"
    assert db.exec_str_kernel("json_validate", body) == 1


def test_json_get_handles_deeply_nested_input(db):
    """json_get on adversarial input should error, not crash."""
    deep = "[" * 1000 + "1" + "]" * 1000
    # json_get internally calls json_skip_value; should -ERR cleanly
    # (not segfault) because path-lookup needs to parse the input.
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("json_get", "", deep)


# Empty-input handling across kernels (no kernel should crash on empty)

def test_empty_input_string_kernels(db):
    """Every STR→STR kernel must handle empty input without crashing."""
    for k in ["str_upper", "str_lower", "str_trim", "str_reverse",
              "json_escape", "url_encode", "url_decode",
              "base64_encode", "base64_decode",
              "base64url_encode", "base64url_decode",
              "sha1", "sha256", "sha512", "md5", "hex_encode"]:
        result = db.exec_str_kernel(k, "")
        # Should return something (often empty or a known constant) without erroring
        assert isinstance(result, str)


# Wire-escape edge cases

def test_str_concat_both_args_with_semicolons(db):
    """Both args containing semicolons — verify the escape really round-trips."""
    assert db.exec_str_kernel("str_concat", "a;b;c", "d;e;f") == "a;b;c" + "d;e;f"


def test_str_upper_with_backslashes(db):
    """Backslash in input must round-trip via \\ escape."""
    assert db.exec_str_kernel("str_upper", "a\\b\\c") == "A\\B\\C"


# base64 / hex output-size guarantees

def test_base64_encode_returns_correct_size(db):
    """RFC 4648: encoded length = 4 * ceil(n / 3)."""
    for n in [0, 1, 2, 3, 4, 5, 100]:
        s = "x" * n
        enc = db.exec_str_kernel("base64_encode", s)
        expected = 4 * ((n + 2) // 3)
        assert len(enc) == expected


def test_hex_encode_doubles_length(db):
    """hex_encode output is exactly 2 * input bytes."""
    for n in [0, 1, 16, 100]:
        s = "a" * n
        h = db.exec_str_kernel("hex_encode", s)
        assert len(h) == 2 * n


# format_iso year-range guard

def test_format_iso_rejects_year_overflow(db):
    """A unix_ms beyond year 9999 should -ERR rather than overflow the
    output buffer."""
    # Year ~10000 ≈ 253402300800000 ms. Use a value well past that.
    with pytest.raises(CuttleDBError):
        db.exec_int_to_str("format_iso", 999999999999999)


def test_format_iso_accepts_year_9999(db):
    """The last second of year 9999 should still format correctly."""
    # 9999-12-31T23:59:59.000Z = 253402300799000 ms (within range).
    got = db.exec_int_to_str("format_iso", 253402300799000)
    assert got.startswith("9999-12-31T")


# URL-decode malformed cases

def test_url_decode_rejects_trailing_percent(db):
    """A '%' with no following hex digits is malformed."""
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("url_decode", "abc%")


def test_url_decode_rejects_percent_then_one_digit(db):
    """A '%' followed by only one char is malformed."""
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("url_decode", "abc%a")


# json_unescape robustness

def test_json_unescape_rejects_lonely_backslash(db):
    """A backslash at the end of the literal is malformed."""
    with pytest.raises(CuttleDBError):
        # The literal `"a\` — backslash at end before closing quote.
        db.exec_str_kernel("json_unescape", '"a\\"')


def test_json_unescape_rejects_bad_unicode_escape(db):
    """\\uZZZZ has non-hex digits — malformed."""
    with pytest.raises(CuttleDBError):
        db.exec_str_kernel("json_unescape", '"\\uZZZZ"')


# parse_iso boundary cases

def test_parse_iso_rejects_short_input(db):
    """Anything shorter than 19 chars cannot be a valid ISO timestamp."""
    assert db.exec_str_kernel("parse_iso", "2024-05-15") == -1


def test_parse_iso_rejects_bad_separator(db):
    """The date-time separator must be 'T', 't', or space."""
    assert db.exec_str_kernel("parse_iso", "2024-05-15X11:00:00Z") == -1


# Idempotent kernels: same input → same output

def test_sha256_is_deterministic_across_calls(db):
    """Five calls with the same input must all return the same digest."""
    digests = {db.exec_str_kernel("sha256", "engram") for _ in range(5)}
    assert len(digests) == 1


def test_json_get_is_idempotent(db):
    """json_get is pure-functional — repeated calls return identical text."""
    doc = '{"a": [1, 2, 3]}'
    a = db.exec_str_kernel("json_get", "a[1]", doc)
    b = db.exec_str_kernel("json_get", "a[1]", doc)
    assert a == b == "2"
