# MAX2TG — external configuration for immutable releases

Status: source-only / no production rollout
Date: 2026-11-23

## Why

The personal bridge loads .env via python-dotenv relative to the checkout.
An immutable release candidate has its own source root and must not copy or
symlink real credentials into its checkout merely to start.

## New opt-in behavior

MAX2TG_DOTENV_PATH is an explicitly configured absolute path to an existing
private .env file. When supplied, load_settings() loads that file without
overriding variables already in the process environment.

On POSIX, the file must be regular, not a symlink, owned by the current
process user and inaccessible by group/others (permission bits 0600 or
more restrictive). Invalid, missing or public files fail closed without
showing the configured path or secret values.

MAX2TG_NO_DOTENV=1 takes precedence: isolated SaaS capsules never inherit
personal bridge credentials.

If MAX2TG_DOTENV_PATH is not supplied, existing load_dotenv() behavior
remains unchanged. This is not a migration of existing credentials.

## Planned one-time release window (NOT performed)

For the reviewed immutable candidate release, configure a future launchd
wrapper to use:
- REPO = exact reviewed immutable candidate source directory;
- PY = corresponding locked Python 3.15.2 virtual environment;
- MAX2TG_DOTENV_PATH = existing privately owned production .env;
- STATE_DIR, LOG_DIR = previous state and log directories;
- preserve the external proxy readiness gate at 127.0.0.1:8080.

Never copy credentials, run a second personal bridge, reclassify UNKNOWN
messages, mutate queue DB schemas, or restart services during rehearsal.
Run strict preflight before serving. In the authorized window: take a
consistent fresh backup including SQLite WAL state, preserve the old
wrapper and runtime, stop/replace only the required launch label,
observe MAX/TG health, queue invariants and error rates, and restore
the previous wrapper/runtime on regression. Treat the separate watchdog,
session monitor and launchd KeepAlive as cutover dependencies.

## Verification

- Nine synthetic tests exercise private absolute paths, ownership
  policy, symlink rejection, precedence, existing-env preservation,
  no-secret-in-error messages, SaaS capsule isolation and legacy fallback.
- Never test with real Telegram or MAX credentials.
- Full product regression and GitHub CI required before merge.
- Production rollout requires separate approval and a single agreed window.

This change provides a safe configuration seam; it does not itself prove
live account acceptance or authorize the production migration.
