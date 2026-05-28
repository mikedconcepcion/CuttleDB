# Security Policy

## Supported Versions

| Version  | Supported          | Notes |
|----------|--------------------|-------|
| 0.6.x    | :white_check_mark: | Active. Security fixes backported. |

CuttleDB follows [Semantic Versioning](https://semver.org/). Security
patches land as patch-version bumps (`0.6.x → 0.6.(x+1)`) within the
current minor series. The v0.x prefix is honest framing for a
substrate that is production-orientation but with surface still
expanding; v1.0 ships when graph types, distributed sync, mTLS, and
EC keys all land.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub
issues, discussions, or pull requests.**

Use **GitHub's private vulnerability reporting** for this repository:

> https://github.com/mikedconcepcion/CuttleDB/security/advisories/new

That form opens a private channel visible only to repository
maintainers. It supports threaded conversation, embargoed fix
coordination, and the eventual public advisory in one place. No
email handling required on either side.

Include in your report:

- A clear description of the issue and its impact.
- Reproduction steps or a proof-of-concept (PoC).
- The CuttleDB version (`cuttledb-server --version` or contents of
  `VERSION`) and build flags (e.g. `CUTTLEDB_WITH_TLS`, `--auth`,
  `--rate-limit`).
- Your assessment of severity (CVSS v3.1 score welcome, not required).
- Whether you'd like public credit when the fix lands.

We will acknowledge receipt within **3 business days** and aim to
give a substantive response within **10 business days**. We
coordinate on embargo + disclosure timeline through the same private
advisory thread.

## Scope

In scope:

- The `cuttledb-server` binary.
- The wire protocol (line-based + WebSocket transport).
- The Python adapter (`cuttledb` on PyPI).
- The JavaScript adapter.
- The HNSW, BM25, DSL, and WAL implementations.
- Authentication (`AUTH`, `TOKEN ADD/LIST/REVOKE`) and the audit log.

Out of scope:

- Vulnerabilities in third-party software you connect to CuttleDB.
- Self-DoS via misconfiguration (rate-limit, audit-dir filesystem
  space).
- Issues that require physical access to the host or root-level
  access to the CuttleDB process.
- Plain-TCP transport — TLS is delivered via `CUTTLEDB_WITH_TLS=1`
  builds or via a TLS sidecar (stunnel/nginx). Operating without TLS
  on untrusted networks is a configuration choice, not a vulnerability.

## Hardening Defaults

CuttleDB ships with security-relevant features that are **opt-in**,
not default-on, so that minimal deployments stay zero-friction:

- `--auth <token>` — require AUTH before any non-PING/HELLO verb.
- `--rate-limit N` — per-connection commands-per-second sliding window.
- `--max-conn N` — total concurrent connection cap (DoS defense).
- `--slow-log-ms N` — log queries slower than N ms to stderr.
- `--idle-timeout-ms N` — close idle connections (default 300 000 ms).
- `--audit-dir <path>` — NDJSON-per-UTC-day dispatched-command log.
- `--tls-cert / --tls-key` — TLS handshake (requires
  `CUTTLEDB_WITH_TLS=1`).
- `--wal-dir <path>` — durability (replay on restart).

A production-grade deployment SHOULD set, at minimum:

```bash
cuttledb-server --port 7780 \
  --auth $CUTTLEDB_TOKEN \
  --rate-limit 1000 \
  --max-conn 512 \
  --slow-log-ms 5 \
  --idle-timeout-ms 60000 \
  --audit-dir /var/log/cuttledb/audit \
  --wal-dir /var/lib/cuttledb/wal
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full hardening
guide and reference deployment topologies.

## Verifying a Release Binary

CuttleDB release binaries are signed via
[sigstore](https://www.sigstore.dev/) (`cosign sign-blob` keyless
flow). Every binary in a GitHub Release ships with a matching
`.cosign.bundle` file containing the signature, the short-lived
signing certificate, and the Rekor transparency-log inclusion proof.

To verify a download (requires
[cosign v2.0+](https://docs.sigstore.dev/system_config/installation/)):

```bash
# 1. Checksum
sha256sum -c SHA256SUMS.txt

# 2. Signature
cosign verify-blob \
  --bundle cuttledb-server-linux-x64.cosign.bundle \
  --certificate-identity-regexp ".*" \
  --certificate-oidc-issuer-regexp ".*" \
  cuttledb-server-linux-x64
```

The `--certificate-identity-regexp` and `--certificate-oidc-issuer-regexp`
flags accept any signing identity — for stricter verification, replace
with the specific identity + issuer of the release maintainer (visible
by inspecting the bundle: `cat *.cosign.bundle | jq`). Cross-reference
the Rekor entry against the
[Rekor transparency log](https://search.sigstore.dev/) for an
independent audit trail.

## Disclosure

Once a fix is ready and an embargo period (typically 30–90 days) has
elapsed, we publish the advisory in `RELEASE_NOTES.md`, in the
GitHub Security Advisories tab, and link from `CHANGELOG.md` at the
patched version.
