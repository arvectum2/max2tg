# MAX2TG — public GitHub PR and CI publication gate

Date: 2026-11-23
Status: PREPARED LOCALLY; NO PUSH / PR / MERGE / DEPLOY

## Remote divergence

Canonical GitHub repository: arvectum2/max2tg; publicly visible.

- GitHub origin/main (remotely verified): 17a151d44bef5e4c9dd015055f026024aa9ad016
- Mac mini main (production checkout): c7b9c47911bdc6c37fe58b3942e0e98cbe91daf8
- Modernization commits atop local main: 76fb5e053e40048046b71b0f2c72b987356b2d62, 36d43fefdd7706c0fe32508cdd14435a7924c1dc.

10 local-only main commits contain approximately 1517 added and 144
removed lines across 38 files, including binary MAX v10 protocol,
durable intake and delivery recovery. The modernization branch includes
these 10 commits as ancestry. Opening a PR against the public GitHub main
immediately would unintentionally include those unrelated changes.

## Proposed sequence, NOT executed

1. Independently review the 10 unpublished main commits for behavior,
   license/IP, secrets, personal data, public visibility, security and
   governance. Do not force-push or silently rebase the running checkout.
2. Explicitly reconcile canonical GitHub main with those reviewed
   changes. This requires separate approval because the repository
   is public and the existing changes span production behavior.
3. Open a separate modernization PR only after reconciliation. Verify
   the PR diff contains the two scoped refactors and quality-gate changes.
4. Run hosted GitHub Actions: Python 3.12/3.14/3.15 full test suites,
   Node 26 watcher syntax and Docker image build+preflight. Capture real
   run IDs/logs, not just workflow syntax.
5. Review the precise release candidate and schedule the single
   approved deployment window with a tested rollback plan.
6. Do not use live accounts for destructive testing or replay UNKNOWN
   messages without independently verified delivery evidence.

## Validated local evidence

- Full candidate suite: 634 PASS on each of Python 3.12.16, 3.14.10,
  3.15.2. No live messenger acceptance performed.
- Python runtime and development locks are separate and aligned.
- Python 3.15.2 release preflight PASS; Python 3.14 safely rejected.
- Docker BuildKit static check PASS. Initial ARM64 native compilation needed GCC; an isolated two-stage Debian build now PASS, including the current pip 26.3 installer. Final offline/read-only smoke test, native ABI and pip check PASS; runtime lacks GCC.
- CI workflow uses checkout v7, setup-python v7, setup-node v7.
  Python 3.15.2 appears in GitHub python-versions manifest.
- No remote branch published, no hosted CI run claimed, and no running
  production bridge or queues changed.

This is source/release reconciliation evidence, not permission to
publish historic commits or restart the bridge.
