"""cuttledb.replicate — change-feed replicator for CuttleDB primary+replicas.

A small, dependency-free worker that tails the LOG on a primary CuttleDB
and replays events into one or more replicas. Designed for Pattern 1
in docs/DEPLOYMENT.md: primary + read replicas.

Usage::

    python -m cuttledb.replicate \\
        --primary  127.0.0.1:7780 \\
        --replicas r1.local:7780,r2.local:7780 \\
        --tables   0:0,0:1 \\
        --interval 0.05

Behavior:
  * Connects to primary and each replica.
  * For each `(hid, tid)` pair, tails LOG. Replays INSERT and DELETE
    events into all replicas. UPDATE not yet emitted by the server
    (planned for v0.5).
  * Maintains a per-table cursor; persists it to a checkpoint file so
    restart resumes from where it left off.
  * Idempotent on replay: INSERT into a replica creates a new row id
    that should match the primary's id, because both started from a
    SAVE-bootstrapped state with synchronized row counts. If a replica
    drifts (insert fails, row already exists), the script logs and
    continues — corruption recovery is out of scope here, bootstrap
    from a fresh SAVE.
  * Exits on SIGINT/SIGTERM. Checkpoint is flushed before exit.

This is not a leader-election layer, not a quorum reader, not a
high-availability system. It's the simple Pattern-1 case turned into
something you can run as a systemd unit.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

from . import CuttleDB, CuttleDBError


def parse_endpoints(s: str) -> List[Tuple[str, int]]:
    """Parse a comma-separated `host:port,host:port,...` string."""
    out: List[Tuple[str, int]] = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        host, _, port = tok.partition(":")
        out.append((host, int(port or "7780")))
    return out


def parse_tables(s: str) -> List[Tuple[int, int]]:
    """Parse a comma-separated `hid:tid,hid:tid,...` string."""
    out: List[Tuple[int, int]] = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        hid, _, tid = tok.partition(":")
        out.append((int(hid), int(tid)))
    return out


class Replicator:
    """Tails LOG on a primary, replays into replicas."""

    def __init__(
        self,
        primary: Tuple[str, int],
        replicas: List[Tuple[str, int]],
        tables: List[Tuple[int, int]],
        interval: float = 0.05,
        checkpoint: str = ".cuttledb-replicate.cursor",
        auth: str | None = None,
    ) -> None:
        self.primary_addr = primary
        self.replica_addrs = replicas
        self.tables = tables
        self.interval = interval
        self.checkpoint_path = Path(checkpoint)
        self.auth = auth
        self.primary: CuttleDB | None = None
        self.replicas: List[CuttleDB] = []
        self.cursors: Dict[str, int] = {}      # "hid:tid" -> cursor
        self.running = True

    # ── Lifecycle ────────────────────────────────────────────────────

    def connect(self) -> None:
        self.primary = CuttleDB.connect(*self.primary_addr, auth=self.auth)
        for addr in self.replica_addrs:
            self.replicas.append(CuttleDB.connect(*addr, auth=self.auth))
        self._load_checkpoint()

    def close(self) -> None:
        self._save_checkpoint()
        if self.primary:
            try: self.primary.close()
            except Exception: pass
        for r in self.replicas:
            try: r.close()
            except Exception: pass

    def stop(self) -> None:
        self.running = False

    # ── Checkpoint persistence ───────────────────────────────────────

    def _load_checkpoint(self) -> None:
        if self.checkpoint_path.exists():
            try:
                self.cursors = json.loads(self.checkpoint_path.read_text())
            except (OSError, json.JSONDecodeError):
                self.cursors = {}
        # Ensure all configured tables have an entry; default to 0.
        for hid, tid in self.tables:
            self.cursors.setdefault(f"{hid}:{tid}", 0)

    def _save_checkpoint(self) -> None:
        try:
            self.checkpoint_path.write_text(json.dumps(self.cursors))
        except OSError as e:
            print(f"[warn] checkpoint write failed: {e}", file=sys.stderr)

    # ── Replay loop ──────────────────────────────────────────────────

    def replay_once(self) -> int:
        """One pass over all tables. Returns the number of events replayed."""
        if self.primary is None:
            return 0
        total = 0
        for hid, tid in self.tables:
            key = f"{hid}:{tid}"
            try:
                cursor, events = self.primary.log(hid, tid, since=self.cursors[key])
            except CuttleDBError as e:
                print(f"[warn] LOG {key} failed: {e}", file=sys.stderr)
                continue
            for ts_ms, row_id, op in events:
                self._replay_event(hid, tid, row_id, op)
                total += 1
            self.cursors[key] = cursor
        return total

    def _replay_event(self, hid: int, tid: int, row_id: int, op: str) -> None:
        if op == "I":
            try:
                row = self.primary.get(hid, tid, row_id)
            except CuttleDBError:
                return  # row vanished between LOG and GET — primary deleted; skip
            for r in self.replicas:
                try:
                    r.insert(hid, tid, row)
                except CuttleDBError as e:
                    print(f"[warn] replica INSERT failed: {e}", file=sys.stderr)
        elif op == "D":
            for r in self.replicas:
                try:
                    r.delete(hid, tid, row_id)
                except CuttleDBError as e:
                    print(f"[warn] replica DELETE failed: {e}", file=sys.stderr)
        # 'U' (update) reserved; server doesn't emit yet (v0.5 target).

    def run(self) -> None:
        next_checkpoint_t = time.time() + 5.0
        while self.running:
            n = self.replay_once()
            now = time.time()
            if now >= next_checkpoint_t:
                self._save_checkpoint()
                next_checkpoint_t = now + 5.0
            if n == 0:
                time.sleep(self.interval)


def main() -> int:
    ap = argparse.ArgumentParser(prog="cuttledb.replicate")
    ap.add_argument("--primary",  required=True, help="host:port of primary")
    ap.add_argument("--replicas", required=True, help="comma-sep host:port list")
    ap.add_argument("--tables",   required=True, help="comma-sep hid:tid list")
    ap.add_argument("--interval", type=float, default=0.05,
                    help="seconds between idle polls")
    ap.add_argument("--checkpoint", default=".cuttledb-replicate.cursor",
                    help="cursor checkpoint file")
    ap.add_argument("--auth", default=os.environ.get("CUTTLEDB_AUTH"),
                    help="AUTH token (or set CUTTLEDB_AUTH env var)")
    args = ap.parse_args()

    primary  = parse_endpoints(args.primary)[0]
    replicas = parse_endpoints(args.replicas)
    tables   = parse_tables(args.tables)

    if not replicas:
        print("error: at least one replica required", file=sys.stderr)
        return 1
    if not tables:
        print("error: at least one (hid:tid) table required", file=sys.stderr)
        return 1

    r = Replicator(
        primary=primary,
        replicas=replicas,
        tables=tables,
        interval=args.interval,
        checkpoint=args.checkpoint,
        auth=args.auth,
    )
    try:
        r.connect()
    except (CuttleDBError, OSError, socket.error) as e:
        print(f"connect failed: {e}", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT,  lambda *_: r.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: r.stop())

    print(f"replicating {len(tables)} table(s) "
          f"from {primary[0]}:{primary[1]} to {len(replicas)} replica(s); "
          f"interval={args.interval}s", file=sys.stderr)

    try:
        r.run()
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
