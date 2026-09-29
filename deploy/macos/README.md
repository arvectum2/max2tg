# macOS autonomous runtime

Live Personal MAX2TG on the Mac mini uses two LaunchAgents:

- `com.arvectum.max2tg` — starts the bridge at login and keeps it alive.
- `com.arvectum.max2tg-session-watch` — checks the dedicated Chrome profile `MAX` every 30 seconds and restarts the bridge when MAX rotates its web session token.

Deployment files:
- `scripts/max2tg-launch.sh`
- `scripts/max2tg-session-watch.js`
- `deploy/macos/com.arvectum.max2tg.plist`
- `deploy/macos/com.arvectum.max2tg-session-watch.plist`

The bridge launcher waits until the external SSD and the local Telegram proxy on `127.0.0.1:8080` are available, so boot ordering is not significant.
The session watcher locates the Chrome profile by display name `MAX`, reads only the local MAX auth record, never prints the token, and atomically updates `.env` only when the token changes. It then calls `launchctl kickstart -k` for the main bridge.

macOS must grant Full Disk Access to the Node binary used by the watcher. The live configuration uses `/opt/homebrew/bin/node`.

A true MAX server-side logout cannot be bypassed: the user must authenticate again in Chrome profile `MAX` (for example via SMS). After the login succeeds, the watcher discovers the new session and restores the bridge automatically, normally within 30 seconds.

The watcher was end-to-end tested by replacing `MAX_TOKEN` with an invalid value: it restored the current Chrome session, restarted the bridge, and the bridge returned to `Authorized!`.
