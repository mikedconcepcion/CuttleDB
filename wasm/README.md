# CuttleDB — embeddable WASM

Run a **full CuttleDB instance entirely in-process** — no server, no socket,
no install. The engine is compiled to WebAssembly; you drive it with the same
JavaScript SDK you'd use over TCP, just with an in-process transport.

Works in **Node and the browser** from the same files.

## Files

| File | What it is |
|------|------------|
| `cuttledb-engine.wasm` | The CuttleDB engine compiled to WebAssembly (~350 KB). |
| `cuttledb-engine.js` | Emscripten loader glue (ES module). |
| `cuttledb.mjs` | The embed layer — a `WasmTransport` + `connect()` that wires the engine into the standard SDK. |
| `demo.mjs` | Node demo: build a table, index it, search it — all in-process. |

## Quick start (Node)

```bash
node demo.mjs                       # build a tiny table and BM25-search it
node demo.mjs "quantum cryptography"
```

```
query:  "fox river"

  1. row 1  score 1.7509  a fast red fox leaps across the river
  2. row 0  score 0.8288  the quick brown fox jumps over the lazy dog
  3. row 4  score 0.8288  the river flows fast under the old stone bridge
```

## API

`connect()` boots the engine in-process and returns an ordinary CuttleDB SDK
instance — **every verb the TCP/WebSocket client exposes works unchanged**
(`open`, `create`, `insert`, `find`, `findc`, `index`, `lsearch`, `bsearch`,
`search`, transactions, …). See [`../adapters/cuttledb.js`](../adapters/cuttledb.js)
for the full method surface.

```js
import { connect } from "./cuttledb.mjs";

const db  = await connect();                               // boot in-process
const hid = await db.open();
const tid = await db.create(hid, "docs", [["body", 2]]);   // 2 = string
await db.insert(hid, tid, ["a fast red fox leaps across the river"]);
const hits = await db.lsearch(hid, tid, 0, 5, "fox");      // [{ rowId, score }]
await db.closeHandle(hid);
db.close();
```

Load a pre-built snapshot instead of building from scratch:

```js
const hid  = await db.loadSnapshot(snapshotBytes);         // Uint8Array
const hits = await db.lsearch(hid, 0, 0, 5, "fox river");
```

`connect(opts)` forwards `opts` to the Emscripten module factory (e.g.
`{ locateFile }` to host the `.wasm` somewhere non-default).

## In the browser

Serve this folder over HTTP so `cuttledb-engine.wasm` is fetchable next to the
loader, then:

```html
<script type="module">
  import { connect } from "./cuttledb.mjs";
  const db  = await connect();
  const hid = await db.open();
  const tid = await db.create(hid, "docs", [["body", 2]]);
  await db.insert(hid, tid, ["the river flows fast under the old stone bridge"]);
  console.log(await db.lsearch(hid, tid, 0, 5, "river"));
</script>
```

## How it works

The engine exposes one in-process entry point, `cuttledb_exec_line`, that runs a
single wire-protocol line and writes the response into a caller-supplied buffer
— the same grammar the TCP server speaks ([PROTOCOL.md](../PROTOCOL.md)), minus
the socket. `cuttledb.mjs` implements a `WasmTransport` around it and injects
that into the standard `CuttleDB` class, so the embedded and server code paths
share one engine, one wire grammar, and one SDK.

This is the same engine artifact CuttleSearch embeds for in-process search —
CuttleDB just exposes the full database surface on top of it.
