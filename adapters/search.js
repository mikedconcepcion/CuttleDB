// CuttleSearch — client for the CuttleSearch read-only HTTP search API.
//
// CuttleSearch is a SEPARATE service from CuttleDB: a read-only BM25 search
// endpoint (default port 8787) that serves a pre-built index snapshot over
// HTTP. It speaks JSON over HTTP, NOT the CuttleDB wire protocol — so it gets
// its own client rather than a method on CuttleDB. This is a free convenience
// for CuttleDB users who also run CuttleSearch: a one-liner instead of hand-
// rolling fetch + JSON parsing.
//
// Zero dependencies — uses the global `fetch` (Node >= 18 / browsers). On
// Node < 18, pass a fetch implementation: `new CuttleSearchClient(url, { fetch })`.
//
// Usage:
//
//   import { CuttleSearchClient } from "cuttledb/search";
//
//   const cs = new CuttleSearchClient("http://localhost:8787");
//   const res = await cs.search("quarterly revenue", { k: 5 });
//   for (const hit of res.hits) console.log(hit.id, hit.score);

const DEFAULT_BASE = "http://localhost:8787";

/** Error from the CuttleSearch service. `code` is the server-provided
 *  `error.code` (e.g. "bad_request"); `status` is the HTTP status. */
export class CuttleSearchError extends Error {
    constructor(message, { code = null, status = null } = {}) {
        super(message);
        this.name = "CuttleSearchError";
        this.code = code;
        this.status = status;
    }
}

export class CuttleSearchClient {
    /**
     * @param {string} [baseUrl] - e.g. "http://localhost:8787"
     * @param {object} [opts]
     * @param {function} [opts.fetch] - fetch impl (defaults to globalThis.fetch)
     */
    constructor(baseUrl = DEFAULT_BASE, { fetch: fetchImpl = null } = {}) {
        this.baseUrl = String(baseUrl).replace(/\/+$/, "");
        this._fetch = fetchImpl ?? globalThis.fetch;
        if (typeof this._fetch !== "function") {
            throw new Error(
                "no fetch available — pass { fetch } (e.g. from the `undici` " +
                "or `node-fetch` package on Node < 18)",
            );
        }
    }

    /**
     * Run a BM25 search over the loaded index.
     * @param {string} q - search text (required, non-empty)
     * @param {object} [opts]
     * @param {number} [opts.k] - max hits; server clamps to [1,100], default 10
     * @param {string} [opts.mode] - "bm25" (default). "vector"/"hybrid" → 501.
     * @returns {Promise<{query: string, k: number, mode: string,
     *                     took_ms: number, total: number,
     *                     hits: {id: number, score: number}[]}>}
     */
    async search(q, { k = null, mode = null } = {}) {
        if (typeof q !== "string" || q === "") {
            throw new CuttleSearchError("query must be a non-empty string");
        }
        const params = new URLSearchParams({ q });
        if (k != null) params.set("k", String(k));
        if (mode != null) params.set("mode", String(mode));
        return this._get(`/search?${params.toString()}`);
    }

    /** Liveness probe. Returns `{ status, service, version }`. */
    async health() {
        return this._get("/health");
    }

    async _get(path) {
        let res;
        try {
            res = await this._fetch(`${this.baseUrl}${path}`);
        } catch (e) {
            throw new CuttleSearchError(`connection failed: ${e.message ?? e}`);
        }
        const text = await res.text();
        let body;
        try {
            body = text ? JSON.parse(text) : {};
        } catch {
            throw new CuttleSearchError(
                `non-JSON response (HTTP ${res.status})`, { status: res.status },
            );
        }
        if (!res.ok) {
            const err = (body && body.error) || {};
            throw new CuttleSearchError(err.message || `HTTP ${res.status}`, {
                code: err.code ?? null,
                status: res.status,
            });
        }
        return body;
    }
}
