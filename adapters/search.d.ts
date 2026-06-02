// TypeScript declarations for the CuttleSearch client.

/** One ranked hit. `id` is the row id in the indexed table. */
export interface SearchHit {
    id: number;
    score: number;
}

/** Result of `search`. */
export interface SearchResponse {
    /** The decoded query, echoed back (sanitized). */
    query: string;
    /** Effective max hits (the clamped value actually applied). */
    k: number;
    /** Retrieval mode. "bm25" = lexical. */
    mode: string;
    /** Reserved (currently 0). */
    took_ms: number;
    /** Number of hits returned. */
    total: number;
    /** Ranked results, highest score first. */
    hits: SearchHit[];
}

/** Result of `health`. */
export interface HealthResponse {
    status: string;
    service: string;
    version: string;
}

/** Options for `search`. */
export interface SearchQueryOptions {
    /** Max hits. Server clamps to [1,100]; absent/zero/non-numeric → 10. */
    k?: number;
    /** Retrieval mode. "bm25" (default, live); "vector"/"hybrid" → 501. */
    mode?: string;
}

export interface CuttleSearchClientOptions {
    /** fetch implementation (defaults to globalThis.fetch; pass one on Node < 18). */
    fetch?: typeof fetch;
}

/** Error from the CuttleSearch service. */
export class CuttleSearchError extends Error {
    /** Server-provided `error.code`, e.g. "bad_request". Null for client-side errors. */
    code: string | null;
    /** HTTP status, when the error came from a response. */
    status: number | null;
}

export class CuttleSearchClient {
    constructor(baseUrl?: string, opts?: CuttleSearchClientOptions);
    /** Run a BM25 search over the loaded index. */
    search(q: string, opts?: SearchQueryOptions): Promise<SearchResponse>;
    /** Liveness probe. */
    health(): Promise<HealthResponse>;
}
