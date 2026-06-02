// CuttleDB WASM demo — a full database, in-process, no server.
//
//   node demo.mjs
//   node demo.mjs "your query here"
//
// Boots the engine compiled to WebAssembly, builds a tiny table from scratch,
// indexes it for BM25, and runs a ranked search — all in one process, no
// socket, no install. The same SDK you'd use over TCP works unchanged here.

import { connect } from "./cuttledb.mjs";

const query = process.argv[2] ?? "fox river";

const DOCS = [
  "the quick brown fox jumps over the lazy dog",
  "a fast red fox leaps across the river",
  "lazy dogs sleep all day in the warm sun",
  "quantum computing changes cryptography forever",
  "the river flows fast under the old stone bridge",
];

const db = await connect();

const hid = await db.open();
const tid = await db.create(hid, "docs", [["body", 2]]); // col 0 = string `body`
for (const text of DOCS) await db.insert(hid, tid, [text]);

console.log(`engine: ${JSON.stringify(await db.info())}`);
console.log(`rows:   ${DOCS.length}`);
console.log(`query:  "${query}"\n`);

const hits = await db.lsearch(hid, tid, 0, 5, query); // BM25 over col 0
if (hits.length === 0) {
  console.log("(no matches)");
} else {
  for (const [rank, hit] of hits.entries()) {
    console.log(
      `  ${rank + 1}. row ${hit.rowId}  score ${hit.score.toFixed(4)}  ${DOCS[hit.rowId]}`
    );
  }
}

await db.closeHandle(hid);
db.close();
