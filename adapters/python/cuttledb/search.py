"""CuttleSearch — client for the CuttleSearch read-only HTTP search API.

CuttleSearch is a **separate service** from CuttleDB: a read-only BM25 search
endpoint (default port 8787) that serves a pre-built index snapshot over HTTP.
It speaks JSON over HTTP, **not** the CuttleDB wire protocol — so it gets its
own client rather than a method on ``CuttleDB``. This is a free convenience for
CuttleDB users who also run CuttleSearch: a one-liner instead of hand-rolling
an HTTP request and JSON parsing.

Zero dependencies — uses ``urllib`` from the standard library.

Usage::

    from cuttledb.search import CuttleSearchClient

    cs = CuttleSearchClient("http://localhost:8787")
    res = cs.search("quarterly revenue", k=5)
    for hit in res["hits"]:
        print(hit["id"], hit["score"])
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_BASE = "http://localhost:8787"


class CuttleSearchError(Exception):
    """Error from the CuttleSearch service.

    ``code`` is the server-provided ``error.code`` (e.g. ``"bad_request"``);
    ``status`` is the HTTP status. Both are ``None`` for client-side errors
    (bad arguments, connection failures).
    """

    def __init__(self, message: str, code: Optional[str] = None,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CuttleSearchClient:
    """Thin HTTP client for the CuttleSearch read-only search API."""

    def __init__(self, base_url: str = DEFAULT_BASE, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, q: str, k: Optional[int] = None,
               mode: Optional[str] = None) -> Dict[str, Any]:
        """Run a BM25 search over the loaded index.

        :param q: search text (required, non-empty).
        :param k: max hits; server clamps to ``[1, 100]``, default 10.
        :param mode: ``"bm25"`` (default). ``"vector"``/``"hybrid"`` → 501.
        :returns: ``{"query", "k", "mode", "took_ms", "total", "hits"}`` where
                  each hit is ``{"id": int, "score": float}``.
        """
        if not isinstance(q, str) or q == "":
            raise CuttleSearchError("query must be a non-empty string")
        params: Dict[str, str] = {"q": q}
        if k is not None:
            params["k"] = str(k)
        if mode is not None:
            params["mode"] = str(mode)
        return self._get("/search?" + urllib.parse.urlencode(params))

    def health(self) -> Dict[str, Any]:
        """Liveness probe. Returns ``{"status", "service", "version"}``."""
        return self._get("/health")

    def _get(self, path: str) -> Dict[str, Any]:
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except Exception:
                raise CuttleSearchError(f"HTTP {e.code}", status=e.code) from None
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise CuttleSearchError(
                err.get("message", f"HTTP {e.code}"),
                code=err.get("code"), status=e.code,
            ) from None
        except urllib.error.URLError as e:
            raise CuttleSearchError(f"connection failed: {e.reason}") from None
