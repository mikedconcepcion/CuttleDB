"""DATETIME column type — round-trip, mixed input, range query.

Live-server tests. Skipped if CuttleDB isn't reachable.
"""
from __future__ import annotations

import datetime as dt
import os
import socket

import pytest

from cuttledb import (
    ColType,
    CuttleDB,
    datetime_to_epoch_ms,
    epoch_ms_to_datetime,
)

HOST = os.environ.get("CUTTLEDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTTLEDB_PORT", "7799"))


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
    with CuttleDB.connect(HOST, PORT) as conn:
        yield conn


@pytest.fixture
def events_table(db):
    hid = db.open()
    tid = db.create(hid, "events", [
        ("name",       ColType.STRING),
        ("created_at", ColType.DATETIME),
    ])
    yield db, hid, tid


def test_datetime_roundtrip_iso_input(events_table):
    """INSERT an ISO 8601 string; GET returns ISO 8601 string."""
    db, hid, tid = events_table
    rid = db.insert(hid, tid, ["launch", "2026-05-25T14:30:00Z"])
    name, created = db.get(hid, tid, rid)
    assert name == "launch"
    assert created == "2026-05-25T14:30:00Z"


def test_datetime_roundtrip_epoch_input(events_table):
    """INSERT a raw epoch ms int; GET returns ISO 8601 string."""
    db, hid, tid = events_table
    # 2026-05-25T14:30:00Z = 1779719400000 ms (verified)
    epoch_ms = 1779719400000
    rid = db.insert(hid, tid, ["launch", epoch_ms])
    name, created = db.get(hid, tid, rid)
    assert name == "launch"
    assert created == "2026-05-25T14:30:00Z"


def test_datetime_iso_with_milliseconds(events_table):
    """ISO 8601 with .fff precision round-trips bit-exactly."""
    db, hid, tid = events_table
    rid = db.insert(hid, tid, ["precise", "2026-05-25T14:30:00.123Z"])
    name, created = db.get(hid, tid, rid)
    assert created == "2026-05-25T14:30:00.123Z"


def test_datetime_with_timezone_offset(events_table):
    """ISO 8601 with +HH:MM offset converts to UTC on store."""
    db, hid, tid = events_table
    # 2026-05-25T10:30:00-04:00 = 2026-05-25T14:30:00Z
    rid = db.insert(hid, tid, ["ny_meeting", "2026-05-25T10:30:00-04:00"])
    _, created = db.get(hid, tid, rid)
    assert created == "2026-05-25T14:30:00Z"


def test_datetime_date_only_form(events_table):
    """Bare YYYY-MM-DD parses as midnight UTC."""
    db, hid, tid = events_table
    rid = db.insert(hid, tid, ["daily", "2026-05-25"])
    _, created = db.get(hid, tid, rid)
    assert created == "2026-05-25T00:00:00Z"


def test_datetime_python_helper_roundtrip(events_table):
    """datetime.datetime → epoch_ms → INSERT → GET → ISO 8601 → datetime."""
    db, hid, tid = events_table
    original = dt.datetime(2026, 5, 25, 14, 30, 0, tzinfo=dt.timezone.utc)
    ms = datetime_to_epoch_ms(original)
    rid = db.insert(hid, tid, ["py_event", ms])
    _, iso = db.get(hid, tid, rid)
    assert iso == "2026-05-25T14:30:00Z"
    # Round-trip via SDK helper (works on all Python versions).
    parsed_back = epoch_ms_to_datetime(ms)
    assert parsed_back == original


def test_datetime_predicate_range_via_knn_where(db):
    """KNN+WHERE with DATETIME column filters by epoch-ms threshold.

    Demonstrates the full-f64 predicate path (parse_one_predicate uses
    strtod, so any epoch ms value works). The SELGT/FCOUNT thresholds
    parse as int32 and clip large epoch-ms values — that's a pre-
    existing wire limitation, not DATETIME-specific. The KNN+WHERE
    path is the right tool for DATETIME range filtering.
    """
    hid = db.open()
    tid = db.create(hid, "events_with_emb", [
        ("name",       ColType.STRING),
        ("created_at", ColType.DATETIME),
        ("embedding",  ColType.VEC, 4),
    ])
    db.insert(hid, tid, ["jan", "2026-01-15T00:00:00Z", [1.0, 0.0, 0.0, 0.0]])
    db.insert(hid, tid, ["may", "2026-05-25T00:00:00Z", [1.0, 0.0, 0.0, 0.0]])
    db.insert(hid, tid, ["dec", "2026-12-31T00:00:00Z", [1.0, 0.0, 0.0, 0.0]])
    # Filter to "created_at > 2026-04-01" via the wire predicate.
    # Wire form: KNN <hid> <tid> <vec_col> <k> <query> WHERE col OP value
    cutoff_ms = 1775001600000  # 2026-04-01T00:00:00Z
    hits = db.knn(hid, tid, col=2, k=10, query=[1.0, 0.0, 0.0, 0.0],
                   where=f"1>{cutoff_ms}")
    assert len(hits) == 2  # may + dec


def test_datetime_min_max_aggregates(events_table):
    """MIN/MAX over DATETIME column return epoch ms (numeric path)."""
    db, hid, tid = events_table
    db.insert(hid, tid, ["a", "2026-01-15T00:00:00Z"])
    db.insert(hid, tid, ["b", "2026-12-31T00:00:00Z"])
    db.insert(hid, tid, ["c", "2026-06-15T00:00:00Z"])
    # MIN returns the smallest epoch ms; MAX the largest.
    min_ms = db.min(hid, tid, col=1)
    max_ms = db.max(hid, tid, col=1)
    # 2026-01-15Z and 2026-12-31Z respectively (epochs verified).
    assert int(min_ms) == 1768435200000  # 2026-01-15T00:00:00Z
    assert int(max_ms) == 1798675200000  # 2026-12-31T00:00:00Z


def test_datetime_iso_invalid_falls_back_to_numeric(events_table):
    """Malformed ISO falls through to numeric parse; non-numeric becomes 0."""
    db, hid, tid = events_table
    # "not-a-date" isn't valid ISO; strtod returns 0; should insert as epoch 0.
    rid = db.insert(hid, tid, ["zero", "not-a-date"])
    _, created = db.get(hid, tid, rid)
    assert created == "1970-01-01T00:00:00Z"


def test_datetime_python_helpers_isolated():
    """Helpers work without a server."""
    naive = dt.datetime(2026, 5, 25, 14, 30, 0)
    aware = naive.replace(tzinfo=dt.timezone.utc)
    # Naive treated as UTC.
    assert datetime_to_epoch_ms(naive) == datetime_to_epoch_ms(aware)
    # Round-trip.
    ms = datetime_to_epoch_ms(aware)
    back = epoch_ms_to_datetime(ms)
    assert back == aware
