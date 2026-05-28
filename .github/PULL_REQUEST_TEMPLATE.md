<!-- Thanks for the PR. Filling out this template makes review faster. -->

## What this PR does

<!-- One or two sentences. The "why" matters more than the "what" —
the diff already shows the what. -->

## Linked issue (if any)

<!-- Closes #N, refs #N, or "no issue — small fix" — all fine. -->

## Type of change

<!-- Check one. -->

- [ ] Bug fix (correctness; no API change)
- [ ] New feature (additive; doesn't break existing API)
- [ ] Breaking change (alters public adapter API or wire protocol)
- [ ] Docs / examples / benchmarks only
- [ ] CI / tooling

## Checklist

- [ ] Tests added or updated for the change (see `adapters/python/tests/`)
- [ ] `pytest adapters/python/tests/ -v` passes on my machine
  against a live `cuttledb-server`
- [ ] If user-facing: `CHANGELOG.md` entry under `[Unreleased]`
- [ ] If a public-API surface changed: docs updated
  (README / `docs/FEATURES.md` / `PROTOCOL.md` as appropriate)
- [ ] Lint clean (`ruff check adapters/python/cuttledb/` from
  `adapters/python/`)

## What I tested

<!-- "ran the full pytest suite," "added test_X, verified it fails
without the change and passes with it," "ran demo_coffee_shop.py
end-to-end," etc. The more specific the better. -->

## Things I'm uncertain about

<!-- Anything you'd specifically like a reviewer to look at? Design
choices you made under uncertainty? Performance tradeoffs? Now is
the time to flag them. -->
