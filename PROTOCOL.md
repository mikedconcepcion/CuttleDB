# CuttleDB Wire Protocol

A simple line-based ASCII protocol. Inspired by Redis's inline-command
form. One command per line. One response per line.

## Transports

CuttleDB speaks the **same line protocol** over three transports. Pick
whichever fits your client.

| Transport | When to use | Notes |
|---|---|---|
| **TCP** | Servers, daemons, Python, Node CLI | Default. `cuttledb-server --port 7780`, connect with raw TCP. |
| **WebSocket** | Browsers, mobile apps, anywhere TCP is unavailable | Same port — server auto-detects HTTP upgrade. One WS text frame per command/response. |
| **In-process** | Embedded, WASM in browser (experimental) | `cuttledb_exec_line(line, outbuf, outlen)`. No socket. |

For raw TCP and WS, framing is identical at the message level:

- Client → Server: `VERB ARG1 ARG2 ARG3 ...\n` (any number of args, space-separated)
- Server → Client: `+OK <value>\r\n` (success) or `-ERR <message>\r\n` (failure)

Server accepts both `\n` and `\r\n` line endings. Server always emits `\r\n`.

A client may pipeline by sending multiple commands in one TCP packet —
all responses are batched in one packet back (server flushes only when
the recv buffer drains). WebSocket pipelining sends each command as one
text frame; responses are still grouped because the server only flushes
when its recv buffer drains.

## Durability (WAL)

If the server is started with `--wal-dir <path>`, every successful
mutation is appended to a write-ahead log for that handle before the
client sees its `+OK` response. On restart against the same directory,
state is recovered automatically.

```
cuttledb-server --port 7780 --wal-dir /var/lib/cuttledb
              --wal-sync interval=20      # default; alternatives: always, none
              --wal-checkpoint-mb 16      # default; snapshot+truncate at this size
```

Files in the WAL dir (per handle):

- `<hid>.cuttledb-wal` — append-only log of mutating wire-protocol lines,
  framed `[seq u32][len u32][payload][crc32 u32]`. The CRC catches
  partial writes from a crash; replay stops at the first bad frame.
- `<hid>.cuttledb-snap` — snapshot in OCTO v1 format, written when the
  WAL crosses the checkpoint threshold. On startup, the snapshot is
  loaded first and the WAL is replayed on top.

What gets logged:
`CREATE`, `INSERT`, `DELETE`, `DELW`, `UPDATE`, `INDEX`.
Not logged: read-only verbs (`GET`, `COUNT`, `SUM`, `MIN`, `MAX`,
`FCOUNT`, `SELGT`, `KNN`, `FIND`), connection meta (`PING`, `HELLO`,
`AUTH`, `INFO`, `STATS`), subscriptions (`SUB`, `UNSUB`, `LOG`),
operator commands (`SAVE`, `LOAD`, `OPEN`, `CLOSE`).

`SAVE` and `LOAD` remain manual export/import verbs. They do not
interact with the WAL — the auto-checkpoint uses its own snapshot file
in the WAL dir.

Without `--wal-dir`, the server runs in ephemeral mode (v0.4
behavior). All state is lost on shutdown.

## Authentication

If the server is started with `--auth <token>`, every new connection
starts **unauthenticated**. The only verbs accepted before AUTH are
`PING`, `HELLO`, and `AUTH` itself. Every other verb returns
`-ERR auth required`.

```
AUTH <token>
  → +OK authenticated         on match
  → -ERR auth failed          on mismatch
```

`HELLO` includes the literal `auth_required` token in its response when
the server is auth-gated, so clients can prompt for credentials before
issuing any commands:

```
HELLO
  → +OK cuttledb 0.3.0-dev proto 1                  (open server)
  → +OK cuttledb 0.3.0-dev proto 1 auth_required    (auth-gated server)
```

Without `--auth`, all connections are pre-authenticated for back-compat
with v0.2 deployments.

## STRING escape contract

STRING values may contain bytes that conflict with wire delimiters
(`,` inside row payload, `;` inside rowlist responses). Both the
client (on the way in) and the server (on the way out) backslash-
escape these bytes so the original payload round-trips losslessly.

**Five characters are escaped.** Implementations MUST match this set
or strings carrying these bytes will misalign column or row counts.

| Byte | Encoded as | Notes |
|---|---|---|
| `\` (backslash) | `\\` | Escape lead-in — must be first or it double-escapes later replacements |
| `,` (comma)     | `\,` | Column separator |
| `;` (semicolon) | `\;` | Row separator (rowlist responses only; harmless in single-row) |
| CR (`\r`)       | `\r` | Two characters: literal backslash + `r` |
| LF (`\n`)       | `\n` | Two characters: literal backslash + `n` |

Any other `\<X>` sequence decodes to literal `X` — decoders MUST treat
unknown escapes as "skip the backslash, take the next byte verbatim"
so older decoders continue to parse newer encoders without breaking.

The wire-contract test suite (`adapters/python/tests/test_wire_contract.py`
and `adapters/tests/wire_contract.mjs`) pins every escape character
end-to-end through `INSERT` → `GET` and `INSERT` → `SELECT_GT`. New
adapters MUST run an equivalent contract test before claiming
compatibility with v0.7+.

## Type tags (for `CREATE`)

| Code | Meaning |
|---|---|
| `0` | int (stored as double, returned as integer-valued string) |
| `1` | float (stored as double, returned as `%g`) |
| `2` | string (stored in server's string arena) |
| `3:dim` | vector — fixed `dim` f32 floats per row. SIMD cosine search via `KNN`. |

## Verbs

### `PING`

Connection health check. Always responds `+OK PONG`.

- Request:  `PING`
- Response: `+OK PONG`

### `HELLO`

Protocol version negotiation. Issue on connect to verify compatibility.

- Request:  `HELLO`
- Response: `+OK cuttledb <version> proto <proto_version>` &nbsp;·&nbsp; e.g. `+OK cuttledb 0.3.0-dev proto 1`

If your client requires a minimum protocol version, parse the `proto` field
and refuse to operate on older servers. Backward-incompatible protocol
changes bump `proto`; additive changes (new verbs) do not.

### `INFO`

Server-level summary in a single line of `key=value` pairs. Use for health
dashboards and monitoring.

- Request:  `INFO`
- Response: `+OK version=<v> uptime_ms=<n> handles=<n> tables=<n> rows=<n> events=<n>`

### `STATS [hid] [tid]`

Counters at three scopes:

- No args — aggregate across all handles:
  `+OK handles=N tables=N rows=N events=N`
- One arg (hid) — per-handle:
  `+OK hid=<hid> tables=N rows=N events=N`
- Two args (hid + tid) — per-table:
  `+OK hid=<hid> tid=<tid> name=<name> cols=N rows=N events=N subs_global=N`

`events` counts entries written to the change ring (used for `LOG` cursors).
`subs_global` is the total live TCP connections registered with the server.

### `OPEN`

Allocate a new database handle. Each handle holds up to 256 tables.

- Request:  `OPEN`
- Response: `+OK <hid>` where `<hid>` is a small non-negative integer.

### `CLOSE <hid>`

Release a handle. All of its tables, columns, and underlying buffers are
freed; the handle id may be reused by a later `OPEN`. Existing
subscriptions on tables of the closed handle stay registered on their
connections but become inert (broadcasts will miss).

- Request:  `CLOSE 0`
- Response: `+OK` (handle freed) or `-ERR bad` (unknown / already freed)

### `CREATE <hid> <name> <col1>:<type1>,<col2>:<type2>,...`

Create a table within a handle. Returns the table id (0-based within
the handle).

- Request:  `CREATE 0 users name:2,dept:2,salary:0`
- Response: `+OK 0`

### `INSERT <hid> <tid> <v1>,<v2>,<v3>`

Insert a row. Values are positional in column order. Numeric values
parsed as decimal integers. String values are raw text up to the next
`,` or end-of-line. Returns the new row's id.

- Request:  `INSERT 0 0 Alice,Eng,100000`
- Response: `+OK 0`

### `BEGIN` / `COMMIT` / `ROLLBACK`

Per-connection transactions. `BEGIN` opens a tx on the current
connection. Subsequent `INSERT` and `UPDATE` are buffered for atomic
WAL flush on `COMMIT`; `ROLLBACK` reverts them in-memory and discards
the buffer.

```
BEGIN              → +OK                          (or -ERR already in tx)
COMMIT             → +OK <num_ops>                (or -ERR not in tx)
ROLLBACK           → +OK <num_ops>                (or -ERR not in tx)
```

**Atomicity guarantee.** A COMMIT writes all the tx's mutations to the
WAL wrapped in `_TXB` / `_TXC` marker frames. On recovery, frames
between `_TXB` and `_TXC` are buffered and applied as a unit; a crash
between the markers leaves no `_TXC`, and replay discards the partial.

**Transactional verbs:**
- `INSERT`, `UPDATE` (bulk) — full support.
- `DELETE`, `DELW` — supported as of v0.5.1. The server serializes the
  deleted row plus the row that swap-with-last moved into its slot, so
  ROLLBACK restores both at their original positions.
- DDL (`CREATE`, `INDEX`, `ALTER`) — returns `-ERR ddl in tx`. Schema
  evolution inside a tx is a v1.0 target.
- A tx is scoped to a single handle. The first mutation pins the
  handle; mutating a different `hid` returns `-ERR tx cross-handle`.
- Tx size cap: 4096 mutations per tx. Exceeding returns `-ERR tx full`.
- No nested transactions. `BEGIN` while in tx returns
  `-ERR already in tx`.

### `ALTER <hid> <tid> ADD <name>:<type>[:dim]`

Add a column to an existing table. Returns the index of the new column.
Existing rows are backfilled with defaults: `0` for numeric, empty
string for string, zero-vector for VEC.

```
ALTER 0 0 ADD salary:0           → +OK 2
ALTER 0 0 ADD email:2            → +OK 3
ALTER 0 0 ADD emb:3:768          → +OK 4
```

Up to `CUTTLEDB_MAX_COLS` (32) columns per table.

### `INDEX <hid> <tid> <col>`

Build a secondary index on a string column. After this call, point
lookups via `FIND` are O(1) average instead of O(N). The index is
maintained automatically on `INSERT` and `DELETE` — no manual rebuild
needed. Idempotent: re-indexing an already-indexed column drops and
recreates the index.

- Request:  `INDEX 0 0 0`
- Response: `+OK 1247` (rows indexed)

**v0.5.0 limitations:**
- String columns only. Numeric/vec indexes are planned for v0.5.1.
- Index is in-memory; rebuilt by re-running `INDEX` after a `LOAD`.

### `FIND <hid> <tid> <col> <value>`

Point lookup: return all row IDs where `col == value`. Uses the
secondary index if present (O(1) avg), otherwise falls back to a linear
scan. The value runs to end-of-line and may contain spaces, but **not
commas** (the wire protocol uses `,` as the `INSERT` field delimiter).

- Request:  `FIND 0 0 0 alice`
- Response: `+OK [0;2;5]`

Empty result: `+OK []`.

### `UPDATE <hid> <tid> <set_col> <set_val> <pred_col> <op> <thr>`

Set `set_col = set_val` for every row where `pred_col {op} thr`.
`op`: `0`=GT, `1`=LT, `2`=EQ. Returns the number of rows updated.

Each updated row emits a `U` entry in the change log and a `>EVT … UPD`
broadcast to subscribers — same as if every row were updated individually.
Cached aggregates (`SUM`) are kept in sync.

- Request:  `UPDATE 0 0 1 999 1 0 30`  (set col1=999 where col1 > 30)
- Response: `+OK 5`

**v0.5.0 limitations:**
- Both `set_col` and `pred_col` must be numeric (int/float). String and
  VEC column updates are planned for v0.5.1.
- `set_val` and `thr` are parsed as integers and cast to double on store.
  Decimal literals require v0.5.1.

### `DELW <hid> <tid> <pred_col> <op> <thr>`

Delete every row where `pred_col {op} thr`. `op`: `0`=GT, `1`=LT, `2`=EQ.
Returns the number of rows deleted.

Each deletion emits a `D` log entry and `>EVT … DEL` broadcast. Server
collects matching row ids first, then deletes in descending order so that
swap-with-last doesn't skip any matching row.

- Request:  `DELW 0 0 1 0 100`  (delete where col1 > 100)
- Response: `+OK 3`

### `DELETE <hid> <tid> <row_id>`

Delete a single row by id (swap-with-last semantics; remaining row ids may
shift). Updates cached aggregates, writes a `D` entry to the change log,
and broadcasts a `>EVT … DEL` to subscribers.

- Request:  `DELETE 0 0 5`
- Response: `+OK 1` (row removed) or `+OK 0` (row id out of range / table missing)

Bulk `DELETE WHERE` is planned for v0.5.

### `GET <hid> <tid> <row_id>`

Fetch a row by id. Returns `<v1>,<v2>,<v3>` comma-joined.

- Request:  `GET 0 0 0`
- Response: `+OK Alice,Eng,100000`

For an out-of-range row id: `-ERR not found`.

### `COUNT <hid> <tid>`

Number of live rows in the table.

- Request:  `COUNT 0 0`
- Response: `+OK 2`

### `SUM <hid> <tid> <col>` &nbsp;·&nbsp; `MIN <hid> <tid> <col>` &nbsp;·&nbsp; `MAX <hid> <tid> <col>`

Aggregate a numeric column. `SUM` is O(1) (cached running sum maintained
on insert/update). `MIN`/`MAX` are O(N) with AVX2 SIMD reduction.

- Request:  `SUM 0 0 2`
- Response: `+OK 175000`

### `FCOUNT <hid> <tid> <col> <threshold>`

Count rows where the numeric column is **strictly greater than** the
threshold. AVX2 SIMD predicate scan.

- Request:  `FCOUNT 0 0 2 50000`
- Response: `+OK 2`

### `SELGT <hid> <tid> <col> <threshold>`

Return all rows where the numeric column is strictly greater than the
threshold, serialized as `[row1;row2;...]` with each row in the same
format as `GET`.

- Request:  `SELGT 0 0 2 80000`
- Response: `+OK [Alice,Eng,100000]`

An empty result is `+OK []`.

### `SUB <hid> <tid>` &nbsp;·&nbsp; `UNSUB <hid> <tid>`

Subscribe to real-time change events on a table. Server sends a
`>EVT` line for every INSERT/UPDATE/DELETE on the table.

- Request:  `SUB 0 0`
- Response: `+OK subscribed 0 0`
- Server pushes (async): `>EVT 0 0 5 INS` &nbsp; (hid, tid, row_id, op)

Op codes: `INS`, `DEL` (planned), `UPD` (planned).

Unsubscribe with `UNSUB <hid> <tid>` → `+OK unsubscribed`. Closing the
TCP connection clears all subscriptions.

### `LOG <hid> <tid> [since_count]`

Per-table change feed. Returns events from the ring buffer (last 1024
events kept). The cursor is the integer at the start of the response —
pass it as `since_count` to tail.

- Request:  `LOG 0 0`
- Response: `+OK 5 [ts:row_id:op;ts:row_id:op;...]`
  - `5` is `count_so_far` (total events ever; use as next cursor)
  - `ts` is process-monotonic milliseconds
  - `op` is `I` (insert), `D` (delete), `U` (update)
- Tail: `LOG 0 0 4` → only events with id ≥ 4

### `KNN <hid> <tid> <col> <k> <query_vec>`

Top-k cosine similarity over a VEC column. The query vector is
pipe-separated f32 floats matching the column's `dim`.

- Request:  `KNN 0 0 1 5 0.1|0.2|0.3|0.4`
- Response: `+OK [row_id:score;row_id:score;...]` (sorted by score desc)

For RAG / agent memory: store embeddings as VEC columns, query with
your query embedding, get top-k nearest rows by row_id, then `GET`
each row for full details.

### Errors

| Response | Meaning |
|---|---|
| `-ERR empty` | line had no content |
| `-ERR bad` | malformed args or out-of-range ids |
| `-ERR not found` | row id out of range |

## Pipelining

```
Client sends (single packet):
  OPEN
  CREATE 0 t v:0
  INSERT 0 0 100
  INSERT 0 0 200
  SUM 0 0 0
  \n

Server responds (single packet):
  +OK 0
  +OK 0
  +OK 0
  +OK 1
  +OK 300
  \r\n
```

The server batches responses internally until its recv buffer drains,
then flushes in one syscall. With `TCP_NODELAY` on the accepted client,
that's one packet of N responses.

## Limits (v0.1)

- 256 tables per handle, 32 columns per table.
- Single TCP client at a time on the open-source line-protocol server.
  Embedded mode and pipelining work normally.
- 64KB max line length per command (recv buffer).
- 64KB max response payload per write (output buffer; large `SELGT`
  results are flushed mid-stream).
