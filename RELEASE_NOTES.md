# Release Notes

## v0.9.0 — 2026-06-06

Security-depth + at-rest-privacy release. The opt-in TLS build grows from
a single RSA-cert handshake into a hardened transport, and a new
client-side encrypted-columns story lets you keep ciphertext in the
server without the server ever seeing a key or a plaintext.

### TLS hardening (opt-in `CUTTLEDB_WITH_TLS=1` build)

The default build still links **no** TLS and stays zero-dependency; these
land only when you compile with `CUTTLEDB_WITH_TLS=1`.

- **EC private keys.** `--tls-key` now accepts EC (P-256 / P-384) keys in
  addition to RSA — the PEM loader auto-detects the key type.
- **Cipher allow-list.** `--tls-ciphers <csv>` restricts the negotiated
  suites to an explicit OpenSSL-style list (e.g.
  `ECDHE-RSA-AES256-GCM-SHA384`); an unknown name fails fast at startup.
- **Mutual TLS.** `--tls-client-ca <bundle>` makes a client certificate
  mandatory and verifies it against the supplied CA bundle — connections
  without a trusted client cert are refused at the handshake.
- **Certificate hot-reload.** The cert/key are re-read when their file
  mtime changes, with no restart and no dropped connections — rotate
  in place.
- **No OCSP / CRL.** Revocation is handled by short-lived certificates
  rotated through hot-reload and narrowed by mTLS, not by an online
  revocation check.

### Client-side encrypted columns

- **Adapter-side AES-256-GCM.** Encrypt a field before `INSERT` and
  decrypt it after `GET` — the server stores ciphertext only, does no
  crypto, and gains no new wire verb. Python (`FieldCipher` +
  `insert_enc` / `get_dec`, optional `cuttledb[crypto]` extra) and JS
  (`FieldCipher` + `insertEnc` / `getDec`, built on `node:crypto`).
- **Language-neutral token.** Encrypted cells carry an `enc:v1:` prefix;
  the format is byte-identical across Python and JS, so a value written
  by one adapter decrypts in the other. The base adapters stay
  zero-dependency — only encrypted columns pull in the optional crypto
  library on Python.

### Compatibility

- No wire-protocol change. Encrypted columns are ordinary STRING cells
  on the wire; TLS hardening is transport-only.
- The base Python and JS adapters remain zero-dependency; `cryptography`
  is an opt-in extra (`pip install cuttledb[crypto]`).
- Adapters published at `0.9.0` (npm `cuttledb`, PyPI `cuttledb`).

## v0.8.0 — 2026-06-02

Relational + retrieval release. Four engine features that were on the
roadmap land together, the JS/TS client catches up to the Python one on
the full retrieval surface, and the adapter package gains an optional
client for the CuttleSearch HTTP search service.

### Highlights

- **Composite secondary indexes + `FINDC`.** Multi-column exact lookup
  in one wire call — `(make, model, year) → rows` in O(1) average,
  instead of a linear `BSEARCH` scan. `INDEX <hid> <tid> <c0> <c1> …`
  builds it; `FINDC` queries it (always correct, indexed or not).
- **String-column UPDATE** (`UPDRS` / `UPDATES`). Set a STRING cell by
  row id, or set a STRING column across rows matching a numeric
  predicate — transactional, index-consistent. No more delete +
  re-insert to edit text.
- **GROUPBY enhancements.** `BY` (multi-column / tuple keys), `HAVING`,
  `ORDER` (key/value, asc/desc), `LIMIT` — `GROUPBY` is now a real
  grouped-aggregate, not just count-per-key.
- **Join improvements.** `Op.EQ` runs as a hash join (O(N+M), no cap);
  `Op.GT` / `Op.LT` non-equi joins; `left` / `right` / `full` outer
  joins with a `-1` NULL sentinel.
- **DDL inside transactions.** `CREATE` / `INDEX` / `ALTER` now commit or
  roll back atomically alongside `INSERT` / `UPDATE` / `DELETE`, across
  WAL replay.

### Adapters

- **JS/TS retrieval parity.** The JS client gains `findc`, `lsearch`
  (BM25), `bsearch` (Boolean DSL), `search` (RRF hybrid), composite
  `index`, and `{ where }`-filtered `knn` / `search` — matching Python.
  `cuttledb.d.ts` re-synced to the full shipped surface.
- **CuttleSearch client (optional).** If you also run CuttleSearch — the
  separate read-only BM25 HTTP service — the adapter package now ships a
  thin, zero-dep client so you can call it in one line instead of
  hand-rolling `fetch` + JSON:

  ```js
  import { CuttleSearchClient } from "cuttledb/search";
  const res = await new CuttleSearchClient("http://localhost:8787")
                      .search("quarterly revenue", { k: 5 });
  // res.hits → [{ id, score }, …]
  ```

  ```python
  from cuttledb.search import CuttleSearchClient
  res = CuttleSearchClient("http://localhost:8787").search("quarterly revenue", k=5)
  ```

  It is a separate import — not a method on `CuttleDB` — because
  CuttleSearch speaks HTTP, not the CuttleDB wire protocol. Errors
  surface as `CuttleSearchError` with `.status` and `.code`.

### Compatibility

- Wire protocol additions are backward-compatible: new verbs and new
  optional clauses on existing verbs; no existing verb changed shape.
- Snapshot format bumped to **v5** for composite indexes; v1/v2/v4
  snapshots still load unchanged.
- Adapters published at `0.8.0` (npm `cuttledb`, PyPI `cuttledb`).

## v0.7.0 — 2026-05-28

Stability + correctness release. Three structural refactors eliminate
the bug *classes* surfaced in v0.6.0 testing, plus an official
Docker image and a wire-contract test suite that pins escape rules
across every adapter.

### Highlights

- **One canonical per-column row emitter** (`emit_row_columns`). GET
  and SELGT share one implementation; adding a new column type is
  now a one-place change. Eliminates the structural condition behind
  the v0.6.0 SELGT-crashes-on-VEC-tables bug.
- **Safe bounded wire-buffer append helper** (`safe_appendf`).
  Replaces the unsafe `send_n += snprintf(...)` pattern at all 13
  call sites in the server. Auto-clamps on truncation so
  `snprintf`'s desired-length return can't overflow the buffer.
- **Wire-format escape contract** — documented in `PROTOCOL.md`,
  enforced by `test_wire_contract.py` (Python) + `wire_contract.mjs`
  (JS). Single canonical list of escape characters; encoder/decoder
  drift across adapters is detected on first divergence.
- **Official Docker image** at `ghcr.io/mikedconcepcion/cuttledb-server`.
  Distroless runtime, ~25 MB, runs as non-root UID 65532. Build
  verifies the binary's sigstore signature before assembling the
  image. `docker run --rm -p 7780:7780 ghcr.io/mikedconcepcion/cuttledb-server:latest`.

### Fixed

- SELGT crashed the connection on any table containing a VEC column.
- `wire_str_encode` didn't escape `;`; STRING values containing a
  semicolon would split rows in SELGT output.
- Python `_parse_rowlist` and JS `parseRowlist` / `get` used naive
  `","` splits, mis-parsing any row whose STRING column contained
  `,` or `;`.
- JS `encodeValue` never escaped outbound STRING values — inserting
  a string containing `,` `\` CR LF from JS silently misaligned the
  column count. Pre-existing latent bug from v0.6.0; surfaced by the
  new contract test.
- SELGT row emitter could overflow `send_buf` on very-high-dim VEC
  rows or escape-expanded long-string rows.

### Added (testing + hardening)

- Soak harness (`test_soak.py` + `.github/workflows/soak.yml`).
  Mixed-workload memory-plateau check; workflow-dispatched with
  configurable duration. Surfaced the SELGT/VEC crash on its first
  real run.
- Signal-handling tests (`test_signals.py`) — clean shutdown on
  SIGTERM / SIGINT (POSIX) and `CTRL_BREAK_EVENT` (Windows).
- Sanitizer-in-CI (server-side ASan + UBSan run on every push and
  PR).
- Continuous fuzz CI (libFuzzer harness for the WAL replay parser;
  scheduled daily + manual; corpus cached between runs).

### Engine roadmap

Items still queued for future releases pending real-user signal:
hash join, outer / non-equi joins, mTLS hardening, DDL inside
transactions, multi-column `GROUPBY` / `HAVING`, client-side
encrypted columns. Tracked in `docs/ROADMAP.md`.

### Verifying a release binary

Every binary in the GitHub Release ships with a `.cosign.bundle`
file containing the signature, signing certificate, and Rekor
transparency-log inclusion proof. See `SECURITY.md` for the recipe.

[v0.7.0]: https://github.com/mikedconcepcion/CuttleDB/releases/tag/v0.7.0

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
