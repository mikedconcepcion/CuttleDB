#!/usr/bin/env python3
"""cuttledb-cli — interactive CLI for CuttleDB. Like redis-cli for CuttleDB.

Usage:
    python cuttledb-cli.py                       # connect to 127.0.0.1:7780
    python cuttledb-cli.py --port 7781
    python cuttledb-cli.py --host 10.0.0.1 --port 7780
    python cuttledb-cli.py --eval "OPEN"         # one-shot, prints result, exits

Built-in commands (start with ':'):
    :help        — list verbs
    :quit        — exit
    :time on/off — print round-trip time per command
"""
import argparse, socket, sys, time

VERBS = """\
Verbs:
  OPEN                                          → +OK <hid>
  CREATE <hid> <name> <c1>:<t1>,<c2>:<t2>,...   → +OK <tid>      (types: 0=int, 1=float, 2=string)
  INSERT <hid> <tid> <v1>,<v2>,...              → +OK <row_id>
  GET <hid> <tid> <row_id>                      → +OK <values>
  COUNT <hid> <tid>                             → +OK <n>
  SUM | MIN | MAX <hid> <tid> <col>             → +OK <value>
  FCOUNT <hid> <tid> <col> <threshold>          → +OK <count>       (col > thr)
  SELGT  <hid> <tid> <col> <threshold>          → +OK [row;row;...]
"""

def connect(host, port):
    s = socket.socket()
    s.connect((host, port))
    return s.makefile('rwb', buffering=0)

def send_cmd(f, line):
    f.write((line + '\n').encode())
    return f.readline().decode().rstrip('\r\n')

def repl(f, show_time):
    print("cuttledb-cli — type :help, :quit. Pipeline with ';'.\n")
    while True:
        try:
            line = input("cuttledb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith(":"):
            if line in (":quit", ":exit", ":q"):
                return
            if line == ":help":
                print(VERBS)
                continue
            if line.startswith(":time"):
                show_time = line.endswith("on")
                print(f"timing: {'on' if show_time else 'off'}")
                continue
            print(f"unknown command: {line}")
            continue
        # Support pipelining via ';' separator
        cmds = [c.strip() for c in line.split(';') if c.strip()]
        t0 = time.perf_counter() if show_time else 0
        if len(cmds) == 1:
            print(send_cmd(f, cmds[0]))
        else:
            f.write(('\n'.join(cmds) + '\n').encode())
            for _ in cmds:
                print(f.readline().decode().rstrip('\r\n'))
        if show_time:
            dt = (time.perf_counter() - t0) * 1000
            print(f"  ({dt:.2f}ms)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7780)
    ap.add_argument("--eval", help="run one command and exit")
    ap.add_argument("--time", action="store_true", help="time every command")
    args = ap.parse_args()
    try:
        f = connect(args.host, args.port)
    except ConnectionRefusedError:
        print(f"error: cannot connect to {args.host}:{args.port}", file=sys.stderr)
        sys.exit(1)
    if args.eval:
        print(send_cmd(f, args.eval))
        return
    repl(f, args.time)

if __name__ == "__main__":
    main()
