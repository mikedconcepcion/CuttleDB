// Type declarations for the embeddable WASM entry point (`cuttledb/wasm`).
import { CuttleDB } from "../cuttledb.js";

export { CuttleDB };

/** A CuttleDB SDK instance booted in-process, with snapshot loading attached. */
export type WasmDB = CuttleDB & {
  /**
   * Mount a pre-built snapshot into the engine's virtual FS and LOAD it.
   * Returns the handle id the snapshot was loaded into.
   */
  loadSnapshot(bytes: Uint8Array | ArrayBuffer, name?: string): Promise<number>;
};

/** In-process transport that runs wire lines against the WASM engine. */
export class WasmTransport {
  constructor(module: unknown);
  connect(): Promise<void>;
  send(cmd: string): Promise<string>;
  sendBatch(cmds: string[]): Promise<string[]>;
  close(): void;
  onEvent(cb: (evt: unknown) => void): () => void;
  writeFile(name: string, bytes: Uint8Array): void;
}

/**
 * Boot the CuttleDB engine in-process (WebAssembly) and return a connected SDK
 * instance. `opts` is forwarded to the Emscripten module factory
 * (e.g. `{ locateFile }` to host the `.wasm` somewhere non-default).
 */
export function connect(opts?: Record<string, unknown>): Promise<WasmDB>;
