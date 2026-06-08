// Client-side field-encryption tests (v0.9.0) — JS side + cross-language.
//
// Three layers:
//   * Node unit tests of FieldCipher (round-trip, passthrough, tamper, keylen).
//   * Cross-language round-trip: encrypt in JS / decrypt in Python and the
//     reverse, with a shared key. This is the real proof that the `enc:v1:`
//     token format (nonce[12] || ct || tag[16], base64) is byte-compatible.
//   * CLI mode (`node field_crypto.mjs enc|dec <keyhex> <value>`) so the
//     Python suite (or anything) can drive the JS cipher the same way.
//
// The cross-language layer shells out to ../python/tests/test_field_crypto.py.
// If `cryptography` isn't installed there, that layer SKIPs (not fails) so the
// JS-only checks still gate. Set CUTTLEDB_PYTHON to pick the interpreter.
//
// Run:  node adapters/tests/field_crypto.mjs

import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { FieldCipher } from "../cuttledb.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PY_DIR = path.resolve(HERE, "..", "python");
const PY_TEST = path.join(PY_DIR, "tests", "test_field_crypto.py");
const PYTHON = process.env.CUTTLEDB_PYTHON || "python";

// Fixed key shared with the Python CLI (matches _KEY there: 00 01 .. 1f).
const KEY_HEX = Array.from({ length: 32 }, (_, i) =>
    i.toString(16).padStart(2, "0")).join("");
const KEY = Buffer.from(KEY_HEX, "hex");

let passed = 0, failed = 0;
function check(name, fn) {
    try { fn(); passed++; console.log(`  PASS ${name}`); }
    catch (e) { failed++; console.error(`  FAIL ${name}: ${e.message}`); }
}

// ── Node-only unit tests ───────────────────────────────────────────────
async function unitTests() {
    const c = await FieldCipher.create(KEY);

    check("round-trip (incl. unicode, specials, empty, long)", () => {
        for (const pt of ["", "alice", "hello world", "unicode: café ☕ 日本語",
                          "with,comma;semi\\back\r\n", "x".repeat(5000)]) {
            const tok = c.encrypt(pt);
            assert.ok(FieldCipher.isEncrypted(tok));
            assert.equal(c.decrypt(tok), pt);
        }
    });

    check("nonce is random (same plaintext → different token)", () => {
        assert.notEqual(c.encrypt("same"), c.encrypt("same"));
    });

    check("non-token passes through unchanged", () => {
        assert.equal(c.decrypt("not-encrypted"), "not-encrypted");
    });

    check("isEncrypted discriminates", () => {
        assert.ok(FieldCipher.isEncrypted(c.encrypt("x")));
        assert.ok(!FieldCipher.isEncrypted("plain"));
        assert.ok(!FieldCipher.isEncrypted(123));
        assert.ok(!FieldCipher.isEncrypted(null));
    });

    check("tamper detection (GCM tag rejects flipped byte)", () => {
        const tok = c.encrypt("secret");
        const body = tok.slice("enc:v1:".length);
        const flipped = (body.at(-2) !== "A" ? "A" : "B") + body.at(-1);
        const bad = "enc:v1:" + body.slice(0, -2) + flipped;
        assert.throws(() => c.decrypt(bad));
    });

    check("wrong key rejected", async () => {
        const c2 = await FieldCipher.create(Buffer.from(
            Array.from({ length: 32 }, (_, i) => i + 1)));
        assert.throws(() => c2.decrypt(c.encrypt("secret")));
    });

    check("key length validation", () => {
        assert.throws(() => new FieldCipher(Buffer.alloc(31), {}));
        assert.throws(() => new FieldCipher(Buffer.alloc(33), {}));
    });

    check("generateKey → 32 random bytes", async () => {
        const k = await FieldCipher.generateKey();
        assert.equal(k.length, 32);
        assert.notEqual(Buffer.compare(k, await FieldCipher.generateKey()), 0);
    });
}

// ── Cross-language round-trip ───────────────────────────────────────────
function pyCli(mode, value) {
    const r = spawnSync(PYTHON, [PY_TEST, mode, KEY_HEX, value], {
        cwd: PY_DIR,
        env: { ...process.env, PYTHONPATH: PY_DIR },
        encoding: "utf8",
    });
    return r;
}

async function crossLanguage() {
    // Probe: is the python cipher importable here at all?
    const probe = pyCli("enc", "probe");
    if (probe.status !== 0) {
        console.log("  SKIP cross-language: python cipher unavailable " +
                    `(status=${probe.status}) — ${(probe.stderr || "").trim()}`);
        return;
    }

    const c = await FieldCipher.create(KEY);
    const samples = ["alice", "", "unicode: café ☕ 日本語",
                     "with,comma;semi\\back\r\n", "x".repeat(2000)];

    check("JS encrypt → Python decrypt", () => {
        for (const pt of samples) {
            const tok = c.encrypt(pt);
            const r = pyCli("dec", tok);
            assert.equal(r.status, 0, `python dec failed: ${r.stderr}`);
            assert.equal(r.stdout, pt, `mismatch for ${JSON.stringify(pt)}`);
        }
    });

    check("Python encrypt → JS decrypt", () => {
        for (const pt of samples) {
            const r = pyCli("enc", pt);
            assert.equal(r.status, 0, `python enc failed: ${r.stderr}`);
            const tok = r.stdout;
            assert.ok(FieldCipher.isEncrypted(tok), `not a token: ${tok}`);
            assert.equal(c.decrypt(tok), pt);
        }
    });
}

async function main() {
    console.log("FieldCipher unit tests (Node):");
    await unitTests();
    console.log("\nCross-language round-trip (JS ↔ Python):");
    await crossLanguage();
    console.log("");
    console.log(`field_crypto result: ${passed} passed, ${failed} failed`);
    process.exit(failed > 0 ? 1 : 0);
}

// ── CLI mode (enc|dec) so other languages can drive the JS cipher ──────
const [, , cliMode, cliKey, cliVal] = process.argv;
if (cliMode === "enc" || cliMode === "dec") {
    const c = await FieldCipher.create(Buffer.from(cliKey, "hex"));
    process.stdout.write(cliMode === "enc" ? c.encrypt(cliVal) : c.decrypt(cliVal));
    process.exit(0);
} else {
    main().catch(e => { console.error(e); process.exit(1); });
}
