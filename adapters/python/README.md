# cuttledb — Python client for CuttleDB

```bash
pip install cuttledb
```

```python
from cuttledb import CuttleDB, ColType

with CuttleDB.connect("127.0.0.1", 7780) as db:
    hid = db.open()
    tid = db.create(hid, "memory", [
        ("text",      ColType.STRING),
        ("embedding", ColType.VEC, 768),
    ])

    db.insert(hid, tid, ["hello world", [0.1] * 768])

    hits = db.knn(hid, tid, col=1, k=5, query=[0.1] * 768)
    for row_id, score in hits:
        print(score, db.get(hid, tid, row_id))
```

See the full feature set and use cases in the
[CuttleDB project README](https://github.com/mikedconcepcion/CuttleDB#readme).

## API surface

| Method | Returns | Notes |
|---|---|---|
| `CuttleDB.connect(host, port)` | `CuttleDB` | classmethod, returns connected client; use as context manager |
| `db.open()` | `int` | new handle id |
| `db.create(hid, name, [(col, type), …])` | `int` | new table id |
| `db.insert(hid, tid, values)` | `int` | new row id |
| `db.insert_batch(hid, tid, rows)` | `list[int]` | pipelined; bulk-load 1000 rows in <40ms |
| `db.get(hid, tid, row_id)` | `list[str]` | values returned as strings (no type coercion) |
| `db.count(hid, tid)` | `int` | O(1) |
| `db.sum / .min / .max(hid, tid, col)` | `float` | SUM O(1); MIN/MAX SIMD |
| `db.fcount_gt(hid, tid, col, threshold)` | `int` | SIMD predicate scan |
| `db.select_gt(hid, tid, col, threshold)` | `list[list[str]]` | all matching rows |
| `db.knn(hid, tid, col, k, query)` | `list[(row_id, score)]` | cosine, sorted desc |
| `db.delete(hid, tid, row_id)` | `bool` | swap-with-last |
| `db.save(hid, path)` / `db.load(path)` | `str` / `int` | binary snapshot |
| `db.sub(hid, tid)` / `db.unsub(hid, tid)` | `str` | register/cancel push subscription |
| `db.poll_events(timeout)` | `list[Event]` | drain pending push events |
| `with db.stream_events() as events: …` | iterator | generator-style event loop |
| `db.log(hid, tid, since=0)` | `(cursor, events)` | per-table change ring buffer |
| `db.ping() / db.hello() / db.info() / db.stats(...)` | `str` / `dict` | server meta |

All methods raise `CuttleDBError` on `-ERR …` responses or protocol violations.

## Concurrency model

`CuttleDB` is **thread-compatible but not thread-safe** — calls serialize
through an internal lock, so multiple threads on one connection won't
corrupt the wire protocol, but they will not run in parallel either. For
parallel workloads, open one connection per worker. The server is
multi-client (thread-per-connection) so this scales.

## Subscriptions without a background thread

The Python SDK is intentionally single-threaded. For real-time push:

```python
db.sub(hid, tid)

# blocking loop with a poll interval
while True:
    for evt in db.poll_events(timeout=1.0):
        handle(evt)
```

Or the generator form:

```python
with db.stream_events(poll_interval=0.1) as events:
    for evt in events:
        handle(evt)
```

If you need true background push (callbacks fired from a reader thread),
wrap `poll_events` in your own thread — it's three lines.
