"""Multi-token auth — TOKEN ADD / LIST / REVOKE, root gating, AUTH multi-match.

Live-server tests. The server must be started with `--auth <root_token>`
so the TOKEN admin verbs are gated and the test can mint additional
tokens. Tests skip if not reachable; explicit ROOT_TOKEN env var or
the fixture default.
"""
from __future__ import annotations

import os
import socket

import pytest

from cuttledb import CuttleDB, CuttleDBError


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
# Prefer an explicitly-named auth-gated port + token (set by scripts/test.sh
# and CI). Fall back to legacy CUTTLEDB_PORT / CUTTLEDB_ROOT_TOKEN for dev
# environments that pre-date the AUTH-port split.
PORT = int(
    os.environ.get("CUTTLEDB_AUTH_PORT")
    or os.environ.get("CUTTLEDB_PORT")
    or "7798"
)
ROOT_TOKEN = (
    os.environ.get("CUTTLEDB_AUTH_TOKEN")
    or os.environ.get("CUTTLEDB_ROOT_TOKEN")
    or "root-secret-for-tests"
)


def _server_up() -> bool:
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_up(),
    reason=f"CuttleDB auth-test server not reachable at {HOST}:{PORT}",
)


@pytest.fixture
def root_db():
    """Connection authenticated as root via the --auth bearer."""
    db = CuttleDB.connect(HOST, PORT)
    db.send(f"AUTH {ROOT_TOKEN}")
    yield db
    db.close()


def test_root_can_add_and_list_tokens(root_db):
    new_id, token = root_db.add_token("alice-laptop")
    assert new_id.startswith("t"), f"unexpected id format: {new_id}"
    assert len(token) == 64  # secrets.token_hex(32)
    tokens = root_db.list_tokens()
    # root + alice = at least 2; could have more from previous tests in the run.
    ids = [t["id"] for t in tokens]
    assert "root" in ids
    assert new_id in ids
    alice = next(t for t in tokens if t["id"] == new_id)
    assert alice["label"] == "alice-laptop"
    assert alice["revoked"] is False
    assert alice["created_ms"] > 0


def test_minted_token_authenticates(root_db):
    new_id, token = root_db.add_token("bob-cli")
    # New connection — authenticate with the minted token, not root.
    user_db = CuttleDB.connect(HOST, PORT)
    try:
        resp = user_db.send(f"AUTH {token}")
        assert "authenticated" in resp.lower()
        # Bob is NOT root, so admin paths must be rejected.
        with pytest.raises(CuttleDBError) as exc:
            user_db.list_tokens()
        assert "root_required" in str(exc.value)
    finally:
        user_db.close()


def test_revoked_token_no_longer_authenticates(root_db):
    new_id, token = root_db.add_token("temp-key")
    # Sanity: minted token works pre-revoke.
    user_db = CuttleDB.connect(HOST, PORT)
    try:
        assert "authenticated" in user_db.send(f"AUTH {token}").lower()
    finally:
        user_db.close()
    # Revoke + a fresh AUTH attempt must fail.
    root_db.revoke_token(new_id)
    user_db2 = CuttleDB.connect(HOST, PORT)
    try:
        with pytest.raises(CuttleDBError) as exc:
            user_db2.send(f"AUTH {token}")
        assert "auth failed" in str(exc.value).lower()
    finally:
        user_db2.close()
    # And it shows as revoked in the LIST output.
    tokens = root_db.list_tokens()
    rec = next(t for t in tokens if t["id"] == new_id)
    assert rec["revoked"] is True


def test_root_cannot_be_revoked(root_db):
    with pytest.raises(CuttleDBError) as exc:
        root_db.revoke_token("root")
    # Server returns "not_found" because token_revoke_by_id refuses
    # to act on "root" explicitly.
    msg = str(exc.value).lower()
    assert ("not_found" in msg) or ("not found" in msg) or ("root" in msg)


def test_non_root_cannot_add_tokens(root_db):
    new_id, token = root_db.add_token("carol-attempts-admin")
    user_db = CuttleDB.connect(HOST, PORT)
    try:
        user_db.send(f"AUTH {token}")
        with pytest.raises(CuttleDBError) as exc:
            user_db.add_token("escalation-attempt")
        assert "root_required" in str(exc.value)
    finally:
        user_db.close()


def test_duplicate_token_rejected(root_db):
    _, token = root_db.add_token("first")
    with pytest.raises(CuttleDBError) as exc:
        root_db.add_token("second", token=token)
    assert "token_add_failed" in str(exc.value)
