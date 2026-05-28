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

/** Result of `knn` — pre-sorted by score descending. */
export interface KnnHit { rowId: number; score: number; }

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
    /** Build a secondary index on a string column. */
    index(hid: number, tid: number, col: number): Promise<number>;
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

    // DML — write
    insert(hid: number, tid: number, values: (string | number | number[])[]): Promise<number>;
    insertBatch(hid: number, tid: number, rows: (string | number | number[])[][]): Promise<number[]>;
    delete(hid: number, tid: number, rowId: number): Promise<boolean>;
    /** Set setCol=setVal for rows matching predicate. Returns rows updated. */
    updateWhere(hid: number, tid: number, setCol: number, setVal: number,
                predCol: number, op: number, threshold: number): Promise<number>;
    /** Delete rows matching predicate. Returns rows deleted. */
    deleteWhere(hid: number, tid: number, predCol: number, op: number,
                threshold: number): Promise<number>;

    // DML — read
    get(hid: number, tid: number, rowId: number): Promise<Row>;
    count(hid: number, tid: number): Promise<number>;
    sum(hid: number, tid: number, col: number): Promise<number>;
    min(hid: number, tid: number, col: number): Promise<number>;
    max(hid: number, tid: number, col: number): Promise<number>;
    fcountGt(hid: number, tid: number, col: number, threshold: number): Promise<number>;
    selectGt(hid: number, tid: number, col: number, threshold: number): Promise<Row[]>;

    // Vector search
    knn(hid: number, tid: number, col: number, k: number, query: number[]): Promise<KnnHit[]>;

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
