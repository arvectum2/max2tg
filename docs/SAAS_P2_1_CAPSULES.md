# P2.1 — Multi-user Session Capsule, safe offline prototype

Date: 2026-10-23. The personal bridge on Mac mini is unchanged.

## What works in this slice

One internal account ID owns one or more opaque capsule IDs. Each capsule has:
- its own directory tree with mode 0700:
  state/, logs/, browser-profile/ (reserved for future interactive login);
- its own JSON credentials file with mode 0600, outside the repository;
- a separate MAX user WebSocket client process and Telegram Bot polling process
  when activated by the operator;
- an independent SQLite outbox, Telegram topic map, history cursors and
  health.json, all under that capsule's STATE_DIR;
- a persistent supervisor registry (capsules.sqlite3) containing session
  IDs, statuses, requested activation and Telegram routing metadata, but
  **no MAX token, bot token or browser cookies**;
- an operating-system flock for each capsule preventing duplicate workers
  after a supervisor restart, plus a separate singleton supervisor lock;
- crash backoff, status monitoring, clean shutdown and read-only per-process
  resource sampling.

The existing single-account worker starts exactly as before via the current
launchd job; no new capsule is currently configured with real credentials
and no SaaS supervisor has been installed into launchd.

## Operator CLI

Use the local command, not a public endpoint:

    .venv/bin/python scripts/capsule_admin.py list
    .venv/bin/python scripts/capsule_admin.py create-user
    .venv/bin/python scripts/capsule_admin.py create-session <opaque-user-id>
    .venv/bin/python scripts/capsule_admin.py configure <capsule-id> --credentials-file /private/location/account.json
    .venv/bin/python scripts/capsule_admin.py activate <capsule-id>
    .venv/bin/python scripts/capsule_admin.py supervise
    .venv/bin/python scripts/capsule_admin.py deactivate <capsule-id>

The supplied credential file must have mode 0600, be created manually
outside the repo and contain MAX_TOKEN, MAX_DEVICE_ID, TG_BOT_TOKEN,
TG_CHAT_ID (private forum supergroup), TG_ADMIN_USER_ID. A per-capsule
TG_ALLOWED_USER_IDS can be set; the bridge adds the admin to the ACL.
Do NOT reuse the current personal MAX credentials, MAX Chrome profile,
Telegram bot token or Telegram supergroup.

The operator must deactivate the capsule before replacing credentials.
Missing credentials are a hard error; workers do not fall back to the
existing repository .env. The supervised worker inherits only a minimal
explicitly whitelisted environment. Secrets never appear as CLI positional
arguments or in the SQLite registry.

## Offline acceptance / safety

tests/test_capsule_saas.py launches **two real local Python worker
processes** using tests/capsule_dummy_worker.py. These dummy processes
never contact MAX or Telegram and never read credentials beyond their
environment: they assert separate process IDs, private state/log/profile
paths, independent Telegram routing IDs and no parent environment leak.

Also tested:
- duplicate TG bot or Telegram supergroup blocked at registration;
- no start without operator activation;
- parent environment and personal .env isolated;
- group-readable credential file rejected;
- crash exits, waits via exponential backoff and restarts same tenant;
- one inactive session can be stopped while another remains online;
- OS lock prevents duplicate worker when a previous worker is still alive;
- unsafe pre-existing SaaS root refused without changing permissions;
- supervisor singleton and SQLite metadata persistence.

We do NOT claim cross-OS-user isolation, encryption at rest, browser
login completion, live two-account MAX traffic, or production SLA.
Current file mode separation protects against other OS users only,
not arbitrary code running as the same macOS user. The production
variant needs isolated Unix users/containers and secure key management.

## Architecture boundary

The initial prototype requires a **dedicated Telegram bot token** and a
private forum supergroup for each capsule. A shared bot cannot safely
be polled by multiple independent workers: Telegram getUpdates has
one active consumer per token. To offer customers one public entry bot,
build a central Telegram ingestion/dispatch layer before SaaS beta.

The browser-profile directory is an allocated placeholder, not an active
browser worker. Future onboarding must authenticate users normally via
MAX web, attach their authorized session explicitly to a capsule, and
handle reauthorization; do not scrape SMS codes.

## Remaining external validation

P0.5b and P1 field acceptance are NOT closed. An isolated MAX test
account, separate TG bot and private test forum are needed to validate
real login/logout, format/media, catch-up, duration and resource use.
Only after two distinct live accounts pass these checks should the
macOS supervisor run as a persistent service.

Next P2 task: shared Telegram control plane / single public entry bot,
onboarding gateway with explicit account authorization and 2 live test
sessions. PostgreSQL, encryption at rest, RKN/PDn obligations, Timeweb
migration and billing are later stages.
