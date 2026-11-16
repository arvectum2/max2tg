# Personal MAX2TG on macOS — 2026-11-16

Three LaunchAgents in the owner's GUI login domain:
- com.arvectum.max2tg: RunAtLoad + KeepAlive; waits for mounted repo and TCP proxy.
- com.arvectum.max2tg-watchdog: separate process every 60 seconds, checks PID and fresh heartbeat; restarts a hung worker subject to a 300-second cooldown. Alerts about persistent incidents at most daily after successful delivery.
- com.arvectum.max2tg-session-watch: read-only session readiness monitor, no Node or automatic extraction/replacement of browser sessions. A revoked MAX session requires owner reauthorization and supported bridge credential provisioning. macOS access denial is reported, not circumvented.

Runtime is isolated under ~/.local/opt/max2tg-runtime: standalone CPython 3.12.14, venv-20261116, installed from requirements.lock. No Homebrew Python, Node or global site-packages at launch. scripts/max2tg-preflight.py checks precise Python/package versions and imports before connecting. Do not update the production venv in place. Build a separate candidate runtime, run all tests and preflight, back up state, then switch the launcher atomically. Retain the previous launcher and runtime for rollback. Review security updates intentionally.

Persistent state on the internal disk: ~/.local/state/max2tg. Do not delete or replace live SQLite files or topics.json as a recovery shortcut. UNKNOWN means delivery is unconfirmed and must not be blindly retried. Catch-up continues past terminal gaps to recover later messages while retaining the unresolved history checkpoint. Long outages beyond the pagination limit still require reconciliation. Telegram Bot API history and update retention limit recovery of messages never received.

Deployment files are under scripts/ and deploy/macos/. Copy the three launcher/monitor shell scripts and two monitor Python scripts to ~/bin; copy the plist files to ~/Library/LaunchAgents, then load in gui/UID. Verify fresh health.json (PID must match live process), Telegram polling round trip, MAX authorization, queues, watchdog.json and session-watch.json. Run scripts/verify_backup_restore.py against the current state. Back up via SQLite backup API, not raw copies of WAL databases.

LaunchAgents start after the owner logs into macOS, not before login. Proxy availability and SSD mounting are external prerequisites. FileVault/login, total loss of power/network, MAX authorization revocation, and Telegram account/server outages cannot be made transparent by this bridge. Full physical reboot and end-to-end fault tests must be performed with explicit maintenance approval and a test chat/account where possible.
