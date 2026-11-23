# MAX2TG P2.2b — shared-bot worker isolation and bidirectional IPC

Date: 2026-11-23. Status: standalone reviewed candidate; NOT deployed.
P2.2b remains OPEN until the MAX client runtime uses the shared adapter.

## Implemented

- Capsule registry supports opt-in dedicated / shared Telegram modes.
  Dedicated default remains compatible. Shared worker credentials contain
  no TG_BOT_TOKEN, and the environment scrubber removes inherited token,
  Telegram proxy and private dotenv path. A private per-capsule HMAC key
  is used for IPC.
- Explicit reverse Unix IPC from shared worker to the one Telegram gateway.
  Direction-separated HMAC keys, per-request nonce, bounded signed frames,
  per-capsule identity and gateway-side ACTIVE forum/owner authorization.
  Central journal commits the outgoing text *before* reporting QUEUED.
  Gateway alone holds the bot token and makes Bot API calls.
- Worker-private SQLite/WAL intent is fsynced BEFORE IPC. A lost IPC ACK
  is reconciled with the same immutable source key, never a new message.
  Delivery status remains ACCEPTED until the gateway reports actual
  Telegram SENT with a positive Telegram message ID. UNKNOWN / BLOCKED
  never get an automatic replay.
- The existing signed gateway->worker IPC now distinguishes definite
  no-MAX-call WorkerUnavailable from an ambiguous failure. Gateway keeps
  RECEIVED retryable only when the worker explicitly attests no send
  attempt was made.
- Token-free text-only inbound adapter maps Telegram message_thread_id
  through the *per-capsule* TopicStore to the MAX chat and persists intent
  in the already-existing TelegramOutbound before any MAX send. Unmapped
  threads, cross-tenant actors, media and replies fail closed.
- Offline tests run two independent tenant session credentials, both
  IPC directions, real local Unix sockets, fake MAX replies, lost acks,
  ambiguous network outcomes, revoked ACL, dedup and restart safety.
- Opt-in shared capsules cannot accidentally start the dedicated personal
  Telegram polling app. The supervisor explicitly rejects starting them
  until the full MAX-client runtime adapter is implemented.

## Main-account Bot API smoke

The owner authorized testing with the current production Telegram identity.
One diagnostic text message was sent using the new reverse worker IPC,
central durable journal and the *existing* Bot API token. The Bot API
returned SENT and a valid message ID was persisted to the private worker
journal. This called Bot API send_message only. No getUpdates, second
poller or service restart was performed. No token or private message
content was stored in source control. Evidence is private under
/Volumes/ArvectumSSD/Arvectum/staging/max2tg-p22b-live-20261123.

## Remaining work

- Complete an explicit MAX listener/TelegramSender adapter for shared
  workers; the personal MAX2TG entrypoint must remain separate. Then
  deliberately enable real shared capsules.
- Preserve MAX-chat->Telegram-thread mappings in the trusted gateway.
  Current central egress sends text to the general forum topic only.
- Expand to media/edit/delete/reaction semantics and reconcile Telegram
  message IDs to MAX message IDs for replies.
- Final one-window production promotion after current SQLite backups,
  controlled source/runtime switch and measured rollback; never
  blindly replay UNKNOWN messages.
- The P2.2c multiuser acceptance milestone is independent of the single
  main-account Bot API transport smoke.

Production MAX2TG, queues, credentials and launchd remain unmodified
by this change. A technical PASS does not imply SaaS readiness.
