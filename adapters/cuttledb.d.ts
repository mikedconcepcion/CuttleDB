// TypeScript declarations for the CuttleDB SDK.

export type Transport = "tcp" | "ws";

export interface CommonOptions {
    /** AUTH token. Sent eagerly after connect. Throws on rejection. */
    auth?: string;
}

export interface TcpOptions extends CommonOptions {
    transport: "tcp";
    host?: string;
    port?: number;
}

export interface WsOptions extends CommonOptions {
    transport: "ws";
    /** WebSocket URL, e.g. `ws://localhost:7780` or `wss://cuttledb.example.com`. */
    url: string;
    /** Optional WebSocket constructor (e.g. from the `ws` npm package on
     *  Node <22). Defaults to globalThis.WebSocket. */
    WebSocket?: any;
}

export type CuttleDBOptions = TcpOptions | WsOptions;

/** A change event pushed by the server in response to a SUB. */
export interface Event {
    hid: number;
    tid: number;
    rowId: number;
    /** "INS" | "DEL" | "UPD" — server-provided opcode. */
    op: string;
}

/** Column spec: `[name, typeCode]` for int/float/string, or
 *  `[name, 3, dim]` for vector columns. Type codes:
 *  0 = int, 1 = float, 2 = string, 3 = vec.  */
export type Column = [string, number] | [string, 3, number];

/** Row returned from `get` / `selectGt` — values are always strings. */
export type Row = string[];

/** Result of `knn` / `lsearch` / `bsearch` / `search` — pre-sorted by score
 *  descending. */
export interface KnnHit { rowId: number; score: number; }

/** Aggregation function for `groupBy`. */
export type AggFn = "count" | "sum" | "min" | "max" | "avg";

/** Options for `groupBy`. */
export interface GroupByOptions {
    /** Aggregation applied per group. Default "count". */
    agg?: AggFn;
    /** Numeric column to aggregate. Required unless agg is "count". */
    aggCol?: number;
    /** Extra grouping columns beyond `groupCol`. */
    by?: number | number[];
    /** Post-aggregation filter `[op, threshold]` (op = Op.*). */
    having?: [number, number];
    /** Sort `[field, dir]`; field "key"|0 / "value"|1, dir "asc"|0 / "desc"|1. */
    order?: ["key" | "value" | 0 | 1, "asc" | "desc" | 0 | 1];
    /** Cap returned groups after ordering. */
    limit?: number;
}

/** One aggregated group. `key` is scalar for a single grouping column, an
 *  array of components when `by` adds more columns; `value` is the aggregate. */
export interface GroupByResult { key: string | string[]; value: number; }

/** Options for `join`. */
export interface JoinOptions {
    /** "inner" (default) | "left" | "right" | "full". Outer joins pair an
     *  unmatched row with -1 (the NULL sentinel) on the other side. */
    how?: "inner" | "left" | "right" | "full";
    /** Join predicate (Op.*). Op.EQ (default) is a hash join; Op.GT / Op.LT
     *  are non-equi nested-loop joins. */
    op?: number;
}

/** Optional WHERE filter clause for `knn` / `search`. */
export interface SearchOptions {
    /** Phase-1 filter: "col OP value [AND col OP value ...]" (string values
     *  quoted, numeric bare, up to 8 AND'd predicates). */
    where?: string;
}

/** Result of `log` — cursor is the new high-water mark. */
export interface LogResult {
    cursor: number;
    events: { tsMs: number; rowId: number; op: string }[];
}

/** Generic key=value INFO/STATS response. */
export type InfoMap = Record<string, string>;

export class CuttleDB {
    constructor(options: CuttleDBOptions);

    connect(): Promise<void>;
    close(): void;

    /** Subscribe to push events. Returns an unsubscribe function. */
    on(eventName: "event", cb: (evt: Event) => void): () => void;

    // Wire-level
    send(cmd: string): Promise<string>;
    sendBatch(cmds: string[]): Promise<string[]>;

    // Server meta
    ping(): Promise<string>;
    hello(): Promise<string>;
    info(): Promise<InfoMap>;
    stats(hid?: number, tid?: number): Promise<InfoMap>;

    // DDL
    open(): Promise<number>;
    closeHandle(hid: number): Promise<void>;
    create(hid: number, name: string, columns: Column[]): Promise<number>;
    /** Build a secondary index. One `col` → a classic per-column index
     *  (queried by `find`); two or more cols → a *composite* index over their
     *  canonical-joined values (queried by `findc`). Returns rows indexed. */
    index(hid: number, tid: number, col: number, ...moreCols: number[]): Promise<number>;
    /** Add a column to an existing table. Returns new column index. */
    alterAdd(hid: number, tid: number, name: string, type: number, dim?: number): Promise<number>;
    /** Open a tx on this connection. */
    begin(): Promise<void>;
    /** Commit the open tx. */
    commit(): Promise<number>;
    /** Revert all mutations in the open tx. */
    rollback(): Promise<number>;
    /** Run fn inside a tx; commits on success, rolls back on throw. */
    transaction<T>(fn: (db: CuttleDB) => Promise<T>): Promise<T>;
    /** Find row IDs where col == value (uses the index if present). */
    find(hid: number, tid: number, col: number, value: string): Promise<number[]>;
    /** Composite point lookup: row IDs where every `cols[i] === values[i]`.
     *  O(1) average with a composite index over the same column list, O(N)
     *  scan otherwise. Values may contain spaces and commas. */
    findc(hid: number, tid: number, cols: number[], values: (string | number)[]): Promise<number[]>;

    // DML — write
    insert(hid: number, tid: number, values: (string | number | number[])[]): Promise<number>;
    insertBatch(hid: number, tid: number, rows: (string | number | number[])[][]): Promise<number[]>;
    /** Like `insert`, but client-side-encrypts cells at `encCols` (must be STRING
     *  columns) with `cipher`. The server stores only ciphertext. */
    insertEnc(hid: number, tid: number, values: (string | number | number[])[],
              cipher: FieldCipher, encCols: Iterable<number>): Promise<number>;
    /** Encrypted-column variant of `insertBatch`. */
    insertBatchEnc(hid: number, tid: number, rows: (string | number | number[])[][],
                   cipher: FieldCipher, encCols: Iterable<number>): Promise<number[]>;
    delete(hid: number, tid: number, rowId: number): Promise<boolean>;
    /** Set setCol=setVal for rows matching predicate. Returns rows updated. */
    updateWhere(hid: number, tid: number, setCol: number, setVal: number,
                predCol: number, op: number, threshold: number): Promise<number>;
    /** Set STRING column `col` on one row by physical rowId (UPDRS). Returns 1.
     *  `newVal` is wire-escaped; string/composite/BM25 indexes stay consistent
     *  and the change participates in transactions. */
    updateRowStr(hid: number, tid: number, rowId: number, col: number, newVal: string): Promise<number>;
    /** Set STRING column `setCol` to `setVal` for rows where
     *  `predCol {op} threshold` (UPDATES). Returns rows updated. */
    updateWhereStr(hid: number, tid: number, setCol: number, setVal: string,
                   predCol: number, op: number, threshold: number): Promise<number>;
    /** Delete rows matching predicate. Returns rows deleted. */
    deleteWhere(hid: number, tid: number, predCol: number, op: number,
                threshold: number): Promise<number>;

    // DML — read
    get(hid: number, tid: number, rowId: number): Promise<Row>;
    /** Like `get`, but decrypts cells at `encCols` with `cipher`. Non-ciphertext
     *  cells pass through unchanged. */
    getDec(hid: number, tid: number, rowId: number,
           cipher: FieldCipher, encCols: Iterable<number>): Promise<Row>;
    count(hid: number, tid: number): Promise<number>;
    sum(hid: number, tid: number, col: number): Promise<number>;
    min(hid: number, tid: number, col: number): Promise<number>;
    max(hid: number, tid: number, col: number): Promise<number>;
    fcountGt(hid: number, tid: number, col: number, threshold: number): Promise<number>;
    selectGt(hid: number, tid: number, col: number, threshold: number): Promise<Row[]>;
    /** Aggregate by one or more grouping columns. */
    groupBy(hid: number, tid: number, groupCol: number, opts?: GroupByOptions): Promise<GroupByResult[]>;
    /** 2-way join. Returns `[leftRowId, rightRowId]` pairs (-1 = unmatched
     *  outer side). */
    join(lHid: number, lTid: number, lCol: number, rHid: number, rTid: number,
         rCol: number, opts?: JoinOptions): Promise<[number, number][]>;

    // Vector / text / hybrid search
    /** Top-`k` nearest rows by cosine similarity. Pass `{ where }` to filter. */
    knn(hid: number, tid: number, col: number, k: number, query: number[],
        opts?: SearchOptions): Promise<KnnHit[]>;
    /** Top-`k` BM25 lexical matches over a STRING column. */
    lsearch(hid: number, tid: number, col: number, k: number, query: string): Promise<KnnHit[]>;
    /** Boolean-DSL retrieval (filters + optional vector/BM25 scoring atoms). */
    bsearch(hid: number, tid: number, k: number, expr: string): Promise<KnnHit[]>;
    /** Hybrid KNN + BM25 retrieval fused via Reciprocal Rank Fusion. Pass
     *  `{ where }` to filter both streams. */
    search(hid: number, tid: number, vecCol: number, textCol: number, k: number,
           vec: number[], query: string, opts?: SearchOptions): Promise<KnnHit[]>;

    // Persistence
    save(hid: number, path: string): Promise<string>;
    load(path: string): Promise<number>;

    // Subscriptions
    sub(hid: number, tid: number): Promise<string>;
    unsub(hid: number, tid: number): Promise<string>;

    // Change feed
    log(hid: number, tid: number, since?: number): Promise<LogResult>;
}

export const Op: {
    readonly GT: 0;
    readonly LT: 1;
    readonly EQ: 2;
};

/** AES-256-GCM cipher for client-side encryption of individual cell values.
 *  Build via the async factory (loads node:crypto once); encrypt/decrypt are
 *  then synchronous. Token format `enc:v1:<base64(iv||ct||tag)>` matches the
 *  Python adapter for cross-language round-trip. */
export class FieldCipher {
    constructor(key: Uint8Array, cryptoMod: any);
    /** Build a FieldCipher from a 32-byte key (loads node:crypto). */
    static create(key: Uint8Array): Promise<FieldCipher>;
    /** Fresh random 32-byte AES-256 key. */
    static generateKey(): Promise<Buffer>;
    /** True if the value is an `enc:v1:` ciphertext token. */
    static isEncrypted(token: unknown): boolean;
    /** Encrypt a string → `enc:v1:…` token. */
    encrypt(plaintext: string): string;
    /** Decrypt an `enc:v1:…` token → string; non-tokens pass through. */
    decrypt(token: string): string;
}

export class TcpTransport {
    constructor(opts: { host?: string; port?: number });
    connect(): Promise<void>;
    send(cmd: string): Promise<string>;
    sendBatch(cmds: string[]): Promise<string[]>;
    onEvent(cb: (evt: Event) => void): () => void;
    close(): void;
}

export class WsTransport {
    constructor(opts: { url: string; WebSocket?: any });
    connect(): Promise<void>;
    send(cmd: string): Promise<string>;
    sendBatch(cmds: string[]): Promise<string[]>;
    onEvent(cb: (evt: Event) => void): () => void;
    close(): void;
}
