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
    /** Build a secondary index. One `col` → a classic per-column index
     *  queried by {@link find}. Two or more columns → a *composite* index
     *  over their canonical-joined values, queried by {@link findc} for O(1)
     *  multi-column exact lookups (numeric and string columns may
     *  participate). Returns the number of rows indexed. Idempotent —
     *  rebuilding the same column list drops and recreates it. Maintained
     *  automatically on insert/delete. */
    async index(hid, tid, col, ...moreCols) {
        const cols = [col, ...moreCols].join(" ");
        return parseInt(await this.send(`INDEX ${hid} ${tid} ${cols}`), 10);
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

    /** Composite point lookup: return all row IDs where every
     *  `cols[i] === values[i]`. O(1) average when a composite index over the
     *  same column list exists (build via `index(hid, tid, ...cols)`); O(N)
     *  scan otherwise. Values are joined with the 0x1f unit separator so they
     *  may contain spaces and commas; numeric values round-trip through the
     *  same canonicalization as stored cells. */
    async findc(hid, tid, cols, values) {
        if (cols.length !== values.length) {
            throw new Error("findc: cols and values length mismatch");
        }
        const colList = cols.map(c => parseInt(c, 10)).join(" ");
        const valBlock = values.map(v => String(v)).join("\x1f");
        const body = await this.send(`FINDC ${hid} ${tid} ${cols.length} ${colList} ${valBlock}`);
        if (!body.startsWith("[") || !body.endsWith("]")) {
            throw new Error(`bad findc result: ${body}`);
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

    /** Set STRING column `col` to `newVal` on one row by physical rowId
     *  (UPDRS, v0.8.0). Returns 1 on success, rejects if `col` is not a
     *  STRING column. `newVal` is wire-escaped so embedded commas/newlines
     *  round-trip. Secondary string / composite / BM25 indexes are kept
     *  consistent and the change participates in transactions. */
    async updateRowStr(hid, tid, rowId, col, newVal) {
        return parseInt(
            await this.send(`UPDRS ${hid} ${tid} ${rowId} ${col} ${encodeValue(newVal)}`),
            10,
        );
    }

    /** Set STRING column `setCol` to `setVal` for every row where
     *  `predCol {op} threshold` (UPDATES, v0.8.0). Returns the number of
     *  rows updated. `setCol` must be STRING, `predCol` numeric. Op:
     *  Op.GT / Op.LT / Op.EQ. `setVal` is wire-escaped; index + transaction
     *  semantics match updateRowStr. */
    async updateWhereStr(hid, tid, setCol, setVal, predCol, op, threshold) {
        return parseInt(
            await this.send(
                `UPDATES ${hid} ${tid} ${setCol} ${predCol} ${op} ${threshold} ${encodeValue(setVal)}`,
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

    /** Aggregate by one or more grouping columns. Returns an array of
     *  `{ key, value }`. `key` is scalar for a single grouping column and
     *  an array of components when `by` adds more columns.
     *
     *  Options (all optional, generic CuttleDB API):
     *    agg     — "count" | "sum" | "min" | "max" | "avg" (default "count")
     *    aggCol  — numeric column to aggregate (required unless count)
     *    by      — extra grouping columns: number | number[]
     *    having  — [op, threshold] post-aggregation filter (op = Op.*)
     *    order   — [field, dir]; field "key"|0 / "value"|1, dir "asc"|0 / "desc"|1
     *    limit   — cap returned groups (after ordering) */
    async groupBy(hid, tid, groupCol, opts = {}) {
        const { agg = "count", aggCol = null, by = null,
                having = null, order = null, limit = null } = opts;
        const aggMap = { count: 0, sum: 1, min: 2, max: 3, avg: 4 };
        if (!(agg in aggMap)) {
            throw new Error(`unknown agg ${agg}; expected one of ${Object.keys(aggMap)}`);
        }
        const op = aggMap[agg];
        let ac;
        if (op === 0) {
            ac = aggCol == null ? 0 : aggCol;
        } else {
            if (aggCol == null) throw new Error(`agg=${agg} requires aggCol`);
            ac = aggCol;
        }
        let cmd = `GROUPBY ${hid} ${tid} ${groupCol} ${op} ${ac}`;
        if (by != null) {
            const cols = Array.isArray(by) ? by : [by];
            if (cols.length) cmd += " BY " + cols.map(c => parseInt(c, 10)).join(" ");
        }
        if (having != null) {
            const [hOp, hThr] = having;
            cmd += ` HAVING ${parseInt(hOp, 10)} ${parseInt(hThr, 10)}`;
        }
        if (order != null) {
            const [oField, oDir] = order;
            const fMap = { key: 0, value: 1 }, dMap = { asc: 0, desc: 1 };
            const of = typeof oField === "string" ? fMap[oField] : parseInt(oField, 10);
            const od = typeof oDir === "string" ? dMap[oDir] : parseInt(oDir, 10);
            cmd += ` ORDER ${of} ${od}`;
        }
        if (limit != null) cmd += ` LIMIT ${parseInt(limit, 10)}`;
        return parseGroupBy(await this.send(cmd));
    }

    /** 2-way join. Returns an array of `[leftRow, rightRow]` pairs. Matches
     *  rows where `left.lCol {op} right.rCol`. Both columns must be
     *  STRING-or-STRING or both numeric; VEC is rejected.
     *
     *  Options (v0.8.0):
     *    how — "inner" (default) | "left" | "right" | "full". Outer joins
     *          pair an unmatched row with -1 (the NULL sentinel) on the
     *          other side.
     *    op  — join predicate (Op.*). Op.EQ (default) runs as a hash join
     *          (O(N+M), no cap). Op.GT / Op.LT are non-equi nested-loop
     *          joins (left>right / left<right; strings compare lexically)
     *          and reject past ~100M comparisons with join_too_large. */
    async join(lHid, lTid, lCol, rHid, rTid, rCol, opts = {}) {
        const { how = "inner", op = Op.EQ } = opts;
        const howMap = { inner: 0, left: 1, right: 2, full: 3 };
        if (!(how in howMap)) {
            throw new Error(`unknown how ${how}; expected one of ${Object.keys(howMap)}`);
        }
        let cmd = `JOIN ${lHid} ${lTid} ${lCol} ${rHid} ${rTid} ${rCol}`;
        if (howMap[how] !== 0) cmd += ` TYPE ${howMap[how]}`;
        if (op !== Op.EQ) cmd += ` OP ${parseInt(op, 10)}`;
        const body = await this.send(cmd);
        if (!body.startsWith("[") || !body.endsWith("]")) {
            throw new Error(`bad join result: ${body}`);
        }
        const inner = body.slice(1, -1);
        if (!inner) return [];
        return inner.split(";").map(tok => {
            const [l, r] = tok.split(",");
            return [parseInt(l, 10), parseInt(r, 10)];
        });
    }

    // ── Vector search ────────────────────────────────────────────

    /** Top-`k` nearest rows by cosine similarity → `[{rowId, score}]` sorted
     *  desc. Pass `{ where }` to apply a Phase-1 filter clause
     *  ("col OP value [AND col OP value ...]"; string values quoted as
     *  `4="playbook"`, numeric bare as `5>3`, up to 8 AND'd predicates). */
    async knn(hid, tid, col, k, query, { where = null } = {}) {
        const q = query.join("|");
        let cmd = `KNN ${hid} ${tid} ${col} ${k} ${q}`;
        if (where) cmd += ` WHERE ${where}`;
        return parseKnn(await this.send(cmd));
    }

    /** Top-`k` lexical matches via BM25 over a STRING column →
     *  `[{rowId, score}]` sorted desc. The inverted index auto-builds on
     *  first use; `send("INDEX <hid> <tid> <col> BM25")` forces a rebuild. */
    async lsearch(hid, tid, col, k, query) {
        return parseKnn(await this.send(`LSEARCH ${hid} ${tid} ${col} ${k} ${query}`));
    }

    /** Boolean-DSL retrieval → `[{rowId, score}]` sorted desc. `expr`
     *  combines filters (AND/OR/parens, `= != < <= > >=`) with optional
     *  scoring atoms: `col~V[v1|v2|...]` (vector over a VEC column) and
     *  `col~"phrase"` (BM25 over a STRING column). Scoring atoms feed a
     *  per-row RRF rank; with none, results order by rowId asc, capped at
     *  `k`. */
    async bsearch(hid, tid, k, expr) {
        return parseKnn(await this.send(`BSEARCH ${hid} ${tid} ${k} ${expr}`));
    }

    /** Hybrid retrieval — fuses KNN and BM25 via Reciprocal Rank Fusion →
     *  `[{rowId, score}]` sorted desc. The score is an RRF rank metric
     *  (comparable across queries by order, not magnitude). Pass `{ where }`
     *  to filter both streams (same syntax as {@link knn}). */
    async search(hid, tid, vecCol, textCol, k, vec, query, { where = null } = {}) {
        const v = vec.join("|");
        let cmd = `SEARCH ${hid} ${tid} ${vecCol} ${textCol} ${k} ${v} ||| ${query}`;
        if (where) cmd += ` WHERE ${where}`;
        return parseKnn(await this.send(cmd));
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

/** Parse one GROUPBY key component at index `i`. Returns
 *  `[value, nextIndex, terminator]` where terminator is "|" (more key
 *  components), ":" (key done, value follows) or "" (end). Quoted →
 *  string; bare → Number when finite, else the raw string. */
function parseGbComponent(s, i) {
    if (s[i] === '"') {
        const end = s.indexOf('"', i + 1);
        if (end < 0) return [s.slice(i + 1), s.length, ""];
        const nxt = end + 1;
        return [s.slice(i + 1, end), nxt + 1, nxt < s.length ? s[nxt] : ""];
    }
    let j = i;
    while (j < s.length && s[j] !== "|" && s[j] !== ":") j++;
    const raw = s.slice(i, j);
    const num = Number(raw);
    const val = raw !== "" && Number.isFinite(num) ? num : raw;
    return [val, j + 1, j < s.length ? s[j] : ""];
}

/** Parse a GROUPBY result body "[key:value;...]" into an array of
 *  `{ key, value }`. Single-column keys are scalar; multi-column keys
 *  (joined with "|" on the wire) become arrays of components. */
function parseGroupBy(body) {
    if (!body.startsWith("[") || !body.endsWith("]")) {
        throw new Error(`bad groupby result: ${body}`);
    }
    const inner = body.slice(1, -1);
    if (!inner) return [];
    const out = [];
    for (const entry of inner.split(";")) {
        if (!entry) continue;
        const comps = [];
        let i = 0, value = "";
        while (i < entry.length) {
            const [comp, next, term] = parseGbComponent(entry, i);
            comps.push(comp);
            i = next;
            if (term === ":") { value = entry.slice(i); break; }
        }
        if (!comps.length) continue;
        out.push({ key: comps.length === 1 ? comps[0] : comps, value: Number(value) });
    }
    return out;
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
