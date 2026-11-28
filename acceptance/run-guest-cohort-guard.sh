#!/usr/bin/env bash
# Explicitly approved disposable guest service only; no restart.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
rev=${1:?exact full commit required}
[[ "$rev" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse HEAD)" == "$rev" && -z "$(git status --porcelain)" ]]
git archive "$rev" | limactl shell max2tg-acceptance -- bash -lc '
set -euo pipefail
rev=$1
[[ "$rev" =~ ^[0-9a-f]{40}$ ]]
run="$HOME/max2tg-guard-run-$rev"
mkdir -m 700 "$run"
tar -x -C "$run"
[[ "$(findmnt -T "$run" -n -o FSTYPE)" == ext4 ]]
[[ -z "${SSH_AUTH_SOCK:-}" ]]
for role in broker controller controller_exit; do
  case "$role" in broker) suffix=1;; controller) suffix=2;; controller_exit) suffix=3;; esac
  unit="max2tg-acceptance-$(date +%s)-$$${suffix}.service"
  sudo -n systemd-run --unit="$unit" --wait --pipe --collect \
    --property="User=$(id -un)" --property=Delegate=yes \
    --property=NoNewPrivileges=yes --property=PrivateNetwork=yes \
    --property=RuntimeMaxSec=45s --property=TasksMax=16 \
    --property=MemoryMax=128M --property=CPUQuota=100% \
    --property="WorkingDirectory=$run" \
    /usr/bin/python3 -B -m tests.linux_delegated_cgroup_fixture "$unit" "$role"
  [[ ! -e "/sys/fs/cgroup/system.slice/$unit" ]]
done
' bash "$rev"
