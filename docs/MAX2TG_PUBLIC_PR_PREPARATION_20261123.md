# MAX2TG — public GitHub PR and CI publication gate

Date: 2026-11-23
Status: HISTORICAL DIVERGENCE RESOLVED; MODERNIZATION PR / CI PENDING; NO DEPLOY

## Historical remote divergence, resolved on 2026-11-23

Canonical GitHub repository: arvectum2/max2tg; publicly visible.

- GitHub origin/main (remotely verified): 17a151d44bef5e4c9dd015055f026024aa9ad016
- Mac mini main (production checkout): c7b9c47911bdc6c37fe58b3942e0e98cbe91daf8
- Modernization commits atop local main: 76fb5e053e40048046b71b0f2c72b987356b2d62, 36d43fefdd7706c0fe32508cdd14435a7924c1dc.

10 local-only main commits contain approximately 1517 added and 144
removed lines across 38 files, including binary MAX v10 protocol,
durable intake and delivery recovery. The modernization branch includes
these 10 commits as ancestry. Opening a PR against the public GitHub main
immediately would unintentionally include those unrelated changes.

## Executed publication of the historical changes and remaining review gates

1. **PASS**: scanned every unpublished patch and tracked tree for secrets,
   private keys, credentials, contact literals and suspicious binaries.
   Manual classification of 15 false-positive credential fields plus
   four high-entropy pathname matches found no exposed secrets.
2. **PASS**: safely reconciled 10 local production fixes with eight
   already-public GitHub commits (Spec Kit) in an isolated worktree.
   Combined main passed 610 Python tests + eight Spec Kit tests and a
   source criterion structural check. No production runtime changed.
3. **PUBLISHED**: GitHub main was fast-forwarded, no force-push,
   to merge commit 63f88b3fcb9d9a28cc10c947c5d328f53071dec8.
4. **NEXT**: create an independent modernization PR, verify its diff is
   only the scoped refactors and CI/toolchain changes, then run hosted
   Python 3.12/3.14/3.15 tests, Node 26 syntax and Docker build/preflight.
5. **AFTER TESTS**: review and merge the exact product candidate, but
   do not deploy/restart until the owner approves one maintenance window.
6. **STILL OUT OF SCOPE**: live destructive messenger acceptance and
   blind UNKNOWN message replay.

## Validated local evidence

- Full candidate suite: 634 PASS on each of Python 3.12.16, 3.14.10,
  3.15.2. No live messenger acceptance performed.
- Python runtime and development locks are separate and aligned.
- Python 3.15.2 release preflight PASS; Python 3.14 safely rejected.
- Docker BuildKit static check PASS. Initial ARM64 native compilation needed GCC; an isolated two-stage Debian build now PASS, including the current pip 26.3 installer. Final offline/read-only smoke test, native ABI and pip check PASS; runtime lacks GCC.
- CI workflow uses checkout v7, setup-python v7, setup-node v7.
  Python 3.15.2 appears in GitHub python-versions manifest.
- The reconciled historical main is now published. Modernization PR and
  hosted CI were pending when this evidence was recorded; production remains unchanged.

This records an owner-authorized review and publication of historical commits;
it does not authorize restarting or deploying the running bridge.
