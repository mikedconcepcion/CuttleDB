// Cluster — client-side composition over multiple CuttleDB nodes.
//
// Mirrors cuttledb.cluster (Python). Three patterns:
//
//   1. Read replicas: writes → cluster.primary, reads → cluster.readRoundRobin()
//   2. Sharding: cluster.shardBy(key) routes to one node
//   3. Fanout writes: cluster.writeToAll(node => node.insert(...))
//
// Distribution is a topology choice, not a database feature. This class
// just wires up the pieces — you compose what your workload needs.
//
// Usage:
//
//   import { CuttleDB } from "cuttledb";
//   import { Cluster } from "cuttledb/cluster";
//
//   // Pattern 1
//   const cluster = await Cluster.withPrimaryAndReplicas({
//       primary:  { transport: "tcp", host: "primary.local", port: 7780 },
//       replicas: [
//           { transport: "tcp", host: "r1.local", port: 7780 },
//           { transport: "tcp", host: "r2.local", port: 7780 },
//       ],
//   });
//   const hid = await cluster.primary.open();
//   ...
//   const replica = cluster.readRoundRobin();
//   console.log(await replica.count(hid, tid));
//
//   // Pattern 2
//   const sharded = await Cluster.sharded([
//       { transport: "tcp", host: "s0", port: 7780 },
//       { transport: "tcp", host: "s1", port: 7780 },
//   ]);
//   const node = sharded.shardBy(userId);
//   await node.insert(hid, tid, [userId, name]);

import { CuttleDB } from "./cuttledb.js";

export class Cluster {
    /**
     * @param {object[]} opts - per-node CuttleDB constructor options
     * @param {object} [primaryOpts] - if given, designated primary for writes
     */
    constructor(opts, primaryOpts = null) {
        if (!Array.isArray(opts) || opts.length === 0) {
            throw new Error("Cluster requires at least one node");
        }
        this.nodeOpts = opts;
        this.primaryOpts = primaryOpts;
        this.nodes = [];
        this._primary = null;
        this._rrIdx = 0;
    }

    // ── Construction patterns ──────────────────────────────────────

    /** Pattern 1: writes go to primary, reads spread across primary + replicas. */
    static async withPrimaryAndReplicas({ primary, replicas, auth }) {
        const all = [primary, ...replicas].map(o => ({ auth, ...o }));
        const c = new Cluster(all, { auth, ...primary });
        await c.connect();
        return c;
    }

    /** Pattern 2: independent shards, no replication, client-side routing. */
    static async sharded(shards, { auth } = {}) {
        const c = new Cluster(shards.map(o => ({ auth, ...o })));
        await c.connect();
        return c;
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect() {
        this.nodes = this.nodeOpts.map(o => new CuttleDB(o));
        await Promise.all(this.nodes.map(n => n.connect()));
        if (this.primaryOpts) {
            const i = this.nodeOpts.findIndex(
                o => sameEndpoint(o, this.primaryOpts),
            );
            if (i >= 0) {
                this._primary = this.nodes[i];
            } else {
                this._primary = new CuttleDB(this.primaryOpts);
                await this._primary.connect();
            }
        }
    }

    close() {
        for (const n of this.nodes) {
            try { n.close(); } catch {}
        }
        if (this._primary && !this.nodes.includes(this._primary)) {
            try { this._primary.close(); } catch {}
        }
        this.nodes = [];
        this._primary = null;
    }

    // ── Access patterns ────────────────────────────────────────────

    /** Designated write target. Throws if no primary was configured. */
    get primary() {
        if (!this._primary) {
            throw new Error(
                "no primary configured — use Cluster.withPrimaryAndReplicas()",
            );
        }
        return this._primary;
    }

    /** Next node by round-robin. Use for a single read; don't hold across requests. */
    readRoundRobin() {
        const node = this.nodes[this._rrIdx % this.nodes.length];
        this._rrIdx++;
        return node;
    }

    /** Route to one node by hashing the key. Default is FNV-1a → mod node count.
     *  Pass `fn(key, n) => index` for custom routing. */
    shardBy(key, fn = null) {
        const n = this.nodes.length;
        const idx = fn ? (fn(key, n) % n) : (fnv1a(String(key)) % n);
        return this.nodes[idx];
    }

    /** Run `writeFn(node)` against every node. Throws (aggregated) if any fail. */
    async writeToAll(writeFn) {
        const settled = await Promise.allSettled(this.nodes.map(writeFn));
        const errors = [];
        const results = settled.map((s, i) => {
            if (s.status === "fulfilled") return s.value;
            errors.push(`node[${i}]: ${s.reason?.message ?? s.reason}`);
            return null;
        });
        if (errors.length) {
            throw new Error(`writeToAll partial failure: ${errors.join("; ")}`);
        }
        return results;
    }

    // ── Diagnostics ────────────────────────────────────────────────

    async info() {
        return Promise.all(this.nodes.map(n => n.info()));
    }

    get size() { return this.nodes.length; }

    [Symbol.iterator]() { return this.nodes[Symbol.iterator](); }
}

// ── Helpers ────────────────────────────────────────────────────────

function sameEndpoint(a, b) {
    if (a.transport !== b.transport) return false;
    if (a.transport === "tcp") return a.host === b.host && a.port === b.port;
    if (a.transport === "ws")  return a.url  === b.url;
    return false;
}

/** FNV-1a 32-bit hash — stable across processes (unlike JS's built-in hashes).
 *  Use this when the same key must always route to the same node. */
function fnv1a(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h;
}
