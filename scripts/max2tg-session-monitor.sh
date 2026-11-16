#!/bin/zsh
set -eu
umask 077
export PYTHONNOUSERSITE=1
unset PYTHONHOME PYTHONPATH || true
exec "$HOME/.local/opt/max2tg-runtime/python-3.12.14/bin/python3.12" "$HOME/bin/max2tg-session-monitor.py"
