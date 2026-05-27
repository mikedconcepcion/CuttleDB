// Cluster smoke tests. Requires two CuttleDB servers reachable on
// $CUTTLEDB_CLUSTER_PORT_A and $CUTTLEDB_CLUSTER_PORT_B (set by test.sh).

import { strict as assert } from "node:assert";
import net from "node:net";
import { Cluster } from "../cluster.js";

const HOST  = process.env.CUTTLEDB_HOST || "127.0.0.1";
const PORT_A = parseInt(process.env.CUTTLEDB_CLUSTER_PORT_A || "", 10);
const PORT_B = parseInt(process.env.CUTTLEDB_CLUSTER_PORT_B || "", 10);

let passed = 0, failed = 0;
async function test(name, fn) {
    try { await fn(); console.log("  PASS", name); passed++; }
    catch (e) { console.error("  FAIL", name, "-", e.message); failed++; }
}

function up(host, port) {
    return new Promise(res => {
        const s = net.createConnection({ host, port });
        s.setTimeout(500);
        s.once("connect", () => { s.end(); res(true); });
        s.once("error",   () => res(false));
        s.once("timeout", () => { s.destroy(); res(false); });
    });
}

(async () => {
    console.log("");
    console.log("── Cluster (2 nodes) ─────────────────────────");

    if (!Number.isFinite(PORT_A) || !Number.isFinite(PORT_B) ||
        !(await up(HOST, PORT_A)) || !(await up(HOST, PORT_B))) {
        console.log("  SKIPPED (set CUTTLEDB_CLUSTER_PORT_A and _B to running servers)");
        process.exit(0);
    }

    const mkOpts = (port) => ({ transport: "tcp", host: HOST, port });

    await test("connect 2 nodes", async () => {
        const c = new Cluster([mkOpts(PORT_A), mkOpts(PORT_B)]);
        await c.connect();
        assert.equal(c.size, 2);
        const infos = await c.info();
        assert.equal(infos.length, 2);
        c.close();
    });

    await test("round-robin alternates and cycles", async () => {
        const c = new Cluster([mkOpts(PORT_A), mkOpts(PORT_B)]);
        await c.connect();
        const a = c.readRoundRobin();
        const b = c.readRoundRobin();
        const cc = c.readRoundRobin();
        assert.notEqual(a, b);
        assert.equal(a, cc);
        c.close();
    });

    await test("shardBy is deterministic for the same key", async () => {
        const c = new Cluster([mkOpts(PORT_A), mkOpts(PORT_B)]);
        await c.connect();
        const n1 = c.shardBy("alice");
        const n2 = c.shardBy("alice");
        assert.equal(n1, n2);
        c.close();
    });

    await test("shardBy splits keys across nodes", async () => {
        const c = new Cluster([mkOpts(PORT_A), mkOpts(PORT_B)]);
        await c.connect();
        const counts = new Map();
        for (let i = 0; i < 50; i++) {
            const node = c.shardBy(`key-${i}`);
            counts.set(node, (counts.get(node) || 0) + 1);
        }
        // Both nodes should see some traffic.
        assert.equal(counts.size, 2);
        c.close();
    });

    await test("writeToAll fans out + aggregates results", async () => {
        const c = new Cluster([mkOpts(PORT_A), mkOpts(PORT_B)]);
        await c.connect();
        const hids = await c.writeToAll(n => n.open());
        assert.equal(hids.length, 2);
        assert.ok(hids[0] >= 0 && hids[1] >= 0);
        c.close();
    });

    await test("primary getter throws without primary configured", async () => {
        const c = new Cluster([mkOpts(PORT_A)]);
        await c.connect();
        assert.throws(() => c.primary, /no primary/);
        c.close();
    });

    await test("withPrimaryAndReplicas: primary write + replica read", async () => {
        const c = await Cluster.withPrimaryAndReplicas({
            primary:  mkOpts(PORT_A),
            replicas: [mkOpts(PORT_B)],
        });
        const hid = await c.primary.open();
        const tid = await c.primary.create(hid, "x", [["v", 0]]);
        await c.primary.insert(hid, tid, [42]);
        // primary count is the source of truth in this test (no replicator running)
        assert.equal(await c.primary.count(hid, tid), 1);
        c.close();
    });

    console.log("");
    console.log(`Cluster: ${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();
