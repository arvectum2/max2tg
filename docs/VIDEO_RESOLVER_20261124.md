# VIDEO_PLAY evidence and acceptance

Public browser bundle observed 2026-11-24:
https://web.max.ru/_app/immutable/chunks/D0OzoqHp.js
SHA256: 3d300974d8508a3674a5f56e4be618249fb4dea8661044398fbc96f91b32a181

The request/ack registry maps 83 to a video lookup with videoId (int64), optional chatId, messageId, postId, token and itemType. The normal chat viewer supplies videoId, chatId, messageId, token; the browser supplies postId for channel-post contexts where applicable. The response contains MP4_144/240/360/480/720/1080 and optional streaming manifests.

Use only MP4 candidates on the existing HTTPS allowlist and bounded downloader (50 MiB). Validate the ISO BMFF file-type header; this is not proof of decoding/playability. Do not send previews as successful delivery for tokenized videos. Bound source lookup retries to three; ambiguous Telegram writes remain quarantined. No changes to existing terminal ledger jobs or historical previews. Channel posts requiring explicit postId, HLS/DASH-only streams and large files without a smaller MP4 are not yet supported. Real source lookup and Telegram playback remain acceptance gates.
