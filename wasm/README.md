# CuttleDB — embeddable WASM

Run a **full CuttleDB instance entirely in-process** — no server, no socket,
no install. The engine is compiled to WebAssembly; you drive it with the same
JavaScript SDK you'd use over TCP, just with an in-process transport.

Works in **Node and the browser** from the same files.

## Do I need to know WebAssembly? No.

You never touch WebAssembly directly. The `.wasm` file is just the database
engine in a portable form; the `.mjs` files are plain JavaScript that load it
for you. You `import` one function (`connect`) and call normal methods
(`open`, `create`, `insert`, `lsearch`, …). If you can call a JavaScript
function, you can use this. Nothing to compile, nothing to install.

## Installed from npm? Skip the copying

If you `npm i cuttledb` (>= 0.8.1) the kit ships inside the package — just:

```js
import { connect } from "cuttledb/wasm";
```

The rest of this page is for the other case: **vendoring the files straight
from this repo** (no npm), or serving them as static files in the browser.

## What you need (read this first)

The full-database kit is **four files that must stay together**:

```
your-project/
├── wasm/
│   ├── cuttledb-engine.wasm     ← the engine (~350 KB)
│   ├── cuttledb-engine.js       ← loader glue
│   └── cuttledb.mjs             ← the part you import
└── adapters/
    └── cuttledb.js              ← the SDK (cuttledb.mjs imports this)
```

`wasm/cuttledb.mjs` does `import { CuttleDB } from "../adapters/cuttledb.js"`,
so **copy both the `wasm/` folder and `adapters/cuttledb.js`** out of this repo,
keeping them side by side. If you copy only `wasm/`, the import breaks.

> Just want search and nothing else? The **CuttleSearch** kit is a single
> self-contained file (no `adapters/` needed) — see the CuttleSearch repo.

## Files

| File | What it is |
|------|------------|
| `cuttledb-engine.wasm` | The CuttleDB engine compiled to WebAssembly (~350 KB). |
| `cuttledb-engine.js` | Emscripten loader glue (ES module). |
| `cuttledb.mjs` | The embed layer — a `WasmTransport` + `connect()` that wires the engine into the standard SDK. |
| `demo.mjs` | Node demo: build a table, index it, search it — all in-process. |

## Quick start (Node)

From inside this folder, just run the demo — no setup:

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

## Your own code

```js
import { connect } from "./cuttledb.mjs";                  // relative path to the file

const db  = await connect();                               // boot in-process
const hid = await db.open();
const tid = await db.create(hid, "docs", [["body", 2]]);   // 2 = string
await db.insert(hid, tid, ["a fast red fox leaps across the river"]);
const hits = await db.lsearch(hid, tid, 0, 5, "fox");      // [{ rowId, score }]
await db.closeHandle(hid);
db.close();
```

The import is a **relative path to the `cuttledb.mjs` file** (`./cuttledb.mjs`,
`../wasm/cuttledb.mjs`, etc. — wherever you put it). It is not an npm package
name; you point at the file you copied.

`connect()` boots the engine in-process and returns an ordinary CuttleDB SDK
instance — **every verb the TCP/WebSocket client exposes works unchanged**
(`open`, `create`, `insert`, `find`, `findc`, `index`, `lsearch`, `bsearch`,
`search`, transactions, …). See [`../adapters/cuttledb.js`](../adapters/cuttledb.js)
for the full method surface.

Load a pre-built snapshot instead of building from scratch:

```js
const hid  = await db.loadSnapshot(snapshotBytes);         // Uint8Array
const hits = await db.lsearch(hid, 0, 0, 5, "fox river");
```

`connect(opts)` forwards `opts` to the Emscripten module factory (e.g.
`{ locateFile }` to host the `.wasm` somewhere non-default).

## In the browser

There is no build step. **Serve this folder over HTTP** (browsers won't fetch
`.wasm` from `file://`) so `cuttledb-engine.wasm` sits next to the loader, then:

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

Any static file server works, e.g. `npx serve .` or `python -m http.server`,
then open the page it prints.

## How it works

The engine exposes one in-process entry point, `cuttledb_exec_line`, that runs a
single wire-protocol line and writes the response into a caller-supplied buffer
— the same grammar the TCP server speaks ([PROTOCOL.md](../PROTOCOL.md)), minus
the socket. `cuttledb.mjs` implements a `WasmTransport` around it and injects
that into the standard `CuttleDB` class, so the embedded and server code paths
share one engine, one wire grammar, and one SDK.

This is the same engine artifact CuttleSearch embeds for in-process search —
CuttleDB just exposes the full database surface on top of it.
