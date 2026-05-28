// CuttleDB Node.js quickstart — assumes server running on 127.0.0.1:7780.
//
// Start the server:
//   cuttledb-server --port 7780
//
// Run this:
//   node examples/node_quickstart.mjs

import { CuttleDB } from "../adapters/cuttledb.js";

const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
await db.connect();

const hid = await db.open();
const tid = await db.create(hid, "txn", [
    ["customer", 2],
    ["type",     2],
    ["amount",   0],
]);

await db.insertBatch(hid, tid, [
    ["alice", "purchase",  100],
    ["bob",   "purchase",  250],
    ["alice", "refund",    -50],
]);

console.log("rows:        ", await db.count(hid, tid));
console.log("sum amount:  ", await db.sum(hid, tid, 2));
console.log("min amount:  ", await db.min(hid, tid, 2));
console.log("max amount:  ", await db.max(hid, tid, 2));
console.log("rows > 100:  ", await db.selectGt(hid, tid, 2, 100));

await db.close();
