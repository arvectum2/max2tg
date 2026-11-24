# MAX2TG — Personal release update, 2026-11-25

Operational progress supplement to the [pinned roadmap](ROADMAP.md). This document does not change the original criterion/task states, execution authority or isolated acceptance requirements.

## Personal media and reliability · 2026-11-25

- [x] Native video and voice, file resolution, WebP normalization, service-event filtering, photo history and safe pagination. Live delivery verified in both directions for native video and voice.
- [x] Photo and mixed photo/video albums: native attachments, ordered receipt validation, real multipart serialization, durable aggregation, no blind replay. Live mixed album acceptance: MAX→TG 2.05 s, TG→MAX 10.60 s; two-photo TG→MAX 5.68 s. Multiple captions numbered with preserved formatting (offline tests only). Actual playback not independently checked. Limits: 2–10 items, 25 MiB/item and 50 MiB aggregate TG→MAX; 45 MiB aggregate MAX→TG.
- [x] Diagnostics: paginated /issues, unique hashed delivery refs, partial-receipt counts; BLOCKED edit review does not apply the edit. Isolated restore of all three ledgers and topic state passed, including ingress and pending album counts. No state replacement.
- [ ] Historical reconciliation: 9 uncertain albums (11 confirmed individual receipts), 2 legacy control events, 3 failed synthetic tests, 1 failed reverse document, 4 blocked edits, 3 history warnings. Preserve receipts and cursor; no bulk replay or status reset.
- [ ] Separate isolated physical fault-injection P0.5b remains deferred (no separate account). SaaS live acceptance not complete. Main branch merge subject to CI review.
- Local one-shot acceptance/cleanup scripts, specific private message IDs, backups, and journals intentionally excluded from repository. The reviewed release removes the account-specific video-preview test command and adds portable FFmpeg discovery plus Linux CI/container media dependencies.

