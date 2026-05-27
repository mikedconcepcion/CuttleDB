# Contributing to CuttleDB

Thanks for your interest. CuttleDB is a small project — the contribution
surface is correspondingly focused, and this doc is short on purpose.

## What this repo contains

CuttleDB ships as one self-contained binary plus client adapters
(Python, JS/TS, WASM). This repository contains everything that a
user of CuttleDB interacts with: the adapters, the wire-protocol
reference (`PROTOCOL.md`), the docs, examples, demos, benchmarks,
tests, and the CI pipeline that runs them.

The server binary itself is built from a separate code base
maintained by the same author. Each GitHub Release ships pre-built,
sigstore-signed binaries for Linux, macOS, and Windows — see the
README's install section, or `SECURITY.md` for the verification
recipe. This open-core split is the v0.x model and isn't changing
for this release series; if it ever opens further, the change would
land here first.

If you're trying to debug server-side behavior beyond what `bench/`
and the test suite cover, open a discussion. We'd rather hear what
you're working on than have you reverse-engineer from logs.

## How to ask a question

- **Usage / "how do I…"** — open a GitHub Discussion (we'd rather it
  be public so the next person searching finds the answer).
- **Suspected bug** — file an issue using the bug template.
- **Security disclosure** — DO NOT open a public issue. See
  [`SECURITY.md`](./SECURITY.md). The channel is GitHub's private
  vulnerability reporting at
  `github.com/mikedconcepcion/CuttleDB/security/advisories/new`.
- **Feature request** — issue with the feature template. Be specific
  about the use case; we deflect generic "support X" asks.

## How to develop the adapters

```bash
git clone https://github.com/mikedconcepcion/CuttleDB.git
cd CuttleDB

# 1. Install the Python adapter into a fresh venv (recommended).
python -m venv .venv
.venv/bin/activate                      # POSIX
# .venv\Scripts\activate                # Windows PowerShell
pip install -e 'adapters/python[test]'  # adapter + pytest
pip install numpy                       # only needed for ML wire-verb tests

# 2. Get a cuttledb-server binary: download from the latest GitHub
#    Release (see README), put on PATH or set:
#        export CUTTLEDB_SERVER_BIN=/path/to/cuttledb-server

# 3. Start the server in another shell:
cuttledb-server --port 7780

# 4. Run the test suite (from the repo root):
pytest adapters/python/tests/ -v
```

The command in step 4 works from any directory; using the full path
keeps the doc copy-paste-safe regardless of where you start.

JS adapter tests live at `adapters/tests/smoke.mjs` and run against
the same server with `node adapters/tests/smoke.mjs`.

## Coding conventions

These are loose but real:

- **Python**: PEP 8 in spirit; `ruff check` should pass (run from
  `adapters/python/`). Black-compatible formatting; line length 88.
- **JavaScript / TypeScript**: 2-space indent, ESM modules, no
  build step in the adapter (`adapters/cuttledb.js` ships as-is).
- **Tests are mandatory** for any new public-API surface. Add a
  test to `adapters/python/tests/` for Python changes; the test
  file naming follows `test_<verb>.py` (e.g. `test_max_conn.py`).
- **Commit messages**: imperative present tense, ~70 char subject,
  body explains why not what. We don't enforce a template but the
  existing log (`git log`) shows the style.

## How to submit a pull request

1. Fork + branch from `main`.
2. Make your change. Add tests.
3. Run `pytest adapters/python/tests/ -v` against a live
   `cuttledb-server` — it must pass on your machine before you push.
4. Open the PR. Use the PR template's checklist.
5. CI runs adapter lint + tests across Linux + macOS + Windows × Py
   3.10 and 3.12 (see `.github/workflows/ci.yml`). It needs to pass
   before review.
6. If your change is user-facing, add an entry to `CHANGELOG.md`
   under `[Unreleased]`.
7. Reviewers may ask for scope reduction; CuttleDB stays small on
   purpose. Don't take pruning personally.

## What we say no to (so you don't waste a PR)

- **Server-side changes** can't be reviewed here (no C source in
  this repo). Open a discussion describing what you'd want; we can
  consider it for the next release.
- **SQL parser**. CuttleDB intentionally uses a Redis-style line
  protocol. We won't add a SQL surface.
- **Built-in inference / LLM hosting**. CuttleDB is the substrate
  for agents, not a model host.
- **Adapters in new languages**: open a discussion first. Maintaining
  an adapter long-term is a real commitment.
- **Premature optimization on the read path** without a bench
  number. `bench/` is where perf claims earn the right to be made.

## License of contributions

CuttleDB is licensed under [Apache License 2.0](./LICENSE). By
submitting a contribution (issue comment, pull request, or
otherwise), you agree that your contribution is provided under the
same Apache 2.0 license, with the patent grant that license
includes. We don't currently require a separate Contributor License
Agreement (CLA); the inbound = outbound Apache-2.0 model covers the
v0.x series. If that changes (e.g. for a future commercial
sub-license), we'd ask for explicit consent before applying any
different terms to past contributions.

## Code of Conduct

[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) — Contributor Covenant
2.1. Enforcement reports go through GitHub's private channel at
`github.com/mikedconcepcion/CuttleDB/security/advisories/new` (the
same private path used for security disclosures; it's the cleanest
private contact route GitHub provides until a dedicated alias is
configured).

Thanks for reading this far. Now go ship something.
