# P2.2b — trusted topic routing and token-free text callback adapter

Base: GitHub main 4dba3fc. Date: 2026-11-25. Offline development slice only; NOT deployed. P2.2b remains open. P2.2c is not accepted.

## Implemented

- A gateway-owned immutable topic binding `(session_id, MAX chat ID) -> (Telegram forum, thread)` with owner authorization and active capsule/forum checks. One forum thread cannot be assigned twice; disabled bindings cannot silently be reused. The trusted operator must verify that the Telegram topic actually exists before binding; no new Telegram topics are created by this slice.
- Scoped signed egress requests select a MAX chat, never a Telegram destination. Central SQLite persists the resolved thread, revalidates the route before dispatch, and requires a positive message ID and exact forum/thread in the Bot API receipt. Incorrect or ambiguous receipts remain UNKNOWN; revoked routes are BLOCKED. The legacy General route remains compatible, but `GatewayEgressIpcServer(..., topic_scoped=True)` rejects unscoped requests and scoped status checks require the original MAX chat.
- `SharedCapsuleTextListener.attach()` connects the MAX message callback to the worker-private durable intent queue; immutable source keys derive from both MAX chat and message IDs. `drain()` reconciles signed central acknowledgements and repairs the per-capsule reply index only from SENT receipts. Telegram-originated echoes are suppressed using durable ingress receipts or the client's verified correlation. No Bot API token or call in this adapter.
- `TelegramGatewayEngine(..., topic_scoped=True)` and the optional trusted-topic check in `SharedCapsuleTextIngress` reject unknown/foreign threads and mismatched worker-local maps. The ingress ledger uses FULL synchronization and checks its dispatch claim. Paused or unauthorized worker cannot initiate a new MAX send.

## Verification

Six new tests using real Unix sockets, two synthetic tenant identities and simulated messenger I/O: identical source IDs and thread numbers stay isolated across forums; full text round-trip and echo suppression; durable receipt index rebuild; revoked topic before send; wrong-thread receipt becomes UNKNOWN; immutable and owner-scoped binding; status route mismatch; worker-local mapping mismatch; strict mode rejects General fallback. Entire local suite 806 passed, Ruff targeted checks and runtime preflight passed; original Spec Kit source gate passed unchanged.

## Constraints and next gate

This adapter handles prebound, plain text only. No automatic topic provisioning, shared worker entrypoint or supervised gateway loop, media, rich formatting, reply sends, history catch-up, edit/delete/reaction integration. Unsupported/unbound callbacks return explicit outcomes; durable handling of these outcomes and catch-up cursors are still required before a real runtime can be enabled. Legacy dedicated worker and existing production bridge are unchanged. The supervisor's `SHARED_RUNTIME_NOT_READY` guard stays in place. Tests simulate both messenger boundaries within one host/process; they do not establish OS-level isolation, cross-process resilience, safe polling-offset recovery, actual MAX/TG delivery, or SaaS readiness.

Next: isolated shared worker/gateway lifecycle with durable ingress-before-offset and trusted route provisioning, explicit recovery/unsupported-event journaling and process-boundary tests. Only after that review should the shared worker start guard be lifted for an approved isolated account. Media and reply semantics remain separate work; no destructive tests against the personal account.
