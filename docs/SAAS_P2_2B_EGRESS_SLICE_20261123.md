# P2.2b — central Telegram text egress: offline slice 1

2026-11-23. Candidate only; NOT DEPLOYED. P2.2b remains OPEN.

## Implemented
- Private central SQLite/WAL text outbox within capsule registry root;
  strict UTF-16 size bounds and immutable (session_id, source_key) dedup.
- Chat targets are resolved only through ACTIVE control-plane routes with
  active capsule AND bound Telegram owner. No worker-provided chat ID,
  thread ID or shared bot token accepted.
- On revoked access, no network calls; state BLOCKED.
- QUEUED -> SENDING -> SENT only on positive Bot API message_id.
  Lost response, cancellation or crash becomes UNKNOWN, never auto-replayed.
- Gateway optionally exposes enqueue_egress_text / drain_egress; no
  shared bot or workers launched by these methods.
- 25 isolated tests: two tenants, dedup, mutation, owner/forum revocation,
  timeout, cancellation, competing senders, concurrent gateway restart,
  false Bot API chat acknowledgements, metadata bounds,
  UTF-16, paths, gateway and replay protection.

## Explicitly NOT implemented
- No worker-to-gateway outbound IPC, no trusted MAX chat -> forum thread
  mapping, no media/edit/delete/reaction routing or ID handback.
- P2.1 tenant workers still have legacy per-session bot token/polling.
  Do not migrate these without a separately reviewed worker mode.
- No real Bot API tests, two independent MAX/Telegram accounts,
  no production process, token, queue, database or config changes.

## Remaining P2.2b and P2.2c gates
1. Separate shared-bot worker mode without a bot token in tenant workers.
2. Authenticated bounded two-way IPC from worker to central egress with
   durable worker intent and explicit ambiguous-delivery semantics.
3. Trusted topic/thread mapping, media/edit/delete and persistent
   message-ID result handback.
4. Two-worker synthetic fault injection and actual isolated test-account
   acceptance. P2.2c is a separate live gate.

Offline technical PASS is not SaaS readiness or production approval.
