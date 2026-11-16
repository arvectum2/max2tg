#!/bin/zsh
set -eu
umask 077
REPO="/Volumes/ArvectumSSD/Arvectum/arvectum-max-bridge"
export STATE_DIR="$HOME/.local/state/max2tg"
export LOG_DIR="$HOME/Library/Logs/Arvectum/MAX2TG"
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME || true
PY="$HOME/.local/opt/max2tg-runtime/venv-20261116/bin/python"
mkdir -p "$STATE_DIR" "$LOG_DIR"
function state() {
    printf '%s %s\n' "$1" "$$" > "$STATE_DIR/launcher.status.tmp"
    mv -f "$STATE_DIR/launcher.status.tmp" "$STATE_DIR/launcher.status"
}
while [[ ! -f "$REPO/.env" || ! -f "$REPO/app/main.py" ]]; do
  state WAIT_REPO
  sleep 5
done
if [[ ! -x "$PY" ]]; then
  state RUNTIME_MISSING
  exit 78
fi
while ! /usr/bin/nc -z 127.0.0.1 8080 >/dev/null 2>&1; do
  state WAIT_PROXY
  sleep 5
done
cd "$REPO"
state PREFLIGHT
# Preflight imports app modules from the repository, not a global environment.
if ! "$PY" -c 'import runpy; runpy.run_path("scripts/max2tg-preflight.py",run_name="__main__")'; then
  state RUNTIME_INVALID
  exit 78
fi
state STARTING
exec "$PY" -m app.main
