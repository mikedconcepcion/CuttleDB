// JS wire-format escape contract — mirror of test_wire_contract.py.
//
// Same canonical character list as the Python suite. If we add a new
// escape character to the wire format, it lands in BOTH lists or someone
// shipped a half-fix.
//
// Run:  node adapters/tests/wire_contract.mjs

import { strict as assert } from "node:assert";
import net from "node:net";
import { CuttleDB } from "../cuttledb.js";

const HOST = process.env.CUTTLEDB_HOST || "127.0.0.1";
const PORT = parseInt(process.env.CUTTLEDB_PORT || "7780", 10);

// Mirror of WIRE_CONTRACT_STRINGS in test_wire_contract.py.
const WIRE_CONTRACT_STRINGS = [
    ["plain-ascii",         "alice"],
    ["contains-comma",      "bob,jr"],
    ["contains-semicolon",  "car;l"],
    ["contains-backslash",  "back\\slash"],
    ["contains-cr",         "with\rcr"],
    ["contains-lf",         "with\nlf"],
    ["contains-all-five",   "all,;\\\r\n!"],
    ["starts-with-special", ",starts-comma"],
    ["ends-with-special",   "ends-comma,"],
    ["only-specials",       ",;\\,;\\"],
    ["repeated-escape",     "\\\\\\\\"],
    ["escape-then-char",    "\\;\\,\\\\!"],
    ["crlf-pair",           "line1\r\nline2"],
    ["empty-string",        ""],
];

function tcpReachable(host, port) {
    return new Promise(res => {
        const s = net.createConnection({ host, port });
        s.setTimeout(500);
        s.once("connect", () => { s.end(); res(true); });
        s.once("error",   () => res(false));
        s.once("timeout", () => { s.destroy(); res(false); });
    });
}

async function main() {
    if (!(await tcpReachable(HOST, PORT))) {
        console.log(`SKIP: CuttleDB server not reachable at ${HOST}:${PORT}`);
        process.exit(0);
    }

    const db = new CuttleDB({ transport: "tcp", host: HOST, port: PORT });
    await db.connect();
    const hid = await db.open();

    // ── GET round-trip (per-string) ──────────────────────────────
    const tid1 = await db.create(hid, "wire_contract", [
        ["payload", 2], ["idx", 0],
    ]);
    let passed = 0, failed = 0;
    for (const [label, val] of WIRE_CONTRACT_STRINGS) {
        const rid = await db.insert(hid, tid1, [val, label.length]);
        const got = await db.get(hid, tid1, rid);
        if (got[0] !== val) {
            console.error(`  FAIL get/${label}: stored=${JSON.stringify(val)} got=${JSON.stringify(got[0])}`);
            failed++;
        } else {
            passed++;
        }
    }
    console.log(`  GET round-trip: ${passed} passed, ${failed} failed`);

    // ── select_gt round-trip (all-at-once) ───────────────────────
    const tid2 = await db.create(hid, "wire_contract_bulk", [
        ["payload", 2], ["idx", 0],
    ]);
    const expected = new Map();
    for (let i = 0; i < WIRE_CONTRACT_STRINGS.length; i++) {
        const [, val] = WIRE_CONTRACT_STRINGS[i];
        const idx = 1000 + i;
        await db.insert(hid, tid2, [val, idx]);
        expected.set(idx, val);
    }
    const rows = await db.selectGt(hid, tid2, 1, 999);
    let bulkPassed = true;
    if (rows.length !== expected.size) {
        console.error(`  FAIL selectGt: row count mismatch — got ${rows.length} want ${expected.size}`);
        bulkPassed = false;
        failed++;
    } else {
        for (const r of rows) {
            if (r.length !== 2) {
                console.error(`  FAIL selectGt: column count split — row=${JSON.stringify(r)}`);
                bulkPassed = false;
                failed++;
                break;
            }
            const idx = parseInt(r[1], 10);
            const payload = r[0];
            const want = expected.get(idx);
            if (payload !== want) {
                console.error(`  FAIL selectGt/idx=${idx}: stored=${JSON.stringify(want)} got=${JSON.stringify(payload)}`);
                bulkPassed = false;
                failed++;
            }
        }
        if (bulkPassed) {
            console.log(`  selectGt all-at-once: PASS (${rows.length} rows)`);
            passed++;
        }
    }

    await db.close?.();
    console.log("");
    console.log(`Wire-contract result: ${passed} passed, ${failed} failed`);
    process.exit(failed > 0 ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
