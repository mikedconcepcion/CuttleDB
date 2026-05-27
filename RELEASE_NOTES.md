# Release Notes

## v0.6.0 — 2026-05-27

Initial public release. CuttleDB is an embedded realtime database
with vector search, WAL durability, and event streaming, shipping as
one self-contained binary with no external runtime dependencies.

### Highlights

- Five-mode retrieval: KNN (vector), BM25 (lexical), RRF hybrid,
  Boolean DSL, filtered KNN. HNSW ANN index for VEC columns —
  12.7× faster than brute force at 100K × 128.
- ACID transactions with WAL durability; mid-transaction kill replay
  exercised by integration tests.
- Real-time push: `SUB` / `UNSUB` / `LOG` per-table change feed.
- Multi-token auth, NDJSON audit log, TLS, Prometheus `/metrics`,
  HTTP `/health`, `--max-conn` cap, rate limit, structured slow-query
  log.
- Python adapter on PyPI; JS adapter on npm; WebSocket transport for
  browser clients.

### Verifying a release binary

Every binary in the GitHub Release ships with a matching `.sig` and
`.pem` for cosign verification. See `SECURITY.md` for the recipe.

### What's not yet here

Graph types, native distributed sync, mTLS, EC keys, hash join,
multi-column `GROUPBY` / `HAVING`, in-transaction DDL. Tracked in
`docs/ROADMAP.md`.

[v0.6.0]: https://github.com/mikedconcepcion/CuttleDB/releases/tag/v0.6.0
