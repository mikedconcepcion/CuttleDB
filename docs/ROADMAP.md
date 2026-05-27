# CuttleDB Roadmap

> Track for the trajectory, not the milestones. Each phase has an
> exit condition; we move when that condition is met, not when a
> calendar says so.

## Shipped — v0.6 (current)

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

## Next — v0.7

**Theme: enterprise completion + ergonomic SQL parity.**

- [ ] **Hash join** — drop the 100M cartesian cap for large equi-
      joins; keep nested-loop for small tables (faster cache
      profile).
- [ ] **Outer join + non-equi predicates** — full SQL-style join
      expressivity.
- [ ] **String-column UPDATE** — multi-token value parsing; closes
      the only remaining write-path gap.
- [ ] **DDL inside transactions** — CREATE / INDEX / ALTER undo
      semantics; lets schema migrations roll back cleanly.
- [ ] **GROUPBY enhancements** — multi-column grouping, `HAVING`,
      `ORDER BY`, hash-table variant for > 256 groups.
- [ ] **mTLS, cipher allow-lists, EC keys, cert hot-reload, OCSP
      stapling**.
- [ ] **Client-side encrypted columns** — encrypt before INSERT,
      decrypt after GET; no server-side crypto.
- [ ] **Continuous fuzz CI** — protocol fuzzer running on every PR.
- [ ] **Sanitizer-in-CI** — ASan + UBSan smoke tests on every PR.
- [ ] **Soak test** — long-running workload + memory plateau check
      every release.

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
