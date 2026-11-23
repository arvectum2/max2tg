# MAX2TG — Telegram HTML chunking refactor (2026-11-23)

Status: source-only / staging-validated / NOT DEPLOYED.

## Motivation

The Telegram transport module mixed network/API I/O and pure HTML/UTF-16
chunking. Moving deterministic transformation to a separate module reduces
surface area for the transport layer and allows independent unit tests.
This is a behavior-preserving refactor, not a production performance claim.

## Changes

- Extract split_telegram_html, _HTMLChunker, _take_part, _utf16_length
  and text/caption capacity constants into app/telegram_html.py (stdlib only).
- Keep old app.tg_sender imports as explicit compatibility re-exports.
- Add tests for astral Unicode, combining marks, HTML escaping, balanced
  chunk boundaries, empty inputs and invalid capacities.
- Pin preferred developer version to Python 3.12 without changing
  application dependency versions or removing Python 3.14 support.
- Add a GitHub Actions full-test matrix for Python 3.12 and 3.14 from
  requirements.lock. The existing GitVerse mirror workflow is unchanged.
- Correct the stale JSON v11 wire transport description in CLAUDE.md
  to the deployed binary MAX v10 implementation.

## Verification

Source baseline: c7b9c47911bdc6c37fe58b3942e0e98cbe91daf8.

Staging path:
  /Volumes/ArvectumSSD/Arvectum/staging/max2tg-refactor-20261123

Package sets installed from unchanged requirements.lock into separate
Python 3.12.14 and Python 3.14.10 environments.

- Pre-change selected module baseline: 54 PASS (Python 3.12).
- Post-change selected module tests: 54 PASS (Python 3.12).
- Full suite: 617 PASS, 21 warnings (Python 3.12).
- Full suite: 617 PASS, 21 warnings (Python 3.14).
- uv pip check: PASS on both environments.
- Ruff F-series, changed Python files: PASS.
- New tests establish pure stdlib dependencies and preserve old import aliases.

## Limits and deployment gate

No live message dispatch, reconnect, edit/delete, UNKNOWN queue changes,
application env update, version upgrade, restart or production writes.
Physical P0.5b failure acceptance still requires isolated test accounts.
Hosted CI has NOT yet been verified because this is a local review branch.
Merge/deploy only after review and an approved window, with reconciliation
and rollback; never blindly resend UNKNOWN messages.
