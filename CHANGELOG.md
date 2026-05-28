# Changelog

All notable changes to CuttleDB are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.7.0] — 2026-05-28

Engine roadmap
items (hash join, outer / non-equi joins, mTLS hardening, DDL inside
transactions, GROUPBY enhancements, client-side encrypted columns)
remain deferred until there is real-user signal to order them.

### Architecture (durable fixes, not band-aids)

Three structural changes that eliminate the bug *classes* the v0.6.0
fixes addressed point-wise. Future occurrences cannot resurface.

- **One canonical per-column row emitter** (`emit_row_columns` in
  `cuttledb.c`). GET and SELGT now share one implementation; adding
  a new column type is a one-place change. The v0.6.0 SELGT/VEC
  crash existed because GET and SELGT had independent open-coded
  emitters and VEC + DATETIME were added to GET but not SELGT.
- **One safe wire-buffer append helper** (`safe_appendf`). Replaces
  the open-coded `send_n += snprintf(buf+send_n, cap-send_n, ...)`
  pattern at every one of its 13 call sites. The naive pattern lets
  `snprintf`'s desired-length return value walk `send_n` past `cap`
  and corrupt memory past the buffer end; the helper clamps on
  truncation. New wire-emit code MUST use this helper.
- **Wire-format escape contract test** (Python `test_wire_contract.py`
  + JS `wire_contract.mjs`) — single canonical list of escape
  characters tested via INSERT→GET and INSERT→SELECT_GT round-trips
  on every adapter. Encoder/decoder drift is detected on first
  divergence. The escape rules are now documented in `PROTOCOL.md`.

### Fixed

- **SELGT crash on tables with VEC columns.** The result emitter
  assumed every non-STRING column had scalar `fdata` storage and
  dereferenced it for VEC columns (which store in `vdata`). On any
  table containing a VEC column, `select_gt` killed the connection
  mid-row. Now mirrors the GET emitter's full per-column logic
  (STRING / VEC / DATETIME / numeric). Regression test
  `test_select_after_vec.py`. _Found by the new soak harness on
  its first real run._
- **Wire encoder didn't escape `;`.** STRING values containing a
  literal semicolon could split rows in SELGT output. `wire_str_encode`
  now escapes `;` in addition to `\` `,` CR LF. Decoders unescape
  symmetrically.
- **Adapter decoders split rows naively.** Both Python
  `_parse_rowlist` and JS `parseRowlist` / `get` used raw `","`
  splits and didn't honour wire escapes; any STRING value containing
  `,` (or now `;`) misaligned column or row counts on the decoded
  side. Now use escape-aware splitters
  (`_split_wire_row` / `_split_wire_rows` in Python;
  `splitWireRow` / `splitWireRows` in JS).
- **JS outbound encoder never escaped STRING values.** Pre-existing
  latent bug: `encodeValue` was `String(v)`, so any inserted STRING
  containing `,` `\` CR LF silently misaligned the column count on
  the wire. Now mirrors the Python encoder.
- **SELGT row emitter could overflow `send_buf`.** `snprintf` returns
  the desired length, which can exceed remaining buffer capacity. On
  very-high-dim VEC rows or escape-expanded long-string rows,
  `send_n` could run past `TCPH_SEND_CAP`. Now clamped on every
  increment; the worst case is a truncated row rather than a memory
  overflow.

### Added (distribution)

- **Official Docker image** (`Dockerfile` + `.github/workflows/docker.yml`)
  — distroless runtime (~25 MB), runs as non-root UID 65532. Multi-stage
  build verifies the binary's sigstore signature before assembling the
  runtime layer. Published to `ghcr.io/mikedconcepcion/cuttledb-server`
  on every release tag, tagged with the version and `latest`. linux/amd64
  only today (the only published binary platform). README quickstart
  added under "Quickstart — Docker".

### Added (QA / CI track)

- **Soak harness** (`adapters/python/tests/test_soak.py`) — long-running
  mixed-workload memory-plateau check. Default 60 s; configurable via
  `CUTTLEDB_SOAK_MINUTES`. Asserts post-warm-up RSS delta under
  threshold (default 30 MB). Sample TSV uploaded as CI artifact.
  Workflow-dispatched: `.github/workflows/soak.yml`.
- **Signal-handling tests** (`test_signals.py`) — clean shutdown on
  SIGTERM / SIGINT (POSIX) and `CTRL_BREAK_EVENT` (Windows). Asserts
  process exits within deadline with expected code and port is
  released. Durability stays under the existing `test_wal.py` suite.
- **Sanitizer-in-CI** (server-side workflow) — builds with ASan +
  UBSan and runs the C unit suite on every push and PR.
- **Fuzz harness for WAL replay** (server-side) + seed corpus +
  scheduled CI workflow. libFuzzer on Linux; corpus cached between
  runs.

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
