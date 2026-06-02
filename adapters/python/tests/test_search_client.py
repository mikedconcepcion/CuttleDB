"""CuttleSearchClient — the convenience client for the CuttleSearch HTTP API.

CuttleSearch is a separate read-only BM25 service (default port 8787), distinct
from the CuttleDB line protocol. These tests hit a live CuttleSearch server;
they skip if one isn't reachable at $CUTTLESEARCH_URL (default localhost:8787).
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request

import pytest

from cuttledb.search import CuttleSearchClient, CuttleSearchError

BASE = os.environ.get("CUTTLESEARCH_URL", "http://localhost:8787")


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=0.5) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_up(), reason=f"CuttleSearch not reachable at {BASE}",
)


def test_empty_query_is_client_side_error():
    cs = CuttleSearchClient(BASE)
    with pytest.raises(CuttleSearchError) as ei:
        cs.search("")
    # Client-side guard: no HTTP round-trip, so no status/code.
    assert ei.value.status is None
    assert ei.value.code is None


def test_health():
    cs = CuttleSearchClient(BASE)
    h = cs.health()
    assert h["status"] == "ok"
    assert h["service"] == "cuttlesearch"
    assert "version" in h


def test_search_shape():
    cs = CuttleSearchClient(BASE)
    res = cs.search("the", k=3)
    assert set(res) >= {"query", "k", "mode", "took_ms", "total", "hits"}
    assert res["mode"] == "bm25"
    assert isinstance(res["hits"], list)
    assert res["total"] == len(res["hits"])
    for hit in res["hits"]:
        assert isinstance(hit["id"], int)
        assert isinstance(hit["score"], (int, float))
    # Hits are pre-sorted by score descending.
    scores = [h["score"] for h in res["hits"]]
    assert scores == sorted(scores, reverse=True)


def test_k_is_honored():
    cs = CuttleSearchClient(BASE)
    res = cs.search("the", k=1)
    assert len(res["hits"]) <= 1
    assert res["k"] == 1


def test_unimplemented_mode_raises_501():
    cs = CuttleSearchClient(BASE)
    with pytest.raises(CuttleSearchError) as ei:
        cs.search("anything", mode="vector")
    assert ei.value.status == 501
    assert ei.value.code == "not_implemented"


def test_bad_mode_raises_400():
    cs = CuttleSearchClient(BASE)
    with pytest.raises(CuttleSearchError) as ei:
        cs.search("anything", mode="bogus")
    assert ei.value.status == 400
    assert ei.value.code == "bad_request"


def test_trailing_slash_in_base_url_is_normalized():
    cs = CuttleSearchClient(BASE.rstrip("/") + "///")
    assert cs.health()["status"] == "ok"
