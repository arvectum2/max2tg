# P1 UX / P0.5b physical acceptance — still open

Date: 2026-10-23.

P1.1–P1.4 passed automated tests, including HTML formatting, albums,
attachment fallbacks, search/history, per-chat mute/pause/archive,
outbox, restart/crash and no-duplicate protections.

No synthetic real-user MAX messages were injected into the personal
production Telegram chats. No browser was logged out and no network,
external disk or MAX live account was disconnected.

The following checklist requires isolated real MAX + Telegram accounts:
- Login via ordinary MAX web UI, restart just the capsule, verify ONLINE
  and correct history cursor with real ID/timestamps.
- Send from MAX a rich-text+emoji message, media album, video, audio,
  document, reply and edit; verify correct Telegram topics and mappings.
- Send and reply from Telegram; verify MAX side.
- Pause chat, send offline messages, resume and audit missing/duplicated IDs.
- Simulate test-only logout, network failures, worker crash and stale CDN URL;
  verify UNKNOWN handling and operator status.
- Test simultaneous distinct authorized MAX accounts for 24h and obtain
  per-worker RSS/CPU/network samples. Establish capacity and battery/SSD
  assumptions without extrapolating from dummy worker RSS.

Gate outcome: AUTOMATED PASS, PHYSICAL PENDING. Do not claim live
multi-user acceptance, 24-hour operation, or end-to-end production readiness.
