// TypeScript declarations for Cluster.

import { CuttleDB, CuttleDBOptions, InfoMap } from "./cuttledb";

export interface ClusterOptions {
    auth?: string;
}

export interface PrimaryReplicasOptions extends ClusterOptions {
    primary: CuttleDBOptions;
    replicas: CuttleDBOptions[];
}

export class Cluster {
    readonly nodes: CuttleDB[];
    readonly size: number;
    readonly primary: CuttleDB;

    constructor(nodeOpts: CuttleDBOptions[], primaryOpts?: CuttleDBOptions | null);

    static withPrimaryAndReplicas(opts: PrimaryReplicasOptions): Promise<Cluster>;
    static sharded(shards: CuttleDBOptions[], opts?: ClusterOptions): Promise<Cluster>;

    connect(): Promise<void>;
    close(): void;

    /** Returns the next node in round-robin order. */
    readRoundRobin(): CuttleDB;

    /** Route a key to one node. Default hash is FNV-1a (stable across processes). */
    shardBy(key: unknown, fn?: (key: unknown, n: number) => number): CuttleDB;

    /** Run a write against every node. Throws if any fail. */
    writeToAll<T>(writeFn: (node: CuttleDB) => Promise<T>): Promise<(T | null)[]>;

    info(): Promise<InfoMap[]>;

    [Symbol.iterator](): IterableIterator<CuttleDB>;
}
