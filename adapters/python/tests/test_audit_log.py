"""Audit log — NDJSON per-day file + AUDIT wire verb (Tier 1.B).

Live tests. Server must be started with `--audit-dir <path>` and
`--auth <root-token>` so admin verbs are gated and audit lines are
emitted. The AUDIT_DIR env var lets us inspect the written files.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
from pathlib import Path

import pytest

from cuttledb import CuttleDB, CuttleDBError


HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7797"))
ROOT_TOKEN = os.environ.get("CUTTLEDB_ROOT_TOKEN", "audit-test-root")
AUDIT_DIR  = os.environ.get("CUTTLEDB_AUDIT_DIR", "")


def _server_up() -> bool:
    try:
        s = socket.create_connection((HOST, PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_up() or not AUDIT_DIR,
    reason=f"audit-test server + CUTTLEDB_AUDIT_DIR required (port {PORT})",
)


@pytest.fixture
def root_db():
    db = CuttleDB.connect(HOST, PORT)
    db.send(f"AUTH {ROOT_TOKEN}")
    yield db
    db.close()


def _today_audit_file() -> Path:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return Path(AUDIT_DIR) / f"audit-{today}.ndjson"


def _read_audit_lines():
    path = _today_audit_file()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_dispatch_writes_audit_line(root_db):
    """Every dispatched command produces an NDJSON line in the day's file."""
    before = len(_read_audit_lines())
    root_db.send("INFO")
    lines = _read_audit_lines()
    assert len(lines) > before
    last = lines[-1]
    assert last["verb"] == "INFO"
    assert last["ok"] is True
    assert last["tok"] == "root"
    assert "ts" in last and last["ts"] > 0


def test_failed_command_marks_ok_false(root_db):
    """When the server replies with -ERR, the audit line records ok=false."""
    try:
        root_db.send("GET 99 0 0")  # bad handle
    except CuttleDBError:
        pass
    lines = _read_audit_lines()
    assert lines, "expected at least one audit line"
    last = lines[-1]
    assert last["verb"] == "GET"
    assert last["ok"] is False


def test_token_id_attributed_per_user(root_db):
    """A connection authenticated as a minted token logs that token's id."""
    new_id, token = root_db.add_token("alice-audit")
    user_db = CuttleDB.connect(HOST, PORT)
    try:
        user_db.send(f"AUTH {token}")
        user_db.send("PING")
        lines = _read_audit_lines()
        # Last few lines should include the alice PING attributed to new_id.
        pings_from_alice = [
            ln for ln in lines[-10:]
            if ln["verb"] == "PING" and ln["tok"] == new_id
        ]
        assert pings_from_alice, (
            "expected at least one PING attributed to alice's token id "
            f"({new_id}); last 10 lines: {lines[-10:]}"
        )
    finally:
        user_db.close()


def test_audit_wire_verb_returns_path(root_db):
    """AUDIT returns the absolute path to today's audit-log file.

    The wire protocol is line-based, so streaming multi-line NDJSON
    over it requires encoding tricks. Pointing at the file is the
    cleaner contract — clients consume via standard NDJSON tooling
    (jq, Vector, Filebeat, Loki) on the path returned here.
    """
    root_db.send("INFO")
    path = root_db.send("AUDIT").strip()
    # Path should reference an existing file.
    assert path, "expected non-empty AUDIT response"
    audit_path = Path(path)
    assert audit_path.exists(), f"server returned {path} but it doesn't exist"
    # File should have one or more JSON lines (everything previously
    # dispatched, including this AUDIT call).
    with audit_path.open("r", encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) >= 2


def test_audit_root_only(root_db):
    """Non-root connections cannot call AUDIT."""
    new_id, token = root_db.add_token("bob-no-audit")
    user_db = CuttleDB.connect(HOST, PORT)
    try:
        user_db.send(f"AUTH {token}")
        with pytest.raises(CuttleDBError) as exc:
            user_db.send("AUDIT")
        assert "root_required" in str(exc.value)
    finally:
        user_db.close()
