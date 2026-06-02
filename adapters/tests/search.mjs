// Node test for the CuttleSearchClient — the convenience client for the
// CuttleSearch read-only HTTP search API (default port 8787).
//
// CuttleSearch is a separate service from the CuttleDB line protocol. This
// test hits a live CuttleSearch server; it skips if one isn't reachable at
// $CUTTLESEARCH_URL (default http://localhost:8787).
//
// Run with:  node adapters/tests/search.mjs

import { strict as assert } from "node:assert";
import { CuttleSearchClient, CuttleSearchError } from "../search.js";

const BASE = process.env.CUTTLESEARCH_URL || "http://localhost:8787";

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

async function serverUp() {
    try {
        const r = await fetch(`${BASE}/health`, { signal: AbortSignal.timeout(500) });
        return r.ok;
    } catch {
        return false;
    }
}

(async () => {
    if (!(await serverUp())) {
        console.log(`SKIP: CuttleSearch not reachable at ${BASE}`);
        process.exit(0);
    }

    console.log("CuttleSearchClient");
    const cs = new CuttleSearchClient(BASE);

    await test("empty query is a client-side error (no round-trip)", async () => {
        await assert.rejects(() => cs.search(""), (e) => {
            assert.ok(e instanceof CuttleSearchError);
            assert.equal(e.status, null);
            assert.equal(e.code, null);
            return true;
        });
    });

    await test("health", async () => {
        const h = await cs.health();
        assert.equal(h.status, "ok");
        assert.equal(h.service, "cuttlesearch");
        assert.ok("version" in h);
    });

    await test("search shape + sorted hits", async () => {
        const res = await cs.search("the", { k: 3 });
        for (const key of ["query", "k", "mode", "took_ms", "total", "hits"]) {
            assert.ok(key in res, `missing key: ${key}`);
        }
        assert.equal(res.mode, "bm25");
        assert.ok(Array.isArray(res.hits));
        assert.equal(res.total, res.hits.length);
        for (const hit of res.hits) {
            assert.equal(typeof hit.id, "number");
            assert.equal(typeof hit.score, "number");
        }
        const scores = res.hits.map(h => h.score);
        assert.deepEqual(scores, [...scores].sort((a, b) => b - a));
    });

    await test("k is honored", async () => {
        const res = await cs.search("the", { k: 1 });
        assert.ok(res.hits.length <= 1);
        assert.equal(res.k, 1);
    });

    await test("unimplemented mode → 501", async () => {
        await assert.rejects(() => cs.search("x", { mode: "vector" }), (e) => {
            assert.equal(e.status, 501);
            assert.equal(e.code, "not_implemented");
            return true;
        });
    });

    await test("bad mode → 400", async () => {
        await assert.rejects(() => cs.search("x", { mode: "bogus" }), (e) => {
            assert.equal(e.status, 400);
            assert.equal(e.code, "bad_request");
            return true;
        });
    });

    await test("trailing slash in base URL is normalized", async () => {
        const c2 = new CuttleSearchClient(BASE.replace(/\/+$/, "") + "///");
        assert.equal((await c2.health()).status, "ok");
    });

    console.log("");
    console.log(`${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();
