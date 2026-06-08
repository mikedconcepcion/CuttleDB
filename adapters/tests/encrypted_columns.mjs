// End-to-end encrypted-column tests (v0.9.0) — JS side.
//
// Proves the server only ever sees ciphertext: launch a real CuttleDB server,
// write with insertEnc / insertBatchEnc, then check a raw GET returns an
// `enc:v1:` token in the encrypted column (plaintext columns untouched) and
// getDec restores the original value.
//
// Self-launches the server from CUTTLEDB_SERVER_BIN; SKIPs if unset.
//
// Run:  CUTTLEDB_SERVER_BIN=.../cuttledb-server.exe node adapters/tests/encrypted_columns.mjs

import { strict as assert } from "node:assert";
import { spawn } from "node:child_process";
import net from "node:net";
import { CuttleDB, FieldCipher } from "../cuttledb.js";

const BINARY = process.env.CUTTLEDB_SERVER_BIN || "";

const STRING = 2, INT = 0;

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS ${name}`); }
    catch (e) { failed++; console.error(`  FAIL ${name}: ${e.message}`); }
}

function freePort() {
    return new Promise((res, rej) => {
        const s = net.createServer();
        s.listen(0, "127.0.0.1", () => {
            const { port } = s.address();
            s.close(() => res(port));
        });
        s.once("error", rej);
    });
}

function waitListening(port, timeoutMs = 3000) {
    const deadline = Date.now() + timeoutMs;
    return new Promise(res => {
        (function attempt() {
            const s = net.createConnection({ host: "127.0.0.1", port });
            s.once("connect", () => { s.end(); res(true); });
            s.once("error", () => {
                if (Date.now() > deadline) return res(false);
                setTimeout(attempt, 50);
            });
        })();
    });
}

async function makeTable(db) {
    const hid = await db.open();
    // col0 secret (STRING, encrypted), col1 label (STRING), col2 score (INT)
    const tid = await db.create(hid, "vault", [
        ["secret", STRING], ["label", STRING], ["score", INT],
    ]);
    return { hid, tid };
}

async function main() {
    if (!BINARY) {
        console.log("SKIP: set CUTTLEDB_SERVER_BIN to run encrypted-column e2e tests");
        process.exit(0);
    }

    const port = await freePort();
    const proc = spawn(BINARY, ["--cuttledb", String(port)], { stdio: "ignore" });
    const up = await waitListening(port);
    if (!up) { proc.kill(); throw new Error("server did not start"); }

    const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port });
    await db.connect();

    await check("server stores ciphertext only; getDec restores plaintext", async () => {
        const { hid, tid } = await makeTable(db);
        const cipher = await FieldCipher.create(await FieldCipher.generateKey());
        const rid = await db.insertEnc(hid, tid, ["top-secret", "public-label", 42],
                                       cipher, [0]);
        const raw = await db.get(hid, tid, rid);
        assert.ok(FieldCipher.isEncrypted(raw[0]), `col0 not encrypted: ${raw[0]}`);
        assert.ok(!raw[0].includes("top-secret"));
        assert.equal(raw[1], "public-label");
        assert.equal(raw[2], "42");
        const dec = await db.getDec(hid, tid, rid, cipher, [0]);
        assert.deepEqual(dec, ["top-secret", "public-label", "42"]);
    });

    await check("multiple encrypted columns", async () => {
        const { hid, tid } = await makeTable(db);
        const cipher = await FieldCipher.create(await FieldCipher.generateKey());
        const rid = await db.insertEnc(hid, tid, ["secret-a", "secret-b", 7], cipher, [0, 1]);
        const raw = await db.get(hid, tid, rid);
        assert.ok(FieldCipher.isEncrypted(raw[0]));
        assert.ok(FieldCipher.isEncrypted(raw[1]));
        const dec = await db.getDec(hid, tid, rid, cipher, [0, 1]);
        assert.deepEqual(dec.slice(0, 2), ["secret-a", "secret-b"]);
    });

    await check("insertBatchEnc", async () => {
        const { hid, tid } = await makeTable(db);
        const cipher = await FieldCipher.create(await FieldCipher.generateKey());
        const rows = Array.from({ length: 5 }, (_, i) => ["alpha", `label${i}`, i]);
        const rids = await db.insertBatchEnc(hid, tid, rows, cipher, [0]);
        assert.equal(rids.length, 5);
        for (let i = 0; i < rids.length; i++) {
            const raw = await db.get(hid, tid, rids[i]);
            assert.ok(FieldCipher.isEncrypted(raw[0]));
            assert.equal(raw[1], `label${i}`);
            const dec = await db.getDec(hid, tid, rids[i], cipher, [0]);
            assert.equal(dec[0], "alpha");
        }
    });

    await check("specials + unicode + empty round-trip", async () => {
        const { hid, tid } = await makeTable(db);
        const cipher = await FieldCipher.create(await FieldCipher.generateKey());
        for (const pt of ["with,comma;semi", "back\\slash\r\nlf", "café ☕ 日本語", ""]) {
            const rid = await db.insertEnc(hid, tid, [pt, "lbl", 0], cipher, [0]);
            const dec = await db.getDec(hid, tid, rid, cipher, [0]);
            assert.equal(dec[0], pt);
        }
    });

    await check("wrong key cannot decrypt", async () => {
        const { hid, tid } = await makeTable(db);
        const cipher = await FieldCipher.create(await FieldCipher.generateKey());
        const other  = await FieldCipher.create(await FieldCipher.generateKey());
        const rid = await db.insertEnc(hid, tid, ["classified", "lbl", 0], cipher, [0]);
        await assert.rejects(async () => { await db.getDec(hid, tid, rid, other, [0]); });
    });

    await db.close?.();
    proc.kill();
    try { proc.kill("SIGKILL"); } catch { /* already gone */ }

    console.log("");
    console.log(`encrypted_columns result: ${passed} passed, ${failed} failed`);
    process.exit(failed > 0 ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
