"""CuttleDB Python SDK — synchronous client for the CuttleDB wire protocol.

Quick start::

    from cuttledb import CuttleDB, ColType

    with CuttleDB.connect("127.0.0.1", 7780) as db:
        hid = db.open()
        tid = db.create(hid, "users", [
            ("name",   ColType.STRING),
            ("salary", ColType.INT),
        ])
        db.insert(hid, tid, ["Alice", 100])
        print(db.count(hid, tid), db.sum(hid, tid, 1))

This package speaks the CuttleDB wire protocol described in PROTOCOL.md.
It connects via TCP only — for in-browser (WASM) use, see the JS adapter.

Design notes:
  * No background thread. ``send`` and ``send_batch`` are synchronous: write
    one packet, read one packet of responses. Server-pushed ``>EVT`` lines
    are drained via ``poll_events(timeout)``.
  * Zero non-stdlib dependencies. Works on Python 3.8+.
  * The wire protocol is line-based ASCII; this client never base64-encodes
    or json-encodes anything. Strings with commas in INSERT need to be
    handled at the application level (the protocol uses ``,`` as separator).
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Union

__version__ = "0.9.0"
__all__ = [
    "CuttleDB",
    "CuttleDBError",
    "ColType",
    "Op",
    "Column",
    "Event",
    "FieldCipher",
    "datetime_to_epoch_ms",
    "epoch_ms_to_datetime",
]


def __getattr__(name: str):
    # PEP 562 lazy import: keep the base package zero-dependency. FieldCipher
    # lives in cuttledb.crypto, which needs the optional `cuttledb[crypto]`
    # extra; importing it here only when actually referenced means a plain
    # `import cuttledb` never pulls in `cryptography`.
    if name == "FieldCipher":
        from .crypto import FieldCipher
        return FieldCipher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class CuttleDBError(RuntimeError):
    """Raised when the server returns ``-ERR …`` or a protocol violation occurs."""


class ColType(IntEnum):
    INT      = 0
    FLOAT    = 1
    STRING   = 2
    VEC      = 3  # use Column.vec(name, dim) helper, or 3-tuple (name, VEC, dim)
    DATETIME = 4  # int64 epoch ms UTC. INSERT/predicate accept ISO 8601 string
                  # ("2026-05-25T14:30:00Z") OR raw epoch ms. GET returns ISO 8601.
                  # Use datetime_to_epoch_ms / epoch_ms_to_datetime helpers below.


def datetime_to_epoch_ms(dt) -> int:
    """Convert a Python datetime.datetime to int64 epoch milliseconds.

    Naive datetimes are assumed UTC (no tz conversion). Aware datetimes
    are converted via their tzinfo. Use this when handing off datetime
    objects to CuttleDB INSERT / UPDATE / WHERE values.
    """
    import datetime as _dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_datetime(ms: int):
    """Convert int64 epoch milliseconds back to a UTC datetime.datetime.

    CuttleDB stores DATETIME as UTC; this returns a tz-aware UTC datetime.
    For local time, call .astimezone() on the result.
    """
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)


class Op(IntEnum):
    """Predicate comparison operators for fcount_gt / select_gt / update_where /
    delete_where. Numeric values are stable wire-protocol contracts."""
    GT = 0
    LT = 1
    EQ = 2


# ── Column spec helpers ──────────────────────────────────────────────────

ColumnSpec = Union[
    Tuple[str, int],        # (name, type)
    Tuple[str, int, int],   # (name, VEC, dim)
    "Column",
]


@dataclass(frozen=True)
class Column:
    name: str
    type: int                              # ColType value
    dim: int = 0                           # only meaningful for VEC
    max_bytes: int = 0                     # STR body-discipline: 0 = unlimited
    prefixes: Tuple[str, ...] = ()         # STR body-discipline: () = no prefix check

    @classmethod
    def vec(cls, name: str, dim: int) -> "Column":
        return cls(name=name, type=int(ColType.VEC), dim=dim)

    def encode(self) -> str:
        if self.type == int(ColType.VEC):
            return f"{self.name}:3:{self.dim}"
        spec = f"{self.name}:{self.type}"
        if self.max_bytes > 0:
            spec += f":MAX={self.max_bytes}"
        if self.prefixes:
            spec += ":PREFIX=" + "|".join(self.prefixes)
        return spec


def _encode_columns(cols: Iterable[ColumnSpec]) -> str:
    parts: List[str] = []
    for c in cols:
        if isinstance(c, Column):
            parts.append(c.encode())
        elif len(c) == 2:
            name, ty = c
            parts.append(f"{name}:{int(ty)}")
        elif len(c) == 3:
            name, ty, dim = c
            if int(ty) != int(ColType.VEC):
                raise ValueError(f"3-tuple column spec is for VEC only: {c!r}")
            parts.append(f"{name}:3:{int(dim)}")
        else:
            raise ValueError(f"bad column spec: {c!r}")
    return ",".join(parts)


def _encode_value(v: object) -> str:
    """Encode a single column value for the wire protocol.

    - lists/tuples → pipe-separated (vector columns)
    - strings → backslash-escape ``\\``, ``,``, ``\\r``, ``\\n`` so the wire
      parser can recover the original bytes (v0.5.2 protocol extension)
    - everything else → ``str()`` (numerics have no special chars)
    """
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v)
    s = str(v)
    # Order matters — escape backslash first or we'd double-escape later replacements.
    s = s.replace("\\", "\\\\")
    s = s.replace(",",  "\\,")
    s = s.replace("\r", "\\r")
    s = s.replace("\n", "\\n")
    return s


def _split_wire_row(s: str) -> List[str]:
    """Split a wire row on bare commas, honoring backslash escapes for
    string columns. Inverse of the server's wire_str_encode."""
    out: List[str] = []
    cur: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            esc = s[i + 1]
            if   esc == "r": cur.append("\r")
            elif esc == "n": cur.append("\n")
            else:            cur.append(esc)   # literal: comma, backslash, etc.
            i += 2
        elif c == ",":
            out.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    out.append("".join(cur))
    return out


def _parse_gb_component(s: str, i: int) -> Tuple[object, int, str]:
    """Parse one GROUPBY key component starting at index ``i``.

    Returns ``(value, next_index, terminator)`` where ``terminator`` is
    ``"|"`` (another key component follows), ``":"`` (key ends, value
    follows), or ``""`` (end of string). Quoted components decode to
    ``str``; bare components decode to ``int`` then ``float`` then ``str``.
    """
    if i < len(s) and s[i] == '"':
        end = s.find('"', i + 1)
        if end < 0:
            return s[i + 1:], len(s), ""
        comp: object = s[i + 1:end]
        nxt = end + 1
        term = s[nxt] if nxt < len(s) else ""
        return comp, nxt + 1, term
    j = i
    while j < len(s) and s[j] not in "|:":
        j += 1
    raw = s[i:j]
    try:
        comp = int(raw)
    except ValueError:
        try:
            comp = float(raw)
        except ValueError:
            comp = raw
    term = s[j] if j < len(s) else ""
    return comp, j + 1, term


def _encode_exec_str(s: str) -> str:
    """Encode a string for EXEC string-kernel args. The wire splits args
    on the first unescaped ``;``, so semicolons must be escaped. ``\\``,
    ``\\r``, and ``\\n`` are escaped for clean round-trip. Comma is *not*
    escaped — EXEC args have no comma delimiter (unlike row INSERT)."""
    out = s.replace("\\", "\\\\")
    out = out.replace(";", "\\;")
    out = out.replace("\r", "\\r")
    out = out.replace("\n", "\\n")
    return out


def _decode_exec_str(s: str) -> str:
    """Inverse of _encode_exec_str. The server already decodes the input
    side; this helper exists for tests + symmetry on the return path
    when the server's wire_str_encode wrapped the response."""
    out: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            esc = s[i + 1]
            if   esc == "r": out.append("\r")
            elif esc == "n": out.append("\n")
            else:            out.append(esc)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ── Event model ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    """Push event sent by the server in response to a SUB on a table.

    Wire format: ``>EVT <hid> <tid> <row_id> <op>``.
    """
    hid: int
    tid: int
    row_id: int
    op: str  # "INS", "DEL", "UPD"


# ── Client ──────────────────────────────────────────────────────────────

class CuttleDB:
    """Synchronous CuttleDB client.

    Use ``CuttleDB.connect(host, port)`` (preferred) or construct with
    ``CuttleDB(host, port)`` and call ``connect()`` explicitly.

    All blocking I/O goes through a single TCP socket. The class is
    thread-compatible but not thread-safe: wrap calls in a lock if you
    intend to share one instance across threads. For multi-threaded
    workloads, open one ``CuttleDB`` per thread.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7780,
        timeout: float = 10.0,
        auth: Optional[str] = None,
        transport: str = "tcp",
        tls_verify: bool = True,
        tls_ca_file: Optional[str] = None,
        tls_server_hostname: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auth = auth
        self.transport = transport
        self.tls_verify = tls_verify
        self.tls_ca_file = tls_ca_file
        self.tls_server_hostname = tls_server_hostname or host
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._pending_events: List[Event] = []
        self._lock = threading.RLock()

    # ── Connection management ────────────────────────────────────────

    @classmethod
    def connect(
        cls,
        host: str = "127.0.0.1",
        port: int = 7780,
        timeout: float = 10.0,
        auth: Optional[str] = None,
        transport: str = "tcp",
        tls_verify: bool = True,
        tls_ca_file: Optional[str] = None,
        tls_server_hostname: Optional[str] = None,
    ) -> "CuttleDB":
        """Factory: construct, connect, and (if ``auth`` given) authenticate.

        ``transport="tls"`` wraps the socket via stdlib ``ssl`` and performs
        a TLS handshake before the wire protocol resumes. Requires the
        server to be started with ``--tls-cert`` / ``--tls-key`` on an
        ``CUTTLEDB_WITH_TLS=1`` build.

        For self-signed certs (development), pass ``tls_verify=False``.
        For pinned CA, pass ``tls_ca_file=/path/to/ca.pem``.

        Raises ``CuttleDBError`` if the server requires auth and either no token
        was supplied or the token was rejected."""
        db = cls(
            host=host, port=port, timeout=timeout, auth=auth,
            transport=transport, tls_verify=tls_verify,
            tls_ca_file=tls_ca_file, tls_server_hostname=tls_server_hostname,
        )
        db._connect()
        return db

    def _connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # v1.0.4 — wrap in TLS if requested. Done AFTER raw TCP connect so
        # SO_REUSEADDR / TCP_NODELAY semantics on the underlying socket are
        # the same in either transport. The ssl module returns a wrapped
        # socket; we replace self._sock with it.
        if self.transport == "tls":
            import ssl
            if self.tls_verify:
                ctx = ssl.create_default_context()
                if self.tls_ca_file:
                    ctx.load_verify_locations(self.tls_ca_file)
            else:
                # Development / self-signed path. Explicitly opt out of all
                # validation; producing this exact code shape in a docs-
                # visible spot so reviewers see the trade-off.
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=self.tls_server_hostname)

        self._sock = s
        if self.auth is not None:
            # send AUTH eagerly so the rest of the session is open
            self.send(f"AUTH {self.auth}")

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._sock.close()
                self._sock = None

    def __enter__(self) -> "CuttleDB":
        if self._sock is None:
            self._connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Wire-level send/recv ─────────────────────────────────────────

    def _write(self, data: bytes) -> None:
        if self._sock is None:
            raise CuttleDBError("not connected")
        self._sock.sendall(data)

    def _readline(self) -> bytes:
        """Read one line ending in \\n. Returns the line without the trailing \\r\\n."""
        if self._sock is None:
            raise CuttleDBError("not connected")
        while b"\n" not in self._buf:
            chunk = self._sock.recv(8192)
            if not chunk:
                raise CuttleDBError("connection closed by server")
            self._buf += chunk
        idx = self._buf.index(b"\n")
        line = self._buf[:idx]
        self._buf = self._buf[idx + 1:]
        if line.endswith(b"\r"):
            line = line[:-1]
        return line

    def _read_response(self) -> str:
        """Read one server line, routing >EVT lines into the event queue."""
        while True:
            line = self._readline().decode("utf-8")
            if line.startswith(">EVT "):
                self._pending_events.append(_parse_event(line))
                continue
            return line

    def send(self, command: str) -> str:
        """Send one command, return one response line (sans ``+OK ``/``-ERR `` prefix
        already consumed). Raises ``CuttleDBError`` on ``-ERR …``.
        """
        with self._lock:
            self._write((command + "\n").encode("utf-8"))
            return self._unwrap(self._read_response())

    def send_raw(self, command: str) -> str:
        """Like ``send`` but returns the full line including ``+OK ``/``-ERR `` prefix
        without raising on errors. Useful for diagnostics.
        """
        with self._lock:
            self._write((command + "\n").encode("utf-8"))
            return self._read_response()

    def send_batch(self, commands: Sequence[str]) -> List[str]:
        """Pipeline: send all commands in one packet, return their responses in order."""
        if not commands:
            return []
        payload = ("\n".join(commands) + "\n").encode("utf-8")
        with self._lock:
            self._write(payload)
            return [self._unwrap(self._read_response()) for _ in commands]

    @staticmethod
    def _unwrap(line: str) -> str:
        if line.startswith("+OK"):
            return line[4:] if len(line) > 3 else ""
        if line.startswith("-ERR "):
            raise CuttleDBError(line[5:])
        raise CuttleDBError(f"unexpected response: {line!r}")

    # ── Server meta ──────────────────────────────────────────────────

    def ping(self) -> str:
        return self.send("PING")

    def hello(self) -> str:
        """Returns the server identification line, e.g. ``cuttledb 0.3.0 proto 1``."""
        return self.send("HELLO")

    def info(self) -> dict:
        line = self.send("INFO")
        return _parse_kv(line)

    def stats(self, hid: Optional[int] = None, tid: Optional[int] = None) -> dict:
        if hid is None:
            return _parse_kv(self.send("STATS"))
        if tid is None:
            return _parse_kv(self.send(f"STATS {hid}"))
        return _parse_kv(self.send(f"STATS {hid} {tid}"))

    # ── DDL ──────────────────────────────────────────────────────────

    def open(self) -> int:
        """Allocate a new database handle, return its id."""
        return int(self.send("OPEN"))

    def close_handle(self, hid: int) -> None:
        """Release a handle and free its tables/columns. The hid may be reused
        by a later ``open()``. Idempotent at the network level — re-closing a
        freed handle returns ``-ERR bad``, raised here as ``CuttleDBError``."""
        self.send(f"CLOSE {hid}")

    def create(self, hid: int, name: str, columns: Iterable[ColumnSpec]) -> int:
        """Create a table within the handle, return its table id (0-based)."""
        spec = _encode_columns(columns)
        return int(self.send(f"CREATE {hid} {name} {spec}"))

    def alter_add(
        self,
        hid: int,
        tid: int,
        name: str,
        type: int,
        dim: int = 0,
        max_bytes: int = 0,
        prefixes: Iterable[str] = (),
    ) -> int:
        """Add a column to an existing table. Returns the new column index.

        Existing rows are backfilled with defaults: ``0`` for numeric,
        empty string for string, zero-vector for VEC. ``dim`` is required
        when ``type == ColType.VEC``.

        STR-only body-discipline constraints: ``max_bytes`` caps value
        length; ``prefixes`` requires values to start with one of the
        listed strings. Backfilled defaults are not retroactively checked."""
        if int(type) == int(ColType.VEC):
            spec = f"{name}:3:{int(dim)}"
        else:
            spec = f"{name}:{int(type)}"
            if max_bytes > 0:
                spec += f":MAX={int(max_bytes)}"
            prefix_list = list(prefixes)
            if prefix_list:
                spec += ":PREFIX=" + "|".join(prefix_list)
        return int(self.send(f"ALTER {hid} {tid} ADD {spec}"))

    # ── Transactions ────────────────────────────────────────────────

    def begin(self) -> None:
        """Open a transaction on this connection. Subsequent mutations on a
        single handle become atomic — ROLLBACK reverts them, COMMIT durably
        applies them as one WAL batch.

        As of v0.8.0 the full mutation set is transactional, including DELETE
        (v0.5.1) and DDL — CREATE / INDEX / ALTER (v0.8.0). DDL is reverted on
        ROLLBACK: a tx-created table or tx-added column is dropped, and an
        index built in-tx is reverted to the pre-tx index (or none). Reads
        inside the tx see your own uncommitted writes. Tx is scoped to a
        single handle — the first mutation pins it; mutating a different hid
        returns ``-ERR tx cross-handle``. Tx size cap: 4096 operations."""
        self.send("BEGIN")

    def commit(self) -> int:
        """Commit the open tx. Returns the number of operations committed.
        Raises ``CuttleDBError`` if not in a transaction."""
        return int(self.send("COMMIT"))

    def rollback(self) -> int:
        """Revert all mutations in the open tx. Returns the count reverted."""
        return int(self.send("ROLLBACK"))

    from contextlib import contextmanager as _ctx

    @_ctx
    def transaction(self):
        """Context manager: ``with db.transaction(): ...``. Commits on clean
        exit, rolls back on exception."""
        self.begin()
        try:
            yield self
        except Exception:
            try: self.rollback()
            except CuttleDBError: pass
            raise
        else:
            self.commit()

    def index(self, hid: int, tid: int, col: int, *more_cols: int) -> int:
        """Build a secondary index. With a single ``col`` (string column) this
        is the classic per-column index queried by :meth:`find`. Pass two or
        more columns to build a *composite* index over their canonical-joined
        values, queried by :meth:`findc` for O(1) multi-column exact lookups
        (numeric and string columns may participate). Returns the number of
        rows indexed. Idempotent — rebuilding the same column list drops and
        recreates it. Maintained automatically on insert/delete."""
        cols = " ".join(str(c) for c in (col, *more_cols))
        return int(self.send(f"INDEX {hid} {tid} {cols}"))

    def find(self, hid: int, tid: int, col: int, value: str) -> List[int]:
        """Return all row IDs where ``col == value``. O(1) if the column has
        a secondary index built via :meth:`index`; O(N) linear scan otherwise.
        ``value`` runs to end-of-line and may contain spaces, but not commas
        (the wire protocol uses ``,`` as the INSERT field separator)."""
        body = self.send(f"FIND {hid} {tid} {col} {value}")
        if not body.startswith("[") or not body.endswith("]"):
            raise CuttleDBError(f"bad find result: {body!r}")
        inner = body[1:-1]
        if not inner:
            return []
        return [int(x) for x in inner.split(";")]

    def findc(self, hid: int, tid: int, cols: Sequence[int],
              values: Sequence[object]) -> List[int]:
        """Composite point lookup: return all row IDs where every
        ``cols[i] == values[i]``. O(1) average when a composite index over the
        same column list exists (build via ``index(hid, tid, *cols)``);
        otherwise an O(N) scan. Values are joined with the 0x1f unit separator
        so they may contain spaces and commas. Numeric values round-trip
        through the same canonicalization as stored cells."""
        if len(cols) != len(values):
            raise CuttleDBError("findc: cols and values length mismatch")
        col_list = " ".join(str(c) for c in cols)
        val_block = "\x1f".join(str(v) for v in values)
        body = self.send(f"FINDC {hid} {tid} {len(cols)} {col_list} {val_block}")
        if not body.startswith("[") or not body.endswith("]"):
            raise CuttleDBError(f"bad findc result: {body!r}")
        inner = body[1:-1]
        if not inner:
            return []
        return [int(x) for x in inner.split(";")]

    # ── DML — write ──────────────────────────────────────────────────

    def insert(self, hid: int, tid: int, values: Sequence[object]) -> int:
        """Insert one row, return the new row id."""
        csv = ",".join(_encode_value(v) for v in values)
        return int(self.send(f"INSERT {hid} {tid} {csv}"))

    def insert_batch(self, hid: int, tid: int, rows: Iterable[Sequence[object]]) -> List[int]:
        """Bulk insert via pipelining. Returns the list of new row ids."""
        cmds = [
            f"INSERT {hid} {tid} " + ",".join(_encode_value(v) for v in row)
            for row in rows
        ]
        return [int(r) for r in self.send_batch(cmds)]

    @staticmethod
    def _enc_row(values: Sequence[object], cipher, enc_cols) -> List[object]:
        """Return a copy of ``values`` with the cells at ``enc_cols`` replaced
        by their ``enc:v1:`` ciphertext tokens. Server-bound only — these are
        ordinary STRING values once encrypted."""
        cols = set(enc_cols)
        return [cipher.encrypt(v) if i in cols else v
                for i, v in enumerate(values)]

    def insert_enc(self, hid: int, tid: int, values: Sequence[object],
                   cipher: "FieldCipher", enc_cols: Iterable[int]) -> int:
        """Like :meth:`insert`, but client-side-encrypts the cells whose column
        indices are in ``enc_cols`` before they leave the process. Those
        columns must be STRING. The server stores only ciphertext; read the
        row back with :meth:`get_dec` (same ``cipher`` + ``enc_cols``)."""
        return self.insert(hid, tid, self._enc_row(values, cipher, enc_cols))

    def insert_batch_enc(self, hid: int, tid: int,
                         rows: Iterable[Sequence[object]],
                         cipher: "FieldCipher",
                         enc_cols: Iterable[int]) -> List[int]:
        """Encrypted-column variant of :meth:`insert_batch`."""
        cols = list(enc_cols)
        return self.insert_batch(
            hid, tid, (self._enc_row(r, cipher, cols) for r in rows)
        )

    def delete(self, hid: int, tid: int, row_id: int) -> bool:
        """Delete a row by id. Returns True if a row was removed."""
        return int(self.send(f"DELETE {hid} {tid} {row_id}")) == 1

    def update_row(
        self,
        hid: int,
        tid: int,
        row_id: int,
        col: int,
        new_val: int,
    ) -> int:
        """Set a single row's numeric cell by physical ``row_id``. Returns 1
        on success, raises ``CuttleDBError`` on bad inputs.

        Precise alternative to :meth:`update_where` when the caller already
        knows the row id and wants to avoid predicate-match ambiguity (e.g.
        bumping a counter where multiple rows may share the same old value).
        """
        return int(self.send(
            f"UPDR {hid} {tid} {row_id} {col} {int(new_val)}"
        ))

    def update_where(
        self,
        hid: int,
        tid: int,
        set_col: int,
        set_val: float,
        pred_col: int,
        op: int,
        threshold: float,
    ) -> int:
        """Set ``set_col`` to ``set_val`` for every row where
        ``pred_col {op} threshold``. Returns the number of rows updated.

        ``op`` is one of ``Op.GT`` / ``Op.LT`` / ``Op.EQ``. Both ``set_col``
        and ``pred_col`` must be numeric columns in v0.5.0; string/vec
        column updates are planned for v0.5.1.

        Each updated row emits a ``U`` event in the change log and a
        ``>EVT … UPD`` broadcast to subscribers — exactly as if each row
        were updated individually."""
        return int(self.send(
            f"UPDATE {hid} {tid} {set_col} {int(set_val)} "
            f"{pred_col} {int(op)} {int(threshold)}"
        ))

    def update_row_str(
        self,
        hid: int,
        tid: int,
        row_id: int,
        col: int,
        new_val: str,
    ) -> int:
        """Set a single STRING cell by physical ``row_id`` (UPDRS, v0.8.0).
        Returns 1 on success, raises ``CuttleDBError`` on bad inputs (e.g.
        ``col`` is not a STRING column).

        The string sibling of :meth:`update_row`. ``new_val`` is wire-escaped
        (``\\``, ``,``, CR, LF) so embedded commas and newlines round-trip.
        Secondary string indexes, composite indexes covering ``col`` and any
        BM25 index on ``col`` are kept consistent; the change participates in
        transactions (``BEGIN``/``ROLLBACK`` restores the prior value)."""
        return int(self.send(
            f"UPDRS {hid} {tid} {row_id} {col} {_encode_value(new_val)}"
        ))

    def update_where_str(
        self,
        hid: int,
        tid: int,
        set_col: int,
        set_val: str,
        pred_col: int,
        op: int,
        threshold: float,
    ) -> int:
        """Set STRING column ``set_col`` to ``set_val`` for every row where
        ``pred_col {op} threshold`` (UPDATES, v0.8.0). Returns the number of
        rows updated.

        The string sibling of :meth:`update_where`. ``set_col`` must be a
        STRING column; ``pred_col`` must be numeric. ``op`` is one of
        ``Op.GT`` / ``Op.LT`` / ``Op.EQ``. ``set_val`` is wire-escaped so
        embedded commas/newlines round-trip. Index maintenance and
        transaction semantics match :meth:`update_row_str`."""
        return int(self.send(
            f"UPDATES {hid} {tid} {set_col} {pred_col} "
            f"{int(op)} {int(threshold)} {_encode_value(set_val)}"
        ))

    def delete_where(
        self,
        hid: int,
        tid: int,
        pred_col: int,
        op: int,
        threshold: float,
    ) -> int:
        """Delete every row where ``pred_col {op} threshold``. Returns the
        number of rows deleted. Each deletion emits a ``D`` log entry and
        a ``>EVT … DEL`` broadcast.

        Internally the server collects matching row ids first, then deletes
        in descending order so that the swap-with-last index shift doesn't
        skip any matching row."""
        return int(self.send(
            f"DELW {hid} {tid} {pred_col} {int(op)} {int(threshold)}"
        ))

    # ── DML — read ───────────────────────────────────────────────────

    def get(self, hid: int, tid: int, row_id: int) -> List[str]:
        """Fetch a row by id. Returns its values as strings (no type coercion).

        STR values are wire-escape-decoded (``\\,`` → ``,``, ``\\r`` → CR, etc.)
        so the original Python string round-trips even with embedded commas."""
        return _split_wire_row(self.send(f"GET {hid} {tid} {row_id}"))

    def get_dec(self, hid: int, tid: int, row_id: int,
                cipher: "FieldCipher", enc_cols: Iterable[int]) -> List[str]:
        """Like :meth:`get`, but decrypts the cells at ``enc_cols`` with
        ``cipher``. Cells that are not ``enc:v1:`` tokens pass through
        unchanged, so rows written before encryption was enabled still read."""
        row = _split_wire_row(self.send(f"GET {hid} {tid} {row_id}"))
        cols = set(enc_cols)
        return [cipher.decrypt(v) if i in cols else v
                for i, v in enumerate(row)]

    def count(self, hid: int, tid: int) -> int:
        return int(self.send(f"COUNT {hid} {tid}"))

    def sum(self, hid: int, tid: int, col: int) -> float:
        return float(self.send(f"SUM {hid} {tid} {col}"))

    def min(self, hid: int, tid: int, col: int) -> float:
        return float(self.send(f"MIN {hid} {tid} {col}"))

    def max(self, hid: int, tid: int, col: int) -> float:
        return float(self.send(f"MAX {hid} {tid} {col}"))

    def fcount_gt(self, hid: int, tid: int, col: int, threshold: float) -> int:
        return int(self.send(f"FCOUNT {hid} {tid} {col} {threshold}"))

    def select_gt(self, hid: int, tid: int, col: int, threshold: float) -> List[List[str]]:
        """Rows where ``col > threshold`` (SIMD predicate scan)."""
        body = self.send(f"SELGT {hid} {tid} {col} {threshold}")
        return _parse_rowlist(body)

    # ── JOIN (v1.0.4) ────────────────────────────────────────────────

    def join(self, l_hid: int, l_tid: int, l_col: int,
             r_hid: int, r_tid: int, r_col: int,
             how: str = "inner", op: object = Op.EQ
             ) -> List[Tuple[int, int]]:
        """2-way join. Returns ``[(left_row, right_row), ...]``.

        Matches rows where ``left.l_col {op} right.r_col``. Type
        compatibility: both columns must be STRING-or-STRING, or both
        numeric (INT/FLOAT/DATETIME interchange freely). VEC columns
        are rejected.

        ``how`` selects the join type (v0.8.0):

        * ``"inner"`` (default) — only matched pairs.
        * ``"left"``  — every left row; unmatched left rows pair with
          right row ``-1`` (the NULL sentinel).
        * ``"right"`` — every right row; unmatched right rows pair with
          left row ``-1``.
        * ``"full"``  — both unmatched sides included.

        ``op`` is the join predicate (:class:`Op`): ``Op.EQ`` (default)
        is an equi-join and runs as a **hash join** — O(N+M), no group
        cap. ``Op.GT`` / ``Op.LT`` are non-equi joins (``left > right`` /
        ``left < right``); these stay nested-loop and reject past ~100M
        comparisons with ``join_too_large``. String columns compare
        lexically (``strcmp``).

        Returns row-id pairs; fetch full rows via :meth:`get` if
        needed (sending entire rows inline would blow the response
        buffer fast).
        """
        how_map = {"inner": 0, "left": 1, "right": 2, "full": 3}
        if how not in how_map:
            raise ValueError(f"unknown how {how!r}; expected one of "
                              f"{sorted(how_map)}")
        cmd = f"JOIN {l_hid} {l_tid} {l_col} {r_hid} {r_tid} {r_col}"
        if how_map[how] != 0:
            cmd += f" TYPE {how_map[how]}"
        if int(op) != int(Op.EQ):
            cmd += f" OP {int(op)}"
        body = self.send(cmd)
        body = body.strip()
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        if not body:
            return []
        out: List[Tuple[int, int]] = []
        for entry in body.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            lr, rr = entry.split(",", 1)
            out.append((int(lr), int(rr)))
        return out

    # ── GROUP BY (v1.0.4) ────────────────────────────────────────────

    def group_by(self, hid: int, tid: int, group_col: int,
                 agg: str = "count", agg_col: Optional[int] = None,
                 by: Optional[Sequence[int]] = None,
                 having: Optional[Tuple[object, float]] = None,
                 order: Optional[Tuple[object, object]] = None,
                 limit: Optional[int] = None
                 ) -> List[Tuple[object, float]]:
        """Aggregate by grouping column. Returns ``[(group_key, value), ...]``.

        ``agg`` is one of: ``"count"``, ``"sum"``, ``"min"``, ``"max"``,
        ``"avg"``. ``agg_col`` is required for everything except count,
        and must be a numeric column (INT/FLOAT/DATETIME).

        ``group_key`` is returned as ``str`` for STRING group columns
        and as ``int``/``float`` for numeric ones. ``value`` is always a
        number.

        v0.8.0 clauses (all optional, generic CuttleDB API):

        * ``by`` — additional grouping columns. With one or more extra
          columns the result is grouped by the tuple of all key columns
          and ``group_key`` is returned as a ``tuple`` (one component per
          column, in ``[group_col, *by]`` order). No group-count cap.
        * ``having`` — ``(op, threshold)`` post-aggregation filter on the
          aggregated value. ``op`` is an :class:`Op` (GT/LT/EQ).
        * ``order`` — ``(field, direction)``. ``field`` is ``"key"``/``0``
          or ``"value"``/``1``; ``direction`` is ``"asc"``/``0`` or
          ``"desc"``/``1``.
        * ``limit`` — cap the number of returned groups (applied after
          ordering).
        """
        agg_op_map = {"count": 0, "sum": 1, "min": 2, "max": 3, "avg": 4}
        if agg not in agg_op_map:
            raise ValueError(f"unknown agg {agg!r}; expected one of "
                              f"{sorted(agg_op_map)}")
        op = agg_op_map[agg]
        if op == 0:
            # COUNT — agg_col is ignored server-side but we still send a
            # placeholder so the parser doesn't trip.
            ac = 0 if agg_col is None else agg_col
        else:
            if agg_col is None:
                raise ValueError(f"agg={agg!r} requires agg_col")
            ac = agg_col

        cmd = f"GROUPBY {hid} {tid} {group_col} {op} {ac}"
        multi = bool(by)
        if by:
            cmd += " BY " + " ".join(str(int(c)) for c in by)
        if having is not None:
            h_op, h_thr = having
            cmd += f" HAVING {int(h_op)} {int(h_thr)}"
        if order is not None:
            o_field, o_dir = order
            field_map = {"key": 0, "value": 1}
            d_map = {"asc": 0, "desc": 1}
            of = field_map[o_field] if isinstance(o_field, str) else int(o_field)
            od = d_map[o_dir] if isinstance(o_dir, str) else int(o_dir)
            cmd += f" ORDER {of} {od}"
        if limit is not None:
            cmd += f" LIMIT {int(limit)}"

        body = self.send(cmd)
        # Wire format: "[key:value;key:value;...]" where string components
        # are quoted and numeric components are bare. Multi-column keys join
        # their components with "|" (e.g. '"a"|"west":2').
        body = body.strip()
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        if not body:
            return []
        out: List[Tuple[object, float]] = []
        for entry in body.split(";"):
            if not entry:
                continue
            comps: List[object] = []
            i = 0
            value = ""
            while i < len(entry):
                comp, i, term = _parse_gb_component(entry, i)
                comps.append(comp)
                if term == ":":
                    value = entry[i:]
                    break
                # term == "|" → another key component follows
            if not comps:
                continue
            key: object = tuple(comps) if multi else comps[0]
            try:
                value_num = float(value)
            except ValueError:
                value_num = 0.0
            out.append((key, value_num))
        return out

    # ── Multi-token auth (v1.0.2) ────────────────────────────────────
    # The connection that calls these MUST have authenticated with the
    # root token (the one passed via `cuttledb-server --auth <token>`).
    # Tokens minted at runtime are in-memory only — server restart
    # reverts to just the --auth token.

    def add_token(self, label: str, token: Optional[str] = None) -> Tuple[str, str]:
        """Mint a new auth token. Returns (id, token).

        ``label`` is a human-readable description (e.g. "alice-laptop"
        or "ci-pipeline"); use it later to identify the token via
        :meth:`list_tokens`.

        ``token`` is the secret bytes. If omitted, a 64-char hex token
        is generated client-side via :func:`secrets.token_hex`. The
        server stores whatever you pass; cryptographic strength is
        your responsibility (this default uses Python's CSPRNG which
        is fine for almost every case).

        The token id (``"tN"``) is what you pass to :meth:`revoke_token`.
        """
        import secrets
        if token is None:
            token = secrets.token_hex(32)
        if " " in label or " " in token:
            raise ValueError("label and token must not contain spaces")
        new_id = self.send(f"TOKEN ADD {label} {token}")
        return new_id, token

    def list_tokens(self) -> List[dict]:
        """List token metadata. Returns dicts with id/label/created_ms/revoked.

        Token bytes are NEVER returned by the server — they're shown
        once at :meth:`add_token` time and must be persisted by the
        caller. Use :meth:`revoke_token` to disable a leaked token.
        """
        body = self.send("TOKEN LIST")
        # Wire shape: "[id:label:created_ms:revoked;id:label:created_ms:revoked;...]"
        body = body.strip()
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        if not body:
            return []
        out: List[dict] = []
        for entry in body.split(";"):
            parts = entry.split(":", 3)
            if len(parts) != 4:
                continue
            out.append({
                "id": parts[0],
                "label": parts[1],
                "created_ms": int(parts[2]) if parts[2] else 0,
                "revoked": bool(int(parts[3])),
            })
        return out

    def revoke_token(self, token_id: str) -> None:
        """Soft-delete a token by id. Subsequent AUTH attempts with the
        token's bytes fail. The root token (id="root") cannot be revoked
        via the wire — rotate it by restarting cuttledb-server with a
        new ``--auth <token>``.
        """
        self.send(f"TOKEN REVOKE {token_id}")

    # ── Vector search ────────────────────────────────────────────────

    def knn(
        self,
        hid: int,
        tid: int,
        col: int,
        k: int,
        query: Sequence[float],
        *,
        where: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        """Top-``k`` nearest rows by cosine similarity. Returns ``(row_id, score)``,
        sorted descending by score.

        ``where`` is an optional Phase 1 filter clause: ``"col_idx OP value
        [AND col_idx OP value ...]"``. ``OP`` is one of ``= != < <= > >=``;
        string values are quoted (``4="playbook"``); numeric values are bare
        (``5>3``). Up to 8 predicates AND'd. The substrate applies the
        filter after KNN scoring; with HNSW it oversamples to keep ``k``
        matching results."""
        q = "|".join(str(x) for x in query)
        cmd = f"KNN {hid} {tid} {col} {k} {q}"
        if where:
            cmd += f" WHERE {where}"
        body = self.send(cmd)
        return _parse_knn(body)

    def lsearch(
        self,
        hid: int,
        tid: int,
        col: int,
        k: int,
        query: str,
    ) -> List[Tuple[int, float]]:
        """Top-``k`` lexical matches via BM25 over a STRING column.

        Returns ``(row_id, score)`` sorted descending. The substrate
        auto-builds the inverted index on first use; call
        ``send("INDEX <hid> <tid> <col> BM25")`` explicitly if you want
        to force a rebuild with non-default ``k1`` / ``b`` parameters.

        See ``search`` for the hybrid (KNN + BM25) variant."""
        body = self.send(f"LSEARCH {hid} {tid} {col} {k} {query}")
        return _parse_knn(body)  # same +OK [row:score;...] shape

    def bsearch(
        self,
        hid: int,
        tid: int,
        k: int,
        expr: str,
    ) -> List[Tuple[int, float]]:
        """Boolean DSL retrieval (v0.5.17 Phase 4). Combines filters
        (AND/OR/parens, ``=`` ``!=`` ``<`` ``<=`` ``>`` ``>=``) with
        optional scoring atoms:

        - ``col~V[v1|v2|...]`` — vector similarity over a VEC column
        - ``col~"phrase"``    — BM25 over a STRING column

        Examples::

            db.bsearch(hid, tid, 5,
                       '(2="playbook" OR 2="ref") AND 3>3 AND 1~V[1.0|0.0]')
            db.bsearch(hid, tid, 10, '0~"hnsw build" AND 4="kernel"')

        Scoring atoms contribute to a per-row RRF rank; if no scoring
        atoms are present, results are ordered by ``row_id`` ascending
        and capped at ``k``. Returns ``(row_id, score)`` sorted desc."""
        body = self.send(f"BSEARCH {hid} {tid} {k} {expr}")
        return _parse_knn(body)

    def search(
        self,
        hid: int,
        tid: int,
        vec_col: int,
        text_col: int,
        k: int,
        vec: Sequence[float],
        query: str,
        *,
        where: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        """Hybrid retrieval (v0.5.17 Phase 3) — fuses KNN and BM25 via
        Reciprocal Rank Fusion.

        Returns ``(row_id, rrf_score)`` sorted desc. RRF scores are a
        ranking metric, not a similarity — comparable across queries
        only by order, not magnitude.

        The substrate pulls ``max(k*4, 50)`` candidates per stream,
        applies the optional WHERE filter to both, then merges. RRF's
        ``k`` parameter is fixed at 60 (the standard default).

        Pass ``where`` to filter both streams; the syntax matches
        ``knn``'s where clause."""
        v = "|".join(str(x) for x in vec)
        cmd = f"SEARCH {hid} {tid} {vec_col} {text_col} {k} {v} ||| {query}"
        if where:
            cmd += f" WHERE {where}"
        body = self.send(cmd)
        return _parse_knn(body)

    # ── Kernel dispatch (v0.5.2) ─────────────────────────────────────

    def exec_kernel(
        self,
        name: str,
        a: Sequence[float],
        b: Optional[Sequence[float]] = None,
    ) -> float:
        """Run a named kernel from the server's registry. Returns a scalar.

        Single-array kernels (vsum_f32, vmax_f32):
            db.exec_kernel("vsum_f32", [1.0, 2.0, 3.0])     # → 6.0

        Two-array kernels (cosine_pair_f32, dot_f32) — arrays must match
        in length and are passed as ``a`` and ``b``:
            db.exec_kernel("cosine_pair_f32", [1, 0], [0, 1])   # → 0.0
            db.exec_kernel("dot_f32",         [1, 2], [3, 4])   # → 11.0

        Server-side kernel dispatch: agents and ML adapters can run a
        registered kernel without inlining the algorithm body."""
        a_str = "|".join(str(x) for x in a)
        if b is None:
            line = f"EXEC {name} {a_str}"
        else:
            b_str = "|".join(str(x) for x in b)
            line = f"EXEC {name} {a_str};{b_str}"
        return float(self.send(line))

    # ── Tensor dispatch (server-side compute for ML adapters) ────────

    def matmul_f32_b(self, A, B):  # type: ignore[no-untyped-def]
        """Binary-framed matmul. Same math as :meth:`matmul_f32` but
        sends FP32 bytes directly over the wire (no text encoding) and
        receives the result as raw FP32 bytes.

        This is the production-throughput path. The wire-text encoding
        of :meth:`matmul_f32` caps at ~6K floats per request; the binary
        path is bounded only by ``MATMUL_B_MAX_INPUT_F32`` (4M floats per
        matrix at v0.5.x).

        Example::

            import numpy as np
            A = np.random.randn(256, 512).astype(np.float32)
            B = np.random.randn(512, 256).astype(np.float32)
            C = db.matmul_f32_b(A, B)
            # → (256, 256) FP32 array — bit-equivalent to A @ B within FP32 sum order
        """
        try:
            import numpy as _np
        except ImportError as e:
            raise RuntimeError("numpy required for matmul_f32_b") from e
        A = _np.ascontiguousarray(A, dtype=_np.float32)
        B = _np.ascontiguousarray(B, dtype=_np.float32)
        if A.ndim != 2 or B.ndim != 2:
            raise ValueError(f"matmul_f32_b expects 2D inputs; got {A.shape}, {B.shape}")
        M, K = A.shape
        K2, N = B.shape
        if K != K2:
            raise ValueError(f"shape mismatch: A is {A.shape}, B is {B.shape}")

        # Send command line + binary A + binary B in one write.
        line = f"MATMUL_B {M} {N} {K}\n".encode("ascii")
        self._write(line)
        self._write(A.tobytes())
        self._write(B.tobytes())

        # Response header: "+OK_B M N\n"  (or "-ERR <code>\r\n")
        header = self._readline()
        if header.startswith(b"-ERR"):
            raise CuttleDBError(header[5:].decode("ascii", errors="replace").strip())
        if not header.startswith(b"+OK_B "):
            raise CuttleDBError(f"unexpected matmul_f32_b header: {header!r}")
        parts = header[len(b"+OK_B "):].split()
        if len(parts) != 2:
            raise CuttleDBError(f"malformed +OK_B header: {header!r}")
        rM, rN = int(parts[0]), int(parts[1])
        if (rM, rN) != (M, N):
            raise CuttleDBError(f"dimension mismatch in response: expected ({M},{N}), got ({rM},{rN})")

        # Read M*N*4 bytes of raw FP32.
        # _readline left any extra bytes in self._buf; drain those first
        # before pulling more from the socket.
        need = rM * rN * 4
        buf = bytearray()
        if self._buf:
            take = min(need, len(self._buf))
            buf.extend(self._buf[:take])
            self._buf = self._buf[take:]
        while len(buf) < need:
            chunk = self._sock.recv(need - len(buf))
            if not chunk:
                raise CuttleDBError("connection closed mid-payload")
            buf.extend(chunk)
        return _np.frombuffer(bytes(buf), dtype=_np.float32).reshape(rM, rN).copy()

    def flash_attn_f32(self, Q, K, V, *, scale=None, causal=False):  # type: ignore[no-untyped-def]
        """Single-head scaled-dot-product attention server-side.

        Computes ``O = softmax(Q K^T * scale [+ causal_mask]) V`` and
        returns ``O`` as a NumPy (seq_q, d) ndarray.

        :param Q: (seq_q, d) FP32
        :param K: (seq_kv, d) FP32
        :param V: (seq_kv, d) FP32
        :param scale: softmax scale; defaults to ``1.0 / sqrt(d)``
        :param causal: if True, mask the upper triangle (requires seq_q == seq_kv)
        """
        try:
            import numpy as _np
        except ImportError as e:
            raise RuntimeError("numpy required for flash_attn_f32") from e
        Q_arr = _np.ascontiguousarray(Q, dtype=_np.float32)
        K_arr = _np.ascontiguousarray(K, dtype=_np.float32)
        V_arr = _np.ascontiguousarray(V, dtype=_np.float32)
        if Q_arr.ndim != 2 or K_arr.ndim != 2 or V_arr.ndim != 2:
            raise ValueError(
                f"flash_attn_f32 expects 2D inputs; got {Q_arr.shape}, {K_arr.shape}, {V_arr.shape}"
            )
        seq_q, d = Q_arr.shape
        seq_kv, d2 = K_arr.shape
        seq_kv_v, d3 = V_arr.shape
        if d != d2 or d != d3:
            raise ValueError(
                f"dim mismatch: Q={Q_arr.shape}, K={K_arr.shape}, V={V_arr.shape}"
            )
        if seq_kv != seq_kv_v:
            raise ValueError(f"K and V seq lengths differ: {K_arr.shape} vs {V_arr.shape}")

        sc = float(scale) if scale is not None else float(1.0 / (d ** 0.5))
        causal_int = 1 if causal else 0
        line = f"FLASH_ATTN_B {seq_q} {seq_kv} {d} {sc:.9g} {causal_int}\n".encode("ascii")
        self._write(line)
        self._write(Q_arr.tobytes())
        self._write(K_arr.tobytes())
        self._write(V_arr.tobytes())

        header = self._readline()
        if header.startswith(b"-ERR"):
            raise CuttleDBError(header[5:].decode("ascii", errors="replace").strip())
        if not header.startswith(b"+OK_FA "):
            raise CuttleDBError(f"unexpected flash_attn_f32 header: {header!r}")
        parts = header[len(b"+OK_FA "):].split()
        if len(parts) != 2:
            raise CuttleDBError(f"malformed +OK_FA header: {header!r}")
        rq, rd = int(parts[0]), int(parts[1])
        if (rq, rd) != (seq_q, d):
            raise CuttleDBError(f"shape mismatch in response: expected ({seq_q},{d}), got ({rq},{rd})")

        need = rq * rd * 4
        buf = bytearray()
        if self._buf:
            take = min(need, len(self._buf))
            buf.extend(self._buf[:take])
            self._buf = self._buf[take:]
        while len(buf) < need:
            chunk = self._sock.recv(need - len(buf))
            if not chunk:
                raise CuttleDBError("connection closed mid-payload")
            buf.extend(chunk)
        return _np.frombuffer(bytes(buf), dtype=_np.float32).reshape(rq, rd).copy()

    def matmul_f32(self, A, B):  # type: ignore[no-untyped-def]
        """Compute C = A @ B server-side. Both inputs are FP32 row-major.

        Accepts either NumPy arrays (shape (M, K) and (K, N)) or Python
        sequences of floats with explicit shapes. Returns a NumPy ndarray
        of shape (M, N), dtype float32.

        Phase 2.5 limit: total floats per matrix bounded by ~32K (the
        wire-text encoding caps practical size; the server hard-caps at
        MATMUL_MAX_INPUT). For larger matmul wait on Phase 2.5.x binary
        framing or do it client-side via NumPy.

        Examples::

            import numpy as np
            A = np.array([[1, 2], [3, 4]], dtype=np.float32)
            B = np.array([[5, 6], [7, 8]], dtype=np.float32)
            C = db.matmul_f32(A, B)
            # → [[19, 22], [43, 50]]
        """
        try:
            import numpy as _np
            A_arr = _np.ascontiguousarray(A, dtype=_np.float32)
            B_arr = _np.ascontiguousarray(B, dtype=_np.float32)
        except ImportError as e:
            raise RuntimeError("numpy required for matmul_f32") from e
        if A_arr.ndim != 2 or B_arr.ndim != 2:
            raise ValueError(f"matmul_f32 expects 2D inputs; got {A_arr.shape}, {B_arr.shape}")
        M, K = A_arr.shape
        K2, N = B_arr.shape
        if K != K2:
            raise ValueError(f"shape mismatch: A is {A_arr.shape}, B is {B_arr.shape}")
        A_flat = A_arr.reshape(-1)
        B_flat = B_arr.reshape(-1)
        # Use .9g for FP32 round-trip precision (matches storage convention).
        a_str = "|".join(f"{float(x):.9g}" for x in A_flat)
        b_str = "|".join(f"{float(x):.9g}" for x in B_flat)
        body = self.send(f"MATMUL {M} {N} {K} {a_str};{b_str}")
        # Parse pipe-encoded result
        out_floats = [float(x) for x in body.split("|")]
        if len(out_floats) != M * N:
            raise CuttleDBError(
                f"matmul_f32: expected {M*N} floats in response, got {len(out_floats)}"
            )
        return _np.array(out_floats, dtype=_np.float32).reshape(M, N)

    # ── String-kernel dispatch (v0.5.3) ──────────────────────────────

    def exec_str_kernel(
        self,
        name: str,
        a: str,
        b: Optional[str] = None,
    ) -> object:
        """Run a string-typed kernel and return its result.

        - For kinds ``str→str`` / ``(str,str)→str``: returns a ``str``.
        - For kind ``str→int`` (e.g. ``str_length``): returns an ``int``.

        Examples::

            db.exec_str_kernel("str_upper",  "hello")           # → "HELLO"
            db.exec_str_kernel("str_lower",  "HELLO")           # → "hello"
            db.exec_str_kernel("str_length", "hello")           # → 5
            db.exec_str_kernel("str_concat", "foo", "bar")      # → "foobar"

        Args are wire-escape-encoded so embedded ``\\`` / ``,`` / ``\\r`` /
        ``\\n`` / ``;`` round-trip. For two-arg kernels, a literal ``;`` in
        a value must be written as ``\\;`` — the wire splits args on the
        first unescaped ``;``.
        """
        a_enc = _encode_exec_str(a)
        if b is None:
            line = f"EXEC {name} {a_enc}"
        else:
            line = f"EXEC {name} {a_enc};{_encode_exec_str(b)}"
        body = self.send(line)
        if body.startswith("s:"):
            return _decode_exec_str(body[2:])
        # Integer-return kernels (e.g. str_length) come back as bare digits.
        return int(body)

    def exec_no_args(self, name: str) -> int:
        """Run a no-argument kernel (e.g. ``now_unix_ms``, ``now_unix_s``).
        Returns an integer (e.g. unix-milliseconds since the epoch).

        Added in v0.5.8 to support capability-style kernels that don't
        need parameters (current time, sequence numbers, future things
        like random_int / pid)."""
        return int(self.send(f"EXEC {name}"))

    def exec_no_args_str(self, name: str) -> str:
        """Run a no-argument kernel returning a string (e.g. ``uuid4``).

        Added in v0.5.9 alongside KSIG_NO_ARGS_TO_STR. Result is the
        wire-decoded string (no leading ``s:`` tag)."""
        body = self.send(f"EXEC {name}")
        if not body.startswith("s:"):
            raise CuttleDBError(f"expected string return, got: {body!r}")
        return _decode_exec_str(body[2:])

    def exec_int_to_str(self, name: str, n: int) -> str:
        """Run a kernel that takes one integer argument and returns a string
        (e.g. ``format_iso(unix_ms)``, ``random_hex(n_bytes)``).

        Added in v0.5.9 alongside KSIG_INT_TO_STR."""
        body = self.send(f"EXEC {name} {int(n)}")
        if not body.startswith("s:"):
            raise CuttleDBError(f"expected string return, got: {body!r}")
        return _decode_exec_str(body[2:])

    # ── Persistence ──────────────────────────────────────────────────

    def save(self, hid: int, path: str) -> str:
        return self.send(f"SAVE {hid} {path}")

    def load(self, path: str) -> int:
        return int(self.send(f"LOAD {path}"))

    # ── Subscriptions ────────────────────────────────────────────────

    def sub(self, hid: int, tid: int) -> str:
        """Register this connection for change events on ``(hid, tid)``.
        Drain events with ``poll_events(timeout)`` or ``stream_events()``."""
        return self.send(f"SUB {hid} {tid}")

    def unsub(self, hid: int, tid: int) -> str:
        return self.send(f"UNSUB {hid} {tid}")

    def poll_events(self, timeout: float = 0.0) -> List[Event]:
        """Drain any pending ``>EVT`` lines off the socket.

        ``timeout`` is the maximum time to block waiting for at least one event.
        ``0`` means non-blocking (return whatever is already buffered)."""
        with self._lock:
            events = list(self._pending_events)
            self._pending_events.clear()
            if events or self._sock is None:
                return events
            self._sock.settimeout(timeout)
            try:
                while True:
                    try:
                        chunk = self._sock.recv(8192)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    self._buf += chunk
                    while b"\n" in self._buf:
                        idx = self._buf.index(b"\n")
                        line = self._buf[:idx].rstrip(b"\r").decode("utf-8")
                        self._buf = self._buf[idx + 1:]
                        if line.startswith(">EVT "):
                            events.append(_parse_event(line))
                        else:
                            # Non-event lines interleaved here are unexpected
                            # (no command was in flight). Stash for next reader.
                            self._buf = line.encode("utf-8") + b"\n" + self._buf
                            return events
                    if events:
                        break
            finally:
                self._sock.settimeout(self.timeout)
            return events

    @contextmanager
    def stream_events(self, poll_interval: float = 0.1) -> Iterator[Iterator[Event]]:
        """Context-managed generator that yields ``Event``s as they arrive.

        Usage::

            with db.stream_events() as events:
                for evt in events:
                    print(evt)
                    if evt.op == "INS" and evt.row_id > 100: break
        """
        stop = False
        def gen() -> Iterator[Event]:
            while not stop:
                for evt in self.poll_events(poll_interval):
                    yield evt
        try:
            yield gen()
        finally:
            stop = True

    # ── Change feed ──────────────────────────────────────────────────

    def log(self, hid: int, tid: int, since: int = 0) -> Tuple[int, List[Tuple[int, int, str]]]:
        """Read the per-table change ring buffer.

        Returns ``(cursor, events)`` where ``cursor`` is the new high-water mark
        to pass on the next call, and ``events`` are tuples ``(ts_ms, row_id, op)``
        with ``op`` in ``"I"``, ``"D"``, ``"U"``."""
        body = self.send(f"LOG {hid} {tid} {since}")
        return _parse_log(body)


# ── Response parsers ────────────────────────────────────────────────────

def _parse_kv(line: str) -> dict:
    """Parse a space-separated ``key=value`` line into a dict (values left as strings)."""
    out: dict = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _parse_event(line: str) -> Event:
    # Format: ">EVT <hid> <tid> <row_id> <op>"
    parts = line.split()
    if len(parts) < 5:
        raise CuttleDBError(f"malformed event: {line!r}")
    return Event(hid=int(parts[1]), tid=int(parts[2]), row_id=int(parts[3]), op=parts[4])


def _split_wire_rows(s: str) -> List[str]:
    """Split a rowlist body on bare ``;``, honoring ``\\;`` escapes.

    Mirror of the server's ``wire_str_encode``: STRING values containing
    a literal semicolon are emitted as ``\\;`` so they don't terminate
    the row early. Naive ``s.split(";")`` would split inside them.
    """
    out: List[str] = []
    cur: List[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            cur.append(c); cur.append(s[i + 1])
            i += 2
        elif c == ";":
            out.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    out.append("".join(cur))
    return out


def _parse_rowlist(body: str) -> List[List[str]]:
    """Parse ``[row;row;...]`` into a list of value-lists. Empty result is ``[]``.

    Splits rows via :func:`_split_wire_rows` (escape-aware on ``;``) and
    each row's columns via :func:`_split_wire_row` (escape-aware on
    ``,``). STRING values containing ``,`` ``;`` ``\\`` CR LF round-trip
    cleanly with the server's ``wire_str_encode``. Naive splits would
    misalign column or row counts on any such value — that was the
    v0.6.0 latent decoder bug, masked by the SELGT/VEC crash.
    """
    if not body.startswith("[") or not body.endswith("]"):
        raise CuttleDBError(f"bad row list: {body!r}")
    inner = body[1:-1]
    if not inner:
        return []
    # Fast path: a backslash only ever appears as an escape (\, \; \\ \r \n),
    # so its absence means no STRING value held a delimiter and native splits
    # are exact — ~10x faster than the char-by-char escape-aware walk on the
    # common numeric/simple-string rows. Fall back only when escapes exist.
    if "\\" not in inner:
        return [row.split(",") for row in inner.split(";")]
    return [_split_wire_row(row) for row in _split_wire_rows(inner)]


def _parse_knn(body: str) -> List[Tuple[int, float]]:
    """Parse ``[row_id:score;row_id:score;...]``."""
    if not body.startswith("[") or not body.endswith("]"):
        raise CuttleDBError(f"bad knn result: {body!r}")
    inner = body[1:-1]
    if not inner:
        return []
    out: List[Tuple[int, float]] = []
    for tok in inner.split(";"):
        rid, _, score = tok.partition(":")
        out.append((int(rid), float(score)))
    return out


def _parse_log(body: str) -> Tuple[int, List[Tuple[int, int, str]]]:
    """Parse ``<cursor> [ts:row_id:op;ts:row_id:op;...]``."""
    cursor_s, _, rest = body.partition(" ")
    cursor = int(cursor_s)
    rest = rest.strip()
    if not (rest.startswith("[") and rest.endswith("]")):
        raise CuttleDBError(f"bad log result: {body!r}")
    inner = rest[1:-1]
    if not inner:
        return cursor, []
    events: List[Tuple[int, int, str]] = []
    for tok in inner.split(";"):
        ts_s, rid_s, op = tok.split(":")
        events.append((int(ts_s), int(rid_s), op))
    return cursor, events
