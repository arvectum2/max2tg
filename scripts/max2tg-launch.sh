#!/bin/zsh
set -u
REPO="/Volumes/ArvectumSSD/Arvectum/arvectum-max-bridge"
export STATE_DIR="/Users/master/.local/state/max2tg"
export LOG_DIR="/Users/master/Library/Logs/Arvectum/MAX2TG"
PY="$REPO/.venv/bin/python"

# After login an external SSD and the local Telegram proxy can appear a few
# seconds later than launchd. Wait instead of failing the service permanently.
while [[ ! -x "$PY" || ! -f "$REPO/.env" ]]; do
  sleep 5
done
while ! /usr/bin/nc -z 127.0.0.1 8080 >/dev/null 2>&1; do
  sleep 2
done

cd "$REPO" || exit 1
exec "$PY" -m app.main
