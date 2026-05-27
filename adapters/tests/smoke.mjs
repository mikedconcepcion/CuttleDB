// Node smoke test for the CuttleDB JS SDK.
//
// TCP + WS transports require a server on $CUTTLEDB_PORT (default 7780).
// Skipped automatically if the server isn't reachable.
//
// Run with:  node adapters/tests/smoke.mjs

import { strict as assert } from "node:assert";
import net from "node:net";
import { CuttleDB } from "../cuttledb.js";

const HOST = process.env.CUTTLEDB_HOST || "127.0.0.1";
const PORT = parseInt(process.env.CUTTLEDB_PORT || "7780", 10);

let passed = 0, failed = 0;

async function test(name, fn) {
    try {
        await fn();
        console.log("  PASS", name);
        passed++;
    } catch (e) {
        console.error("  FAIL", name, "-", e.message);
        failed++;
    }
}

function tcpReachable(host, port) {
    return new Promise(res => {
        const s = net.createConnection({ host, port });
        s.setTimeout(500);
        s.once("connect", () => { s.end(); res(true); });
        s.once("error",   () => res(false));
        s.once("timeout", () => { s.destroy(); res(false); });
    });
}

async function runTransportSuite(label, makeDb) {
    console.log("");
    console.log(label);
    const db = makeDb();
    await db.connect();

    await test("ping", async () => {
        assert.equal(await db.ping(), "PONG");
    });

    await test("hello", async () => {
        const h = await db.hello();
        assert.ok(h.startsWith("cuttledb "), `unexpected: ${h}`);
    });

    await test("info", async () => {
        const info = await db.info();
        assert.ok("version" in info);
    });

    await test("stats", async () => {
        const s = await db.stats();
        assert.ok("handles" in s);
    });

    let hid, tid;
    await test("open + create + insert", async () => {
        hid = await db.open();
        tid = await db.create(hid, "users", [["name", 2], ["salary", 0]]);
        const a = await db.insert(hid, tid, ["Alice", 100]);
        const b = await db.insert(hid, tid, ["Bob",   250]);
        assert.equal(a, 0);
        assert.equal(b, 1);
    });

    await test("count + sum + min + max", async () => {
        assert.equal(await db.count(hid, tid), 2);
        assert.equal(await db.sum(hid, tid, 1), 350);
        assert.equal(await db.min(hid, tid, 1), 100);
        assert.equal(await db.max(hid, tid, 1), 250);
    });

    await test("get", async () => {
        const row = await db.get(hid, tid, 0);
        assert.deepEqual(row, ["Alice", "100"]);
    });

    await test("insert_batch", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "v", [["x", 0]]);
        const ids = await db.insertBatch(h2, t2, [[1], [2], [3], [4], [5]]);
        assert.deepEqual(ids, [0, 1, 2, 3, 4]);
        assert.equal(await db.sum(h2, t2, 0), 15);
    });

    await test("fcountGt + selectGt", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "v", [["x", 0]]);
        await db.insertBatch(h2, t2, [[1],[2],[3],[4],[5],[6],[7],[8],[9],[10]]);
        assert.equal(await db.fcountGt(h2, t2, 0, 7), 3);
        const rows = await db.selectGt(h2, t2, 0, 8);
        const xs = rows.map(r => r[0]).sort();
        assert.deepEqual(xs, ["10", "9"]);
    });

    await test("knn (vector)", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "m", [["doc", 2], ["e", 3, 4]]);
        await db.insert(h2, t2, ["a", [1.0, 0.0, 0.0, 0.0]]);
        await db.insert(h2, t2, ["b", [0.0, 1.0, 0.0, 0.0]]);
        await db.insert(h2, t2, ["c", [0.7, 0.7, 0.0, 0.0]]);
        const hits = await db.knn(h2, t2, 1, 2, [1.0, 0.0, 0.0, 0.0]);
        assert.equal(hits.length, 2);
        assert.equal(hits[0].rowId, 0);
        assert.ok(hits[0].score > hits[1].score);
    });

    await test("delete", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "v", [["x", 0]]);
        await db.insertBatch(h2, t2, [[1],[2],[3]]);
        assert.equal(await db.delete(h2, t2, 1), true);
        assert.equal(await db.count(h2, t2), 2);
    });

    await test("log cursor", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "v", [["x", 0]]);
        await db.insert(h2, t2, [10]);
        await db.insert(h2, t2, [20]);
        const { cursor, events } = await db.log(h2, t2, 0);
        assert.ok(cursor >= 2);
        assert.ok(events.length >= 2);
    });

    await test("error propagation", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "v", [["x", 0]]);
        await db.insert(h2, t2, [1]);
        await assert.rejects(() => db.get(h2, t2, 999), /not found/);
    });

    await test("CLOSE frees handle, slot reusable", async () => {
        const h2 = await db.open();
        await db.create(h2, "v", [["x", 0]]);
        await db.closeHandle(h2);
        // Operating on a freed handle errors.
        await assert.rejects(() => db.create(h2, "ghost", [["x", 0]]), /bad/);
    });

    await test("UPDATE WHERE bulk numeric", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await db.insertBatch(h2, t2, [[10], [20], [30], [40], [50]]);
        // set v=999 WHERE v > 30
        const n = await db.updateWhere(h2, t2, 0, 999, 0, 0 /*GT*/, 30);
        assert.equal(n, 2);
        assert.equal(await db.sum(h2, t2, 0), 10 + 20 + 30 + 999 + 999);
        assert.equal(await db.count(h2, t2), 5);
    });

    await test("DELETE WHERE bulk", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await db.insertBatch(h2, t2, [[10], [20], [30], [40], [50]]);
        const n = await db.deleteWhere(h2, t2, 0, 0 /*GT*/, 25);
        assert.equal(n, 3);
        assert.equal(await db.count(h2, t2), 2);
        assert.equal(await db.sum(h2, t2, 0), 30);
    });

    await test("UPDATE/DELETE WHERE no-match returns 0", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await db.insertBatch(h2, t2, [[1], [2], [3]]);
        assert.equal(await db.updateWhere(h2, t2, 0, 99, 0, 0, 9999), 0);
        assert.equal(await db.deleteWhere(h2, t2, 0, 0, 9999), 0);
        assert.equal(await db.count(h2, t2), 3);
    });

    await test("FIND with index, post-insert maintenance", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["name", 2]]);
        await db.insertBatch(h2, t2, [["alice"], ["bob"], ["alice"]]);
        const indexed = await db.index(h2, t2, 0);
        assert.equal(indexed, 3);
        const alice = (await db.find(h2, t2, 0, "alice")).sort();
        assert.deepEqual(alice, [0, 2]);
        // post-index insert
        await db.insert(h2, t2, ["alice"]);
        assert.deepEqual((await db.find(h2, t2, 0, "alice")).sort(), [0, 2, 3]);
        // missing value
        assert.deepEqual(await db.find(h2, t2, 0, "nobody"), []);
    });

    await test("FIND index follows swap-with-last on DELETE", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["name", 2]]);
        await db.insertBatch(h2, t2, [["a"], ["b"], ["a"], ["c"]]);
        await db.index(h2, t2, 0);
        await db.delete(h2, t2, 1);  // delete bob; c (row 3) moves to row 1
        assert.deepEqual(await db.find(h2, t2, 0, "b"), []);
        assert.deepEqual(await db.find(h2, t2, 0, "c"), [1]);
        const row1 = await db.get(h2, t2, 1);
        assert.equal(row1[0], "c");
    });

    await test("tx commit persists; rollback reverts inserts", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await db.begin();
        await db.insert(h2, t2, [10]);
        await db.insert(h2, t2, [20]);
        const n = await db.commit();
        assert.equal(n, 2);
        assert.equal(await db.count(h2, t2), 2);

        await db.begin();
        await db.insert(h2, t2, [99]);
        await db.rollback();
        assert.equal(await db.count(h2, t2), 2);
    });

    await test("tx errors: nested begin, commit outside, ddl in tx", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await assert.rejects(() => db.commit(),   /not in tx/);
        await db.begin();
        await assert.rejects(() => db.begin(),    /already in tx/);
        await assert.rejects(() => db.create(h2, "x", [["v", 0]]), /ddl in tx/);
        await db.rollback();
    });

    await test("transaction() helper commits on success, rolls back on throw", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["v", 0]]);
        await db.transaction(async () => {
            await db.insert(h2, t2, [1]);
        });
        assert.equal(await db.count(h2, t2), 1);

        await assert.rejects(() => db.transaction(async () => {
            await db.insert(h2, t2, [2]);
            throw new Error("boom");
        }), /boom/);
        assert.equal(await db.count(h2, t2), 1);
    });

    await test("ALTER ADD column backfills defaults", async () => {
        const h2 = await db.open();
        const t2 = await db.create(h2, "t", [["name", 2]]);
        await db.insert(h2, t2, ["alice"]);
        const newIdx = await db.alterAdd(h2, t2, "salary", 0);
        assert.equal(newIdx, 1);
        const row = await db.get(h2, t2, 0);
        assert.deepEqual(row, ["alice", "0"]);   // backfilled
    });

    db.close();
}

async function runAuthSuite(host, port, token) {
    console.log("");
    console.log(`── AUTH transport (${host}:${port}, token=${token}) ───`);

    await test("ping allowed without auth", async () => {
        const db = new CuttleDB({ transport: "tcp", host, port });
        await db.connect();
        assert.equal(await db.ping(), "PONG");
        db.close();
    });

    await test("open rejected without auth", async () => {
        const db = new CuttleDB({ transport: "tcp", host, port });
        await db.connect();
        await assert.rejects(() => db.open(), /auth required/);
        db.close();
    });

    await test("connect with correct token succeeds", async () => {
        const db = new CuttleDB({ transport: "tcp", host, port, auth: token });
        await db.connect();
        const hid = await db.open();
        assert.ok(hid >= 0);
        db.close();
    });

    await test("connect with wrong token throws", async () => {
        const db = new CuttleDB({ transport: "tcp", host, port, auth: "wrong" });
        await assert.rejects(() => db.connect(), /auth failed/);
    });

    await test("HELLO advertises auth_required", async () => {
        const db = new CuttleDB({ transport: "tcp", host, port });
        await db.connect();
        assert.ok((await db.hello()).includes("auth_required"));
        db.close();
    });
}

(async () => {
    if (await tcpReachable(HOST, PORT)) {
        await runTransportSuite(
            `── TCP transport (${HOST}:${PORT}) ───────────────`,
            () => new CuttleDB({ transport: "tcp", host: HOST, port: PORT }),
        );
    } else {
        console.log("");
        console.log(`── TCP transport — SKIPPED (server not at ${HOST}:${PORT}) ──`);
    }

    if (await tcpReachable(HOST, PORT)) {
        await runTransportSuite(
            `── WS transport  (ws://${HOST}:${PORT}) ─────────────`,
            () => new CuttleDB({ transport: "ws", url: `ws://${HOST}:${PORT}` }),
        );
    } else {
        console.log("");
        console.log(`── WS transport — SKIPPED (server not at ${HOST}:${PORT}) ──`);
    }

    // AUTH suite — only when env vars set.
    const authPort = process.env.CUTTLEDB_AUTH_PORT;
    const authTok  = process.env.CUTTLEDB_AUTH_TOKEN;
    if (authPort && authTok) {
        await runAuthSuite(HOST, parseInt(authPort, 10), authTok);
    } else {
        console.log("");
        console.log("── AUTH suite — SKIPPED (set CUTTLEDB_AUTH_PORT + CUTTLEDB_AUTH_TOKEN to run) ──");
    }

    console.log("");
    console.log(`Result: ${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();
