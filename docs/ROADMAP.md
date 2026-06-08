# CuttleDB Roadmap

> Track for the trajectory, not the milestones. Each phase has an
> exit condition; we move when that condition is met, not when a
> calendar says so.

## Shipped — v0.6

**Theme: production-orientation hybrid retrieval substrate.**

Five-mode retrieval — KNN (vector), LSEARCH (BM25), SEARCH (RRF
fusion), BSEARCH (Boolean DSL), filtered KNN (`WHERE`) — in one
self-contained binary. Plus a full write path, durability,
transactions, and the enterprise-hardening Tier 1 set.

- [x] **Hybrid retrieval** — KNN + LSEARCH + SEARCH + BSEARCH; AVX2
      SIMD; HNSW ANN auto-routes for KNN at scale.
- [x] **HNSW ANN index** — full lifecycle (build / INSERT / DELETE /
      SAVE / LOAD). 12.7× faster than brute-force at 100K × 128.
- [x] **WAL durability** — `--wal-dir` opt-in; CRC32 frames;
      checkpoint at threshold. Replay on startup.
- [x] **Transactions** — `BEGIN` / `COMMIT` / `ROLLBACK` per-
      connection; atomic WAL flush via `_TXB` / `_TXC` markers;
      DELETE-in-tx.
- [x] **Real-time push** — `SUB` / `UNSUB` / `LOG` per-table change
      feed.
- [x] **Aggregations** — COUNT/SUM O(1); MIN/MAX/FCOUNT SIMD;
      GROUPBY.
- [x] **Joins** — 2-way inner equi-join `JOIN`.
- [x] **Datetime column type**.
- [x] **Multi-token auth** — `TOKEN ADD / LIST / REVOKE`; root-gated
      admin; per-token IDs in audit log.
- [x] **Audit log** — `--audit-dir <path>`; NDJSON per UTC day; one
      line per dispatched command with verb, token_id, fd, ts, ok.
- [x] **TLS** — `--tls-cert` / `--tls-key` flags; RSA cert + server-
      side handshake; `CUTTLEDB_WITH_TLS=1` build flag.
- [x] **WebSocket transport** — same port as TCP; auto-detects HTTP
      upgrade.
- [x] **Python + JS adapters** — full verb coverage; pipelining;
      SUB/poll.
- [x] **Distribution-by-composition** — `Cluster` adapter,
      `cuttledb.replicate` companion, `docs/DEPLOYMENT.md` reference
      architectures.
- [x] **`--max-conn N` cap** — total concurrent connections; basic
      connection-flood defense.
- [x] **HTTP `/health` endpoint** — k8s liveness / readiness probe
      target. PING-equivalent over HTTP, pre-auth, on the same port
      as WebSocket.
- [x] **Prometheus `/metrics` endpoint** — counters and gauges for
      verbs, connections, errors, uptime. Same port.
- [x] **Structured slow-query log** — `--slow-log-ms N
      --slow-log-file <path>`; NDJSON, day-rotated.

**Exit condition for v0.6:** ✅ Single self-contained binary, under
1 MB. Pass-rate on the substrate test suite ≥ 95%. Independent
first-time user can do `cuttledb-server --port 7780` and run a
working CRUD + KNN + SUB session in under 60 seconds.

## Shipped — v0.7

**Theme: stability + correctness + test hardening.**

- [x] **Canonical per-column row emitter** — GET and SELGT share one
      implementation; adding a column type is a one-place change.
- [x] **Safe bounded wire-buffer append** (`safe_appendf`) — auto-
      clamps on truncation; replaces the unsafe `snprintf` accumulate
      pattern at all call sites.
- [x] **Wire-format escape contract** — documented in `PROTOCOL.md`,
      enforced by cross-adapter contract tests.
- [x] **Official Docker image** — distroless, ~25 MB, non-root UID
      65532; build verifies the binary's sigstore signature.
- [x] **Soak harness** — mixed-workload memory-plateau check.
- [x] **Signal-handling tests** — clean shutdown on SIGTERM / SIGINT
      / `CTRL_BREAK_EVENT`.
- [x] **Sanitizer-in-CI** — server-side ASan + UBSan on every push/PR.
- [x] **Continuous fuzz CI** — libFuzzer harness for the WAL replay
      parser; scheduled daily + manual.

## Shipped — v0.8

**Theme: relational completion + retrieval parity.**

- [x] **Composite secondary indexes + `FINDC`** — multi-column exact
      lookup in one wire call, O(1) average. Snapshot format → v5
      (v1/v2/v4 still load).
- [x] **String-column UPDATE** — `UPDRS` (by row id) / `UPDATES` (by
      numeric predicate); index- and transaction-consistent. Closes
      the last write-path gap.
- [x] **GROUPBY enhancements** — multi-column `BY` (tuple keys, no
      group cap), `HAVING`, `ORDER` (key/value, asc/desc), `LIMIT`.
- [x] **Join improvements** — hash equi-join (O(N+M), no cap);
      non-equi `GT`/`LT`; `left`/`right`/`full` outer joins (-1 NULL
      sentinel).
- [x] **DDL inside transactions** — `CREATE` / `INDEX` / `ALTER`
      commit or roll back atomically with row mutations, across WAL
      replay.
- [x] **JS adapter retrieval parity** — `findc`, `lsearch`, `bsearch`,
      `search` (RRF), composite `index`, `{where}`-filtered `knn` /
      `search` — matching Python.
- [x] **CuttleSearch convenience client (optional)** — zero-dep
      `CuttleSearchClient` in the `cuttledb` package (`cuttledb/search`
      JS, `cuttledb.search` Python) for the separate read-only BM25
      HTTP service.

## Shipped — v0.9 (current)

**Theme: security depth + at-rest privacy.**

- [x] **TLS hardening** (opt-in `CUTTLEDB_WITH_TLS=1` build) —
      EC (P-256 / P-384) keys; cipher allow-list (`--tls-ciphers`);
      mutual TLS (`--tls-client-ca`: client cert mandatory + verified
      against the CA bundle); certificate hot-reload (mtime-polled, no
      restart). **No OCSP / CRL** — revocation is handled by short-lived
      certs rotated via hot-reload and narrowed by mTLS.
- [x] **Client-side encrypted columns** — adapter-side AES-256-GCM:
      encrypt before INSERT, decrypt after GET; the server stores
      ciphertext only, no server-side crypto and no new wire verb.
      Python (`FieldCipher` + `insert_enc` / `get_dec`, optional
      `cuttledb[crypto]` extra) and JS (`FieldCipher` + `insertEnc` /
      `getDec`, `node:crypto`). Language-neutral `enc:v1:` token —
      cross-language decrypt verified.

## Then — v1.0

**Theme: distribution + graph + temporal.**

- [ ] **Graph types + traversal** (`MATCH` verb).
- [ ] **Native CRDT / distributed sync** — two CuttleDBs sync state
      automatically over the existing LOG/SUB primitives.
- [ ] **Cluster-of-one peer pairing** — local-first / Gun.js
      lineage.
- [ ] **`SELECT AS OF <ts>` temporal queries** — substrate ready;
      surface lands here.
- [ ] **Predicate-filtered `SUB`** — substrate ready; surface lands
      here.
- [ ] **GPU HNSW index** — substrate present; index migrates from
      CPU to GPU.
- [ ] **Reproducible-build attestation** — bit-identical builds
      across runners + provenance attestation.

**Exit condition for v1.0:** distributed sync works without operator
intervention. Graph traversal joins relational queries cleanly. The
substrate stays under 1 MB.
