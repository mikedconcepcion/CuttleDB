// CuttleDB — embeddable WASM (in-process, no server).
//
// Runs the CuttleDB engine compiled to WebAssembly entirely in-process: no
// server, no socket, no install. The full JavaScript SDK works unchanged —
// this module just supplies a third transport ("wasm") that executes wire
// lines in-process via the engine's `cuttledb_exec_line` entry point instead
// of sending them over TCP/WebSocket.
//
// Works in Node and the browser (the loader targets both). In the browser,
// serve this folder so cuttledb-engine.wasm is fetchable next to the loader.
//
//   import { connect } from "cuttledb/wasm";   // installed package
//   // or, vendored from the repo: import { connect } from "./wasm/cuttledb.mjs";
//   const db = await connect();              // boots the engine in-process
//   const hid = await db.open();
//   const tid = await db.create(hid, "docs", [["body", 2]]);  // 2 = string
//   await db.insert(hid, tid, ["the quick brown fox"]);
//   const hits = await db.lsearch(hid, tid, 0, 5, "fox");      // BM25
//
// Or load a pre-built snapshot and query it:
//
//   const hid = await db.loadSnapshot(snapshotBytes);          // Uint8Array
//   const hits = await db.lsearch(hid, 0, 0, 5, "fox river");
//
// `db` is an ordinary CuttleDB SDK instance, so every verb the TCP/WS client
// exposes (create/insert/find/findc/index/transactions/…) works in-process.

import createCuttleDB from "./cuttledb-engine.js";
import { CuttleDB } from "../cuttledb.js";

const OUT_CAP = 1 << 16; // 64 KiB response buffer, matches the wire send cap.

// A drop-in transport for the CuttleDB SDK that runs each wire line in-process
// against the WASM engine. Mirrors the TcpTransport/WsTransport interface the
// SDK expects (connect / send / sendBatch / close / onEvent).
export class WasmTransport {
  constructor(module) {
    this._m = module;
  }

  async connect() {} // the module is already booted before construction.

  send(cmd) {
    return Promise.resolve(this._exec(cmd));
  }

  sendBatch(cmds) {
    return Promise.resolve(cmds.map((c) => this._exec(c)));
  }

  close() {}

  // No server-push events in-process; return a no-op unsubscribe.
  onEvent(_cb) {
    return () => {};
  }

  // Expose the engine's virtual FS for snapshot loading.
  writeFile(name, bytes) {
    this._m.FS.writeFile(name, bytes);
  }

  _exec(line) {
    const m = this._m;
    const outPtr = m._malloc(OUT_CAP);
    try {
      // The entry point returns the number of bytes written and does NOT
      // null-terminate, so bound the read by `n` — otherwise a reused buffer
      // leaks the tail of a longer prior response into this one.
      const n = m.ccall(
        "cuttledb_exec_line",
        "number",
        ["string", "number", "number"],
        [line, outPtr, OUT_CAP]
      );
      if (n <= 0) return "";
      return m.UTF8ToString(outPtr, n).replace(/\r?\n$/, "");
    } finally {
      m._free(outPtr);
    }
  }
}

// Boot the engine in-process and return a connected CuttleDB SDK instance.
// `opts` is forwarded to the Emscripten module factory (e.g. { locateFile }
// to host the .wasm somewhere non-default).
export async function connect(opts = {}) {
  const module = await createCuttleDB({ noInitialRun: true, ...opts });
  const transport = new WasmTransport(module);
  const db = new CuttleDB({ transport });
  await db.connect();

  // Mount a snapshot into the engine's virtual FS and LOAD it into a fresh
  // handle. `bytes` is a Uint8Array (or ArrayBuffer) of an index.snap. Returns
  // the handle id the snapshot was loaded into.
  db.loadSnapshot = async (bytes, name = "/index.snap") => {
    const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    transport.writeFile(name, u8);
    return parseInt(await db.send(`LOAD ${name}`), 10);
  };

  return db;
}

export { CuttleDB };
