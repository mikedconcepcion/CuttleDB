// CuttleDB JavaScript SDK — works in Node (TCP) and the browser (WebSocket).
//
// Usage (Node, server on 127.0.0.1:7780):
//   import { CuttleDB } from "cuttledb";
//   const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
//   await db.connect();
//   const hid = await db.open();
//   ...
//
// Usage (Browser, WebSocket to a shared server):
//   import { CuttleDB } from "cuttledb/browser";
//   const db = new CuttleDB({ transport: "ws", url: "ws://localhost:7780" });
//   await db.connect();
//
// The class API is identical across transports. The only knobs are the
// transport name and its options.

const DEFAULT_RECV_CAP = 65536;

// ── Transport: TCP (Node) ─────────────────────────────────────────────

class TcpTransport {
    constructor({ host = "127.0.0.1", port = 7780 } = {}) {
        this.host = host;
        this.port = port;
        this.socket = null;
        this._buf = "";
        this._waiters = [];     // {resolve, reject} for in-flight requests
        this._eventListeners = new Set();
    }

    async connect() {
        const net = await import("node:net");
        await new Promise((resolve, reject) => {
            const s = net.createConnection({ host: this.host, port: this.port }, () => {
                this.socket = s;
                s.setNoDelay(true);
                s.setEncoding("utf8");
                s.on("data", (chunk) => this._onData(chunk));
                s.on("error", (err) => this._failAll(err));
                s.on("close", () => this._failAll(new Error("connection closed")));
                resolve();
            });
            s.once("error", reject);
        });
    }

    _onData(chunk) {
        this._buf += chunk;
        let nl;
        while ((nl = this._buf.indexOf("\n")) !== -1) {
            let line = this._buf.slice(0, nl);
            this._buf = this._buf.slice(nl + 1);
            if (line.endsWith("\r")) line = line.slice(0, -1);
            if (line.startsWith(">EVT ")) {
                const evt = parseEvent(line);
                for (const cb of this._eventListeners) cb(evt);
            } else {
                const w = this._waiters.shift();
                if (w) w.resolve(line);
            }
        }
    }

    _failAll(err) {
        const ws = this._waiters; this._waiters = [];
        for (const w of ws) w.reject(err);
    }

    send(cmd) {
        return new Promise((resolve, reject) => {
            if (!this.socket) return reject(new Error("not connected"));
            this._waiters.push({ resolve, reject });
            this.socket.write(cmd + "\n");
        });
    }

    sendBatch(cmds) {
        return new Promise((resolve, reject) => {
            if (!this.socket) return reject(new Error("not connected"));
            const out = [];
            for (let i = 0; i < cmds.length; i++) {
                this._waiters.push({
                    resolve: (line) => {
                        out.push(line);
                        if (out.length === cmds.length) resolve(out);
                    },
                    reject,
                });
            }
            this.socket.write(cmds.join("\n") + "\n");
        });
    }

    onEvent(cb)  { this._eventListeners.add(cb);    return () => this._eventListeners.delete(cb); }

    close() {
        if (this.socket) {
            try { this.socket.end(); } catch {}
            this.socket = null;
        }
    }
}

// ── Transport: WebSocket (browser or Node 22+) ────────────────────────

class WsTransport {
    constructor({ url, WebSocket: WSCtor } = {}) {
        if (!url) throw new Error("WsTransport: pass url (e.g. ws://host:7780)");
        this.url = url;
        this._WS = WSCtor || globalThis.WebSocket;
        if (!this._WS) {
            throw new Error("WsTransport: no WebSocket available. Use Node 22+ or pass opts.WebSocket.");
        }
        this.socket = null;
        this._waiters = [];
        this._eventListeners = new Set();
    }

    connect() {
        return new Promise((resolve, reject) => {
            const ws = new this._WS(this.url);
            ws.binaryType = "arraybuffer";
            ws.onopen = () => { this.socket = ws; resolve(); };
            ws.onmessage = (e) => {
                const text = typeof e.data === "string"
                    ? e.data
                    : new TextDecoder().decode(new Uint8Array(e.data));
                this._onMessage(text);
            };
            ws.onerror = (e) => {
                if (!this.socket) reject(new Error("WebSocket connect failed"));
            };
            ws.onclose = () => this._failAll(new Error("WebSocket closed"));
        });
    }

    _onMessage(text) {
        // A frame may contain one response line, or (in subscriber mode) a
        // response interleaved with one or more >EVT lines. Split on newline,
        // route each non-empty line.
        for (const raw of text.split("\n")) {
            const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
            if (!line) continue;
            if (line.startsWith(">EVT ")) {
                const evt = parseEvent(line);
                for (const cb of this._eventListeners) cb(evt);
            } else {
                const w = this._waiters.shift();
                if (w) w.resolve(line);
            }
        }
    }

    _failAll(err) {
        const ws = this._waiters; this._waiters = [];
        for (const w of ws) w.reject(err);
    }

    send(cmd) {
        return new Promise((resolve, reject) => {
            if (!this.socket) return reject(new Error("not connected"));
            this._waiters.push({ resolve, reject });
            this.socket.send(cmd);  // one command per frame
        });
    }

    sendBatch(cmds) {
        return new Promise((resolve, reject) => {
            if (!this.socket) return reject(new Error("not connected"));
            const out = [];
            for (let i = 0; i < cmds.length; i++) {
                this._waiters.push({
                    resolve: (line) => {
                        out.push(line);
                        if (out.length === cmds.length) resolve(out);
                    },
                    reject,
                });
            }
            // The server's pipeline-flush kicks in once recv drains, so sending
            // all frames in close succession still yields one batched response.
            for (const c of cmds) this.socket.send(c);
        });
    }

    onEvent(cb) { this._eventListeners.add(cb); return () => this._eventListeners.delete(cb); }

    close() {
        if (this.socket) {
            try { this.socket.close(); } catch {}
            this.socket = null;
        }
    }
}

// ── Event parsing ─────────────────────────────────────────────────────

function parseEvent(line) {
    // ">EVT hid tid row_id op"
    const parts = line.split(" ");
    return {
        hid:   parseInt(parts[1], 10),
        tid:   parseInt(parts[2], 10),
        rowId: parseInt(parts[3], 10),
        op:    parts[4],
    };
}

// ── Public class ──────────────────────────────────────────────────────

export class CuttleDB {
    constructor(opts = {}) {
        const transport = opts.transport || "tcp";
        if (transport === "tcp") {
            this.transport = new TcpTransport(opts);
        } else if (transport === "ws") {
            this.transport = new WsTransport(opts);
        } else if (typeof transport === "object") {
            this.transport = transport;  // injected (for tests)
        } else {
            throw new Error(`unknown transport: ${transport}`);
        }
        this._auth = opts.auth || null;
    }

    async connect() {
        await this.transport.connect();
        if (this._auth) {
            // Send AUTH eagerly; rejects if server doesn't accept the token.
            await this.send(`AUTH ${this._auth}`);
        }
    }

    close() { return this.transport.close(); }

    on(eventName, cb) {
        if (eventName !== "event") {
            throw new Error(`unknown event: ${eventName}`);
        }
        return this.transport.onEvent(cb);
    }

    // ── Wire-level send (post-unwrap) ────────────────────────────

    async send(cmd)            { return this._ok(await this.transport.send(cmd)); }
    async sendBatch(cmds)      { return (await this.transport.sendBatch(cmds)).map(this._ok); }

    _ok = (line) => {
        if (line.startsWith("+OK")) return line.length > 3 ? line.slice(4) : "";
        if (line.startsWith("-ERR ")) throw new Error(line.slice(5));
        throw new Error(`unexpected response: ${line}`);
    };

    // ── Server meta ──────────────────────────────────────────────

    async ping()  { return this.send("PING"); }
    async hello() { return this.send("HELLO"); }
    async info()  { return parseKv(await this.send("INFO")); }
    async stats(hid, tid) {
        if (hid === undefined) return parseKv(await this.send("STATS"));
        if (tid === undefined) return parseKv(await this.send(`STATS ${hid}`));
        return parseKv(await this.send(`STATS ${hid} ${tid}`));
    }

    // ── DDL ──────────────────────────────────────────────────────

    async open() { return parseInt(await this.send("OPEN"), 10); }

    /** Release a handle and free its tables/columns. The id may be reused
     *  by a later `open()`. Throws if the hid is unknown or already freed. */
    async closeHandle(hid) { await this.send(`CLOSE ${hid}`); }

    /**
     * Create a table. `columns` is an array of `[name, type]` (int/float/string)
     * or `[name, 3, dim]` (vector). Types: 0=int, 1=float, 2=string, 3=vec.
     */
    async create(hid, name, columns) {
        const spec = columns.map(c => {
            if (c.length === 2) return `${c[0]}:${c[1]}`;
            if (c.length === 3 && c[1] === 3) return `${c[0]}:3:${c[2]}`;
            throw new Error(`bad column spec: ${JSON.stringify(c)}`);
        }).join(",");
        return parseInt(await this.send(`CREATE ${hid} ${name} ${spec}`), 10);
    }

    /** Add a column to an existing table. Returns the new column index.
     *  Existing rows are filled with defaults (0 / "" / zero-vector). */
    async alterAdd(hid, tid, name, type, dim = 0) {
        const spec = type === 3 ? `${name}:3:${dim}` : `${name}:${type}`;
        return parseInt(await this.send(`ALTER ${hid} ${tid} ADD ${spec}`), 10);
    }

    // ── Transactions ───────────────────────────────────────────────

    async begin()    { await this.send("BEGIN"); }
    async commit()   { return parseInt(await this.send("COMMIT"),   10); }
    async rollback() { return parseInt(await this.send("ROLLBACK"), 10); }

    /** Run `fn(this)` inside a tx. Commits on success, rolls back on throw. */
    async transaction(fn) {
        await this.begin();
        try {
            const r = await fn(this);
            await this.commit();
            return r;
        } catch (e) {
            try { await this.rollback(); } catch {}
            throw e;
        }
    }

    /** Build a secondary index on a string column. Returns the row count
     *  indexed. Subsequent inserts/deletes maintain it automatically.
     *  v0.5.0 limitation: string columns only. */
    async index(hid, tid, col) {
        return parseInt(await this.send(`INDEX ${hid} ${tid} ${col}`), 10);
    }

    /** Return all row IDs where `col == value`. O(1) with an index, O(N)
     *  without. The value may contain spaces but not commas. */
    async find(hid, tid, col, value) {
        const body = await this.send(`FIND ${hid} ${tid} ${col} ${value}`);
        if (!body.startsWith("[") || !body.endsWith("]")) {
            throw new Error(`bad find result: ${body}`);
        }
        const inner = body.slice(1, -1);
        if (!inner) return [];
        return inner.split(";").map(s => parseInt(s, 10));
    }

    // ── DML — write ──────────────────────────────────────────────

    async insert(hid, tid, values) {
        const csv = values.map(encodeValue).join(",");
        return parseInt(await this.send(`INSERT ${hid} ${tid} ${csv}`), 10);
    }

    async insertBatch(hid, tid, rows) {
        const cmds = rows.map(r => `INSERT ${hid} ${tid} ${r.map(encodeValue).join(",")}`);
        return (await this.sendBatch(cmds)).map(s => parseInt(s, 10));
    }

    async delete(hid, tid, rowId) {
        return parseInt(await this.send(`DELETE ${hid} ${tid} ${rowId}`), 10) === 1;
    }

    /** Set `setCol = setVal` for every row where `predCol {op} threshold`.
     *  Returns the number of rows updated. Op: Op.GT / Op.LT / Op.EQ.
     *  Both columns must be numeric in v0.5.0. */
    async updateWhere(hid, tid, setCol, setVal, predCol, op, threshold) {
        return parseInt(
            await this.send(
                `UPDATE ${hid} ${tid} ${setCol} ${setVal} ${predCol} ${op} ${threshold}`,
            ), 10,
        );
    }

    /** Delete every row where `predCol {op} threshold`. Returns the number
     *  of rows deleted. Each deletion logs + broadcasts a DEL event. */
    async deleteWhere(hid, tid, predCol, op, threshold) {
        return parseInt(
            await this.send(`DELW ${hid} ${tid} ${predCol} ${op} ${threshold}`),
            10,
        );
    }

    // ── DML — read ───────────────────────────────────────────────

    async get(hid, tid, rowId)         { return splitWireRow(await this.send(`GET ${hid} ${tid} ${rowId}`)); }
    async count(hid, tid)              { return parseInt(await this.send(`COUNT ${hid} ${tid}`), 10); }
    async sum(hid, tid, col)           { return Number(await this.send(`SUM ${hid} ${tid} ${col}`)); }
    async min(hid, tid, col)           { return Number(await this.send(`MIN ${hid} ${tid} ${col}`)); }
    async max(hid, tid, col)           { return Number(await this.send(`MAX ${hid} ${tid} ${col}`)); }
    async fcountGt(hid, tid, col, t)   { return parseInt(await this.send(`FCOUNT ${hid} ${tid} ${col} ${t}`), 10); }

    async selectGt(hid, tid, col, t) {
        const body = await this.send(`SELGT ${hid} ${tid} ${col} ${t}`);
        return parseRowlist(body);
    }

    // ── Vector search ────────────────────────────────────────────

    async knn(hid, tid, col, k, query) {
        const q = query.join("|");
        const body = await this.send(`KNN ${hid} ${tid} ${col} ${k} ${q}`);
        return parseKnn(body);
    }

    // ── Persistence ──────────────────────────────────────────────

    async save(hid, path) { return this.send(`SAVE ${hid} ${path}`); }
    async load(path)      { return parseInt(await this.send(`LOAD ${path}`), 10); }

    // ── Subscriptions ────────────────────────────────────────────

    async sub(hid, tid)   { return this.send(`SUB ${hid} ${tid}`); }
    async unsub(hid, tid) { return this.send(`UNSUB ${hid} ${tid}`); }

    // ── Change feed ──────────────────────────────────────────────

    async log(hid, tid, since = 0) {
        const body = await this.send(`LOG ${hid} ${tid} ${since}`);
        return parseLog(body);
    }
}

// ── Helpers ───────────────────────────────────────────────────────────

function encodeValue(v) {
    if (Array.isArray(v)) return v.join("|");
    let s = String(v);
    // Backslash-escape the bytes the server's wire parser treats as
    // delimiters: ``\\`` ``,`` ``\\r`` ``\\n``. Mirror of the Python
    // adapter's _encode_value. Order matters — escape backslash first.
    s = s.replaceAll("\\", "\\\\");
    s = s.replaceAll(",",  "\\,");
    s = s.replaceAll("\r", "\\r");
    s = s.replaceAll("\n", "\\n");
    return s;
}

function parseKv(line) {
    const out = {};
    for (const tok of line.split(" ")) {
        const eq = tok.indexOf("=");
        if (eq >= 0) out[tok.slice(0, eq)] = tok.slice(eq + 1);
    }
    return out;
}

/* Escape-aware row split: split on bare ``,``, honoring ``\\<X>``
 * escapes. Mirror of the server's wire_str_encode: ``\\,`` decodes to a
 * literal comma, ``\\;`` to ``;``, ``\\\\`` to ``\\``, ``\\r`` to CR,
 * ``\\n`` to LF, and any other ``\\<X>`` to a literal X. */
function splitWireRow(s) {
    const out = [];
    let cur = "";
    for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (c === "\\" && i + 1 < s.length) {
            const esc = s[i + 1];
            if      (esc === "r") cur += "\r";
            else if (esc === "n") cur += "\n";
            else                  cur += esc;   // literal: , ; \ etc.
            i += 1;
        } else if (c === ",") {
            out.push(cur);
            cur = "";
        } else {
            cur += c;
        }
    }
    out.push(cur);
    return out;
}

/* Escape-aware rowlist split: split on bare ``;``, honoring ``\\;``
 * escapes so STRING values containing a literal semicolon don't
 * terminate the row early. Counterpart to splitWireRow. */
function splitWireRows(s) {
    const out = [];
    let cur = "";
    for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (c === "\\" && i + 1 < s.length) {
            cur += c + s[i + 1];
            i += 1;
        } else if (c === ";") {
            out.push(cur);
            cur = "";
        } else {
            cur += c;
        }
    }
    out.push(cur);
    return out;
}

function parseRowlist(body) {
    if (!body.startsWith("[") || !body.endsWith("]")) {
        throw new Error(`bad row list: ${body}`);
    }
    const inner = body.slice(1, -1);
    if (!inner) return [];
    return splitWireRows(inner).map(splitWireRow);
}

function parseKnn(body) {
    if (!body.startsWith("[") || !body.endsWith("]")) {
        throw new Error(`bad knn result: ${body}`);
    }
    const inner = body.slice(1, -1);
    if (!inner) return [];
    return inner.split(";").map(tok => {
        const [rid, score] = tok.split(":");
        return { rowId: parseInt(rid, 10), score: parseFloat(score) };
    });
}

function parseLog(body) {
    const sp = body.indexOf(" ");
    const cursor = parseInt(body.slice(0, sp), 10);
    const rest = body.slice(sp + 1);
    if (!rest.startsWith("[") || !rest.endsWith("]")) {
        throw new Error(`bad log result: ${body}`);
    }
    const inner = rest.slice(1, -1);
    if (!inner) return { cursor, events: [] };
    const events = inner.split(";").map(tok => {
        const [ts, rid, op] = tok.split(":");
        return { tsMs: parseInt(ts, 10), rowId: parseInt(rid, 10), op };
    });
    return { cursor, events };
}

/** Predicate comparison operators for fcountGt / selectGt / updateWhere /
 *  deleteWhere. Numeric values are stable wire-protocol contracts. */
export const Op = Object.freeze({ GT: 0, LT: 1, EQ: 2 });

// Re-exports for users who want to construct transports directly.
export { TcpTransport, WsTransport };

// ── CLI smoke when run directly under Node ────────────────────────────

if (typeof process !== "undefined" && import.meta.url === `file://${process.argv[1]?.replaceAll("\\", "/")}`) {
    const db = new CuttleDB({ transport: "tcp", host: "127.0.0.1", port: 7780 });
    await db.connect();
    const hid = await db.open();
    const tid = await db.create(hid, "t", [["name", 2], ["v", 0]]);
    await db.insert(hid, tid, ["Alice", 100]);
    await db.insert(hid, tid, ["Bob", 200]);
    console.log("count:", await db.count(hid, tid));
    console.log("sum:  ", await db.sum(hid, tid, 1));
    console.log("rows: ", await db.selectGt(hid, tid, 1, 50));
    db.close();
}
