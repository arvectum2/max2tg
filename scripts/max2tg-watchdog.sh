#!/bin/zsh
set -eu
umask 077
SCRIPT="$HOME/bin/max2tg-watchdog.py"
PY="$HOME/.local/opt/max2tg-runtime/python-3.12.14/bin/python3.12"
# The watchdog must still run if the project's venv is broken.
if [[ ! -x "$PY" ]] || ! "$PY" -c "import json, ssl, urllib.request" >/dev/null 2>&1; then
  PY=/usr/bin/python3
fi
exec "$PY" "$SCRIPT"
