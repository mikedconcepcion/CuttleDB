# Changelog

All notable changes to CuttleDB are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.9.0] — 2026-06-06

**Theme: security depth + at-rest privacy.** TLS gains the hardening an
internal-network deployment needs, and string columns can be encrypted
client-side so the server only ever stores ciphertext.

### Added

- **TLS hardening** (opt-in `CUTTLEDB_WITH_TLS=1` build; the default
  binary stays zero-dependency and TLS-less). All four land behind the
  existing `--tls-cert` / `--tls-key` flags:
  - **EC private keys.** The PEM loader now accepts P-256 / P-384 EC
    keys in addition to RSA, so deployments can use smaller, faster
    elliptic-curve certificates.
  - **Cipher allow-list** — `--tls-ciphers <csv>`. Restrict the
    negotiated suites to an explicit list (OpenSSL-style names, e.g.
    `ECDHE-RSA-AES256-GCM-SHA384,ECDHE-ECDSA-CHACHA20-POLY1305`).
    Unknown names fail fast at startup.
  - **Mutual TLS** — `--tls-client-ca <bundle.pem>`. When set, a client
    certificate becomes **mandatory** and is verified against the CA
    bundle; the handshake is rejected for a missing or untrusted client
    cert.
  - **Certificate hot-reload.** The cert/key files are re-read when
    their mtime advances (polled once per accept), so operators can
    rotate certificates without restarting the server or dropping live
    connections.
  - **Revocation model.** There is deliberately **no OCSP / CRL**.
    Revocation is handled by short-lived certificates rotated via
    hot-reload, narrowed by mTLS — a smaller, auditable surface that
    suits the small-binary ethos.
- **Client-side encrypted columns** (adapter-side; no server-side
  crypto and no new wire verbs). Encrypt selected STRING cells before
  they leave the process and decrypt them after read-back, so the
  database stores only opaque ciphertext:
  - **Python** — `cuttledb.crypto.FieldCipher` plus `insert_enc`,
    `insert_batch_enc`, and `get_dec`. Gated behind the optional
    `cuttledb[crypto]` extra (the `cryptography` package); the base
    install stays zero-dependency.
  - **JavaScript / TypeScript** — `FieldCipher` plus `insertEnc`,
    `insertBatchEnc`, and `getDec`, built on `node:crypto` (lazy-loaded
    so the browser bundle is unaffected).
  - **Format** — AES-256-GCM with a fresh 12-byte nonce and 16-byte
    auth tag, serialized as `enc:v1:<base64(nonce || ciphertext ||
    tag)>`. The token layout is identical across the Python and JS
    adapters, so a value encrypted in one language decrypts in the
    other. A wrong key fails the GCM tag check; non-token cells pass
    through unchanged, so mixed plaintext/ciphertext columns read fine.

### Notes

- Base adapter installs remain zero-dependency: field encryption is an
  opt-in Python extra and a lazy `node:crypto` import in JS.
- Key management is the caller's responsibility — losing the key means
  losing the data. CuttleDB never sees the key or the plaintext.

## [0.8.0] — 2026-06-02

### Added

- **Composite secondary indexes + `FINDC` verb.** A multi-column exact
  index, queried by a single wire call, for the "match on several columns
  at once" shape that a single-column `FIND` could not serve in O(1).
  Motivated by the fitment lookup in the CuttleSearch store —
  `(make, model, year) → product rows` — which previously fell back to a
  linear `BSEARCH` scan.
  - **Build:** `INDEX <hid> <tid> <c0> <c1> [c2…]` — two or more column
    ids after the table build a composite index over their values. A
    leading digit disambiguates from the single-column / `HNSW` / `BM25`
    forms (those tokens start with a letter). Both numeric and string
    columns may participate; `VEC` columns are rejected. Max 8 composite
    indexes per table.
  - **Query:** `FINDC <hid> <tid> <ncols> <c0> <c1> … <v0>\x1f<v1>…` —
    returns `[r0;r1;…]` for the rows where every `col == value`. The
    value block runs to end-of-line, `0x1f`-separated, so values may
    contain spaces and commas. O(1) average when a composite index over
    the same ordered column list exists; otherwise an O(N) scan over
    per-row composite keys (so `FINDC` is always correct, indexed or not).
  - **Canonicalization:** stored cells and query values pass through the
    same form (`%lld` for integral numbers, `%.17g` otherwise, raw bytes
    for strings), so `2018`, `2018.0`, and `2018.00` collapse to one key.
  - **Maintenance:** indexes update incrementally on `INSERT`/`DELETE`
    (swap-with-last fixup mirrors the per-column `StrIdx`). A pending
    transaction drops composite indexes conservatively (rebuild via
    `INDEX` after commit), mirroring the existing HNSW/BM25 behavior.
  - **Persistence:** snapshot format bumped to **v5** — only the column
    lists travel; the hash table is rebuilt from rows on `LOAD` (cheap,
    matches how per-column string indexes are implicit). v1/v2/v4
    snapshots still load unchanged.
  - **Python adapter:** `index(hid, tid, *cols)` now accepts a column
    list; new `findc(hid, tid, cols, values)`.
  - Tests: `adapters/python/tests/test_composite_findc.py` (10 cases —
    indexed + linear-scan parity, numeric canonicalization, incremental
    insert/delete, snapshot round-trip, VEC rejection).

- **String-column UPDATE** — `UPDRS` and `UPDATES` verbs. `UPDRS <hid>
  <tid> <rowId> <col> <val>` sets one STRING cell by physical row id;
  `UPDATES <hid> <tid> <setCol> <setVal> <predCol> <op> <thr>` sets a
  STRING column for every row matching a numeric predicate. The change
  keeps string / composite / BM25 indexes consistent and participates in
  transactions. Until now only numeric `UPDATE` existed; STRING columns
  could only be rewritten by delete + re-insert.
  - **Adapters:** `update_row_str` / `update_where_str` (Python),
    `updateRowStr` / `updateWhereStr` (JS).
  - Tests: `adapters/python/tests/test_update_str.py`.

- **GROUPBY enhancements** — `GROUPBY` gains four optional clauses:
  `BY <cols…>` (multi-column grouping → tuple keys, no group-count cap),
  `HAVING <op> <thr>` (post-aggregation filter on the aggregated value),
  `ORDER <field> <dir>` (sort by `key`/`value`, `asc`/`desc`), and
  `LIMIT <n>` (cap groups after ordering). Aggregates: count / sum / min
  / max / avg.
  - **Adapters:** `group_by(…, by=, having=, order=, limit=)` (Python),
    `groupBy(hid, tid, groupCol, opts)` (JS).
  - Tests: `adapters/python/tests/test_groupby_v08.py`.

- **Join improvements** — `JOIN` becomes a real relational join. `Op.EQ`
  equi-joins now run as a **hash join** (O(N+M), no group cap); `Op.GT` /
  `Op.LT` are non-equi nested-loop joins (rejected past ~100M comparisons
  with `join_too_large`). Outer joins via `TYPE`: `left` / `right` /
  `full` pair an unmatched row with the `-1` NULL sentinel on the other
  side. STRING-or-STRING and numeric (INT/FLOAT/DATETIME) columns join;
  VEC is rejected.
  - **Adapters:** `join(…, how=, op=)` (Python),
    `join(…, opts)` (JS) → `[(leftRow, rightRow), …]`.
  - Tests: `adapters/python/tests/test_join_v08.py`.

- **DDL inside transactions** — `CREATE` / `INDEX` / `ALTER` are now
  allowed between `BEGIN` and `COMMIT` and are reverted on `ROLLBACK`
  (previously they returned `-ERR ddl in tx`). DDL joins the already-
  transactional `INSERT` / `UPDATE` / `DELETE`, so schema and data
  changes commit or roll back as one unit — including across a WAL
  replay.
  - Tests: `adapters/python/tests/test_ddl_in_tx_v08.py`,
    `adapters/python/tests/test_wal_ddl_in_tx.py`.

- **JS adapter retrieval parity.** The JS/TS client reaches feature
  parity with the Python client on the retrieval surface: `findc`
  (composite point lookup), `lsearch` (BM25), `bsearch` (Boolean DSL),
  `search` (RRF hybrid), a multi-column composite `index`, and an
  optional `{ where }` filter on `knn` / `search`. `cuttledb.d.ts` is
  synced to the full shipped surface (it had drifted behind the already-
  shipped `groupBy` / `join` / `updateRowStr` / `updateWhereStr`).

- **CuttleSearch convenience client (optional add-on).** A thin, zero-
  dependency client for the separate read-only **CuttleSearch** BM25 HTTP
  service (default port 8787) ships inside the adapter package — for
  CuttleDB users who also run CuttleSearch and don't want to hand-roll
  `fetch` + JSON parsing. It is a **distinct import** (`cuttledb/search`
  in JS, `cuttledb.search` in Python), not a method on `CuttleDB`,
  because CuttleSearch speaks HTTP rather than the DB wire protocol.
  - `CuttleSearchClient(baseUrl).search(q, { k, mode })` →
    `{ query, k, mode, took_ms, total, hits: [{ id, score }] }`; plus
    `health()`. Errors surface as `CuttleSearchError` carrying `.status`
    and `.code` (e.g. `400` `bad_request`, `501` `not_implemented`).
  - Tests: `adapters/tests/search.mjs`,
    `adapters/python/tests/test_search_client.py`.

### Changed

- Transaction semantics: the smoke suites (Python `test_smoke.py`, JS
  `smoke.mjs`) now assert the v0.8.0 contract — DDL inside a tx is
  allowed and reverted on rollback — instead of the old `ddl in tx`
  rejection.

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
