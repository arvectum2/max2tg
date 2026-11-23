# MAX2TG — Product-first handler refactor and runtime modernization

Date: 2026-11-23
Status: **STAGING VALIDATED; NO PRODUCTION DEPLOYMENT**
Repository: arvectum2/max2tg
Parent source: 76fb5e053e40048046b71b0f2c72b987356b2d62
Branch: refactor/max2tg-handler-modernize-20261123

## Objective and scope

Improve maintainability of the large Telegram handler without changing the
MAX/TG protocol, message delivery semantics, persisted mapping, outboxes,
UNKNOWN states, credentials or operational behavior. Modernize to the newest
stable toolchain and libraries that pass tests together, retaining the existing
development compatibility matrix. Release/runtime ownership stays with MAX2TG;
shared Company policy is not a source of authority.

## Code changes

- Extract pure Telegram UTF-16 entity conversion into app/telegram_entities.py.
- Extract normalized MAX search results/contact names into app/max_search.py.
- Extract the actual /help command handler and help text into app/tg_help.py.
- Keep function identities and legacy imports from app.tg_handler unchanged.
- Correct an existing menu-bind exception path: on topic creation failure,
  return a generic localized warning rather than throw NameError from an
  undefined variable or expose raw exception details to Telegram.
- Add contract, negative/error-path and re-export identity tests, all mocked
  without live MAX or Telegram connections.

tg_handler.py shrank from 2721 to approximately 2544 lines. This is an
incremental refactor, not a comprehensive decomposition of all handlers.

## Current stable runtime baseline candidate

The tested preferred Python target is 3.15.2 (current secure maintenance
release as of November 2026), with full test compatibility preserved on
Python 3.12.16 and 3.14.10.

Dependencies are resolved using uv's newest compatible package candidates,
with requirements locked against Python 3.12 (oldest supported) to retain
typing-extensions needed only on earlier interpreters. The selected versions
of all packages common to the Python 3.15-only resolution are **identical**
to the Python 3.12-compatible lock; the only additional package is
typing-extensions==4.16.0. No forced downgrade was necessary.

Representative runtime and dev changes:
- aiohttp 3.14.3 -> 3.14.5
- anyio 4.15.1 -> 4.16.0
- multidict 6.9.1 -> 7.2.1
- certifi 2026.7.22 -> 2026.11.20
- python-dotenv 1.2.3 -> 1.2.4
- pytest 9.1.1 -> 9.2.0
- pytest-asyncio 1.4.0 -> 1.5.0
- Ruff 0.17.6 for code review, added to development dependencies.
- python-telegram-bot 22.8, msgpack 1.1.2 and lz4 4.4.4 remain
  unchanged since they are the current latest resolution for this stack.

Reproducible lock layout:
- requirements.txt: direct application requirements
- requirements.lock: newest compatible resolved application runtime only
- requirements-dev.in: application + test/lint tool requirements
- requirements-dev.lock: resolved application + dev tools (runtime pins align)
- .python-version: preferred Python 3.15

Preflight explicitly requires Python 3.15.2 and checks all runtime pins,
native imports, TLS and in-memory SQLite. It rejects mismatched runtimes
before initiating any messenger connection. Previous preflight was hard-coded
to 3.12.14 and could not parse uv-generated comment lines.

A previous two-stage Dockerfile moved Debian glibc packages to Alpine musl,
which is unsafe for Python native wheels. Initial Linux ARM64 build of
the candidate failed: lz4 and msgpack require compilation under Python
3.15.2, but slim lacked GCC. The revised Dockerfile uses the same pinned
Debian Python 3.15.2-slim image for both builder and runtime stages,
installs build-essential only in the builder, and copies a validated venv
into the compiler-free runtime image. Production Docker is untouched.
Official Docker Hub tag metadata confirmed image exists with amd64 and arm64
manifests; actual candidate Docker **build has not yet been run**.

Node.js session watcher uses built-in Node libraries only and requires no npm
dependencies. Mac mini Node 26.13.1 LTS syntax check passed; CI Node job checks
syntax without executing the watcher or accessing browser credentials.

## Validation completed on Mac mini ARM64

Isolated staging: /Volumes/ArvectumSSD/Arvectum/staging/max2tg-handler-modernize-20261123

- Existing baseline on Python 3.15.2 + latest pinned packages: 617 PASS.
- Candidate after refactor and new safety tests: 634 PASS each on
  Python 3.12.16, 3.14.10, and 3.15.2 = 1902 PASS executions.
- uv pip check: PASS on all three virtual environments.
- Candidate Python 3.15.2 preflight: PASS (also direct script invocation without PYTHONPATH).
- Negative preflight Python 3.14: correctly rejected (exit 1).
- Ruff F rules: PASS for touched application modules, scripts and new tests.
- Historical repo-wide Ruff F baseline: 73 findings; current remaining
  legacy findings: 71, outside this scoped refactor. CI deliberately gates
  touched modules until legacy cleanup has independent tests and approval.
- Live Telegram/MAX integration, physical fault injection, hosted CI and
  Docker build: NOT executed. No claims about production latency or delivery.

## Non-regression and safety

Personal bridge launchd service, existing source main checkout, production
Python 3.12.14 environment, session watcher, token rotation, queued deliveries,
message history, state and databases were NOT changed or restarted.

The production launch wrapper and its separate watchdog / session monitor
still reference the previous runtime and require a coordinated update.
Do not merge this branch into a live checkout and restart the service against
its original runtime: the new strict preflight would reject that mismatch.
Prepare an immutable candidate checkout plus venv, verify old vs new source
and queue-state safety, run protected CI and smoke tests with test accounts,
then obtain explicit Owner authorization for the cutover.

Roll back by restoring the exact previous checkout and pinned virtualenv.
Never blindly replay UNKNOWN messages or reset durable queues. The roadmap's
isolated P0.5b physical acceptance and SaaS P2.2b/c remain separate work.

## Historical GitHub divergence resolved

The 10 local reliability commits were scanned for secrets and merged
with eight existing remote Spec Kit commits. Canonical GitHub main is
now merge commit 63f88b3fcb9d9a28cc10c947c5d328f53071dec8;
running local main remains unchanged until the approved deployment.
See docs/MAX2TG_PUBLIC_PR_PREPARATION_20261123.md for evidence.
Publish the modernization separately as an independently tested PR.

## Docker ARM64 build, direct smoke evidence and installer currency

The initial Docker ARM64 build failed because lz4 and msgpack needed
GCC for CPython 3.15.2. An isolated two-stage Debian build (builder
has build-essential, final runtime has no GCC) subsequently SUCCEEDED
using pip 26.2.2. Direct smoke checks on the built image passed:
preflight with no network/read-only filesystem, Linux native ABI
round-trip for lz4/msgpack, pip check, and absence of GCC. Image was
268900383 bytes, linux/arm64. The builder and CI are now adjusted
to use current stable pip 26.3 (released 2026-11-02) explicitly.
The final image with pip 26.3 was rebuilt successfully on Linux/arm64;
the image size is 270425148 bytes. Its no-network, read-only preflight,
native lz4/msgpack ABI, pip check and compiler exclusion all PASS.
Its image ID is sha256:8fbb9e5780bcac7c9c829d0dc7b3b96920f7d84f2e61f509841dd2cded299e55.
No production runtime or application dependency was changed.
