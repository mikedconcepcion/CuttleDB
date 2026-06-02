# CuttleDB — JavaScript / TypeScript SDK

> Embedded realtime database with vector search, BM25, hybrid retrieval,
> WAL durability, and per-table change streams. ESM only. Works in
> Node (TCP / WebSocket) and the browser (WebSocket).

```bash
npm install cuttledb
```

This package is the **client SDK**. It talks to a `cuttledb-server`
process over TCP or WebSocket. Get the server binary from
[GitHub Releases](https://github.com/mikedconcepcion/CuttleDB/releases/latest)
or pull the official Docker image:

```bash
docker run --rm -p 7780:7780 ghcr.io/mikedconcepcion/cuttledb-server:latest
```

## Quickstart

```js
import { CuttleDB, ColType } from "cuttledb";

const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
await db.connect();

const hid = await db.open();
const tid = await db.create(hid, "memory", [
    ["text",      ColType.STRING],
    ["embedding", ColType.VEC, 768],
]);

await db.insert(hid, tid, ["hello world", new Array(768).fill(0.1)]);

const hits = await db.knn(hid, tid, 1, 5, new Array(768).fill(0.1));
console.log(hits);  // [{ rowId: 0, score: 1.0 }]
```

## Browser (WebSocket)

```js
import { CuttleDB } from "cuttledb";

const db = new CuttleDB({ transport: "ws", url: "ws://localhost:7780" });
await db.connect();
// ...same API as above
```

## Five retrieval modes

| Method | What |
|---|---|
| `db.knn(hid, tid, col, k, query)` | Vector search — AVX2 cosine or HNSW |
| `db.lsearch(hid, tid, col, k, query)` | BM25 lexical search |
| `db.search(hid, tid, opts)` | RRF hybrid: vector + lexical fused server-side |
| `db.bsearch(hid, tid, k, expr)` | Boolean DSL — filters + scoring atoms |
| `db.knn(..., {where})` | Predicate-filtered KNN |

Real-time:

```js
const stream = await db.subscribe(hid, tid);
for await (const evt of stream) {
    // evt = { table, op: "INSERT"|"UPDATE"|"DELETE", rowId, ... }
}
```

## CuttleSearch client (optional)

If you also run [CuttleSearch](https://github.com/mikedconcepcion/CuttleDB) —
the read-only BM25 HTTP search service — this package ships a tiny client so
you don't have to hand-roll `fetch` + JSON parsing. It's a *separate* service
(HTTP, default port 8787), so it's a separate import, not a method on `CuttleDB`:

```js
import { CuttleSearchClient } from "cuttledb/search";

const cs = new CuttleSearchClient("http://localhost:8787");
const res = await cs.search("quarterly revenue", { k: 5 });
for (const hit of res.hits) console.log(hit.id, hit.score);
```

Errors surface as `CuttleSearchError` with `.status` and `.code` (e.g. `400`
`bad_request`, `501` `not_implemented`). Zero deps — uses native `fetch`.

## What's in the box

- ESM-only — `import`, no `require`
- Zero runtime dependencies
- TCP + WebSocket transports
- Full verb coverage: KNN, LSEARCH, SEARCH, BSEARCH, FINDC, JOIN, GROUPBY,
  transactions (incl. DDL), indexes, change feed, multi-token AUTH
- `Cluster` adapter for client-side composition across multiple
  CuttleDB instances
- `CuttleSearchClient` for the optional CuttleSearch HTTP search service

## Compatibility

- Node ≥ 18
- Browser: any with `WebSocket` + native `BigInt`
- Server protocol: matches the cuttledb-server binary in the
  same major.minor release. v0.8.x client works with v0.8.x server.

## Links

- Repository: https://github.com/mikedconcepcion/CuttleDB
- Wire protocol: https://github.com/mikedconcepcion/CuttleDB/blob/main/PROTOCOL.md
- Architecture: https://github.com/mikedconcepcion/CuttleDB/blob/main/docs/ARCHITECTURE.md
- Roadmap: https://github.com/mikedconcepcion/CuttleDB/blob/main/docs/ROADMAP.md
- Security + signature verification: https://github.com/mikedconcepcion/CuttleDB/blob/main/SECURITY.md
- Changelog: https://github.com/mikedconcepcion/CuttleDB/blob/main/CHANGELOG.md

## License

Apache-2.0 — see [`LICENSE`](https://github.com/mikedconcepcion/CuttleDB/blob/main/LICENSE).
