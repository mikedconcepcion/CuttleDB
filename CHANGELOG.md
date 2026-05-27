# Changelog

All notable changes to CuttleDB are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.6.0] — 2026-05-27

Initial public release.

### Retrieval
- Five-mode retrieval: KNN (vector), LSEARCH (BM25), SEARCH (RRF
  hybrid), BSEARCH (Boolean DSL), filtered KNN (`KNN ... WHERE`).
- HNSW ANN index for VEC columns; full lifecycle (build / INSERT /
  DELETE / SAVE / LOAD); 12.7× faster than brute-force at
  100K × 128.
- AVX2+FMA cosine kernel.

### Writes + durability
- ACID transactions: `BEGIN` / `COMMIT` / `ROLLBACK`; DELETE inside
  transactions.
- Write-ahead log with CRC32 frames; mid-transaction kill replay.
- Bulk mutations: `UPDATE WHERE`, `DELETE WHERE`.
- Snapshot persistence: `SAVE` / `LOAD`; HNSW survives round-trip.
- DDL: `ALTER TABLE ADD COLUMN`; secondary string indexes (`INDEX`).

### Real-time
- `SUB` / `UNSUB` per-table change feed.
- `LOG <hid> <tid> [since]` ring buffer + cursor tail (1024 events).

### Aggregations
- O(1) `COUNT` and `SUM` via cached running aggregates.
- SIMD `MIN` / `MAX` / `FCOUNT`.
- `GROUPBY` with COUNT/SUM/MIN/MAX/AVG (single column, up to 256
  groups).
- 2-way inner equi-join (`JOIN`).
- DATETIME column type with ISO 8601 round-trip.

### Security + ops
- Multi-token auth: `TOKEN ADD` / `LIST` / `REVOKE`.
- Audit log NDJSON, one line per dispatched command, day-rotated.
- TLS handshake: `--tls-cert` / `--tls-key`; RSA cert; server-side
  only; `CUTTLEDB_WITH_TLS=1` build flag.
- `--max-conn N` cap (DoS defense).
- `--rate-limit N` per-connection.
- `--idle-timeout-ms N` (slow-loris defense).
- HTTP `/health` endpoint (k8s probe).
- Prometheus `/metrics` endpoint (9 series).
- Structured slow-query log:
  `--slow-log-ms N --slow-log-file <path>`; NDJSON, day-rotated.

### Transport + clients
- Line-based wire protocol; TCP + WebSocket transports on same port.
- Python adapter on PyPI (`pip install cuttledb`).
- JavaScript / TypeScript adapter on npm (`npm install cuttledb`).

### ML adapters (experimental)
- Wire verbs for server-side compute: `MATMUL`, `MATMUL_B`
  (binary-framed), `FLASH_ATTN_B`.

### Distribution
- `Cluster` client-side composition + `cuttledb.replicate` companion;
  five reference architectures in `docs/DEPLOYMENT.md`.

### Build + release
- Multi-platform CI: Linux + macOS + Windows × Python 3.10 / 3.12.
- Release binaries signed via sigstore (`cosign sign-blob` keyless).
- Apache-2.0 license.

[0.6.0]: https://github.com/mikedconcepcion/CuttleDB/releases/tag/v0.6.0
