#!/bin/bash
# CuttleDB adapter test runner — starts a server, runs Python + JS test
# suites, cleans up. Run from the CuttleDB/ directory or from the repo
# root.
#
#   bash CuttleDB/scripts/test.sh
#
# Required:
#   CUTTLEDB_SERVER_BIN  path to a cuttledb-server binary (no default —
#                        you must set this before running)
#
# Optional env vars:
#   CUTTLEDB_PORT        default 17789
#   CUTTLEDB_AUTH_PORT   default 17790
#   CUTTLEDB_AUTH_TOKEN  default "test-secret-token"
#   CUTTLEDB_CLUSTER_PORT_A   default 17791
#   CUTTLEDB_CLUSTER_PORT_B   default 17792
#   SKIP_PYTHON          set to 1 to skip Python tests
#   SKIP_JS              set to 1 to skip JS tests

set -e

PORT="${CUTTLEDB_PORT:-17789}"
AUTH_PORT="${CUTTLEDB_AUTH_PORT:-17790}"
AUTH_TOKEN="${CUTTLEDB_AUTH_TOKEN:-test-secret-token}"
CLUSTER_PORT_A="${CUTTLEDB_CLUSTER_PORT_A:-17791}"
CLUSTER_PORT_B="${CUTTLEDB_CLUSTER_PORT_B:-17792}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="${CUTTLEDB_SERVER_BIN:-}"

if [ -z "$SERVER" ] || [ ! -x "$SERVER" ]; then
    echo "error: CUTTLEDB_SERVER_BIN is not set or not executable." >&2
    echo "  Download a release binary from GitHub Releases and point this" >&2
    echo "  env var at it before running the test suite. For example:" >&2
    echo "    export CUTTLEDB_SERVER_BIN=/path/to/cuttledb-server" >&2
    exit 1
fi

echo "Starting CuttleDB server (open) on port $PORT..."
"$SERVER" --port "$PORT" > /tmp/cuttledb-test-server.log 2>&1 &
SERVER_PID=$!

echo "Starting CuttleDB server (auth-gated) on port $AUTH_PORT..."
"$SERVER" --port "$AUTH_PORT" --auth "$AUTH_TOKEN" > /tmp/cuttledb-test-auth.log 2>&1 &
AUTH_PID=$!

echo "Starting CuttleDB cluster nodes on ports $CLUSTER_PORT_A, $CLUSTER_PORT_B..."
"$SERVER" --port "$CLUSTER_PORT_A" > /tmp/cuttledb-test-cluster-a.log 2>&1 &
CLUSTER_A_PID=$!
"$SERVER" --port "$CLUSTER_PORT_B" > /tmp/cuttledb-test-cluster-b.log 2>&1 &
CLUSTER_B_PID=$!

cleanup() {
    for pid in "$SERVER_PID" "$AUTH_PID" "$CLUSTER_A_PID" "$CLUSTER_B_PID"; do
        if kill -0 "$pid" 2>/dev/null; then
            if command -v taskkill >/dev/null 2>&1; then
                taskkill //PID "$pid" //F >/dev/null 2>&1 || true
            else
                kill "$pid" 2>/dev/null || true
            fi
        fi
    done
}
trap cleanup EXIT
sleep 1
export CUTTLEDB_AUTH_PORT="$AUTH_PORT"
export CUTTLEDB_AUTH_TOKEN="$AUTH_TOKEN"
export CUTTLEDB_CLUSTER_PORT_A="$CLUSTER_PORT_A"
export CUTTLEDB_CLUSTER_PORT_B="$CLUSTER_PORT_B"

FAILED=0

if [ "${SKIP_PYTHON:-0}" != "1" ]; then
    echo ""
    echo "── Python adapter ─────────────────────────────────"
    cd "$ROOT/adapters/python"
    PYTHONPATH=. CUTTLEDB_PORT="$PORT" python -m pytest tests/ -v || FAILED=1
fi

if [ "${SKIP_JS:-0}" != "1" ]; then
    echo ""
    echo "── JS adapter (TCP) ───────────────────────────────"
    cd "$ROOT"
    CUTTLEDB_PORT="$PORT" node adapters/tests/smoke.mjs || FAILED=1

    echo ""
    echo "── JS Cluster ─────────────────────────────────────"
    node adapters/tests/cluster.mjs || FAILED=1
fi

echo ""
if [ "$FAILED" = "0" ]; then
    echo "All adapter tests passed."
else
    echo "Some adapter tests failed."
fi
exit $FAILED
