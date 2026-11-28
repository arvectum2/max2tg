#!/usr/bin/env bash
# Explicit operator-approved acceptance only. Restarts ONLY the named guest.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
rev=${1:?exact full commit required}
[[ "$rev" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse "$rev^{commit}")" == "$rev" ]]
[[ -z "$(git status --porcelain)" ]]
[[ "$(git rev-parse HEAD)" == "$rev" ]]

git archive "$rev" | limactl shell max2tg-acceptance -- bash -lc '
set -euo pipefail
rev=$1
[[ "$rev" =~ ^[0-9a-f]{40}$ ]]
run="$HOME/max2tg-acceptance-run-$rev"
mkdir -m 700 "$run"
mkdir -m 700 "$run/code"
tar -x -C "$run/code"
[[ "$(findmnt -T "$run" -n -o FSTYPE)" == ext4 ]]
[[ "$(stat -fc %T /sys/fs/cgroup)" == cgroup2fs ]]
[[ -z "${SSH_AUTH_SOCK:-}" ]]
unit="max2tg-acceptance-$(date +%s)-$$.service"
sudo -n systemd-run --unit="$unit" --wait --pipe --collect \
  --property="User=$(id -un)" --property=Delegate=yes \
  --property=NoNewPrivileges=yes --property=PrivateNetwork=yes \
  --property=RuntimeMaxSec=45s --property=TasksMax=16 \
  --property=MemoryMax=128M --property=CPUQuota=100% \
  --property="WorkingDirectory=$run/code" \
  /usr/bin/python3 -B -m tests.linux_delegated_cgroup_fixture "$unit"
[[ ! -e "/sys/fs/cgroup/system.slice/$unit" ]]
sudo -n systemd-run --unit="max2tg-journal-${rev:0:12}-prepare.service" --wait --pipe --collect \
  --property="User=$(id -un)" --property=NoNewPrivileges=yes \
  --property=PrivateNetwork=yes --property=RuntimeMaxSec=30s \
  --property=TasksMax=16 --property=MemoryMax=128M \
  --property="WorkingDirectory=$run/code" \
  /usr/bin/python3 -B -m tests.guest_journal_persistence prepare "$run/max2tg-journal-state"
' bash "$rev"

# Graceful guest restart only after both stages have succeeded.
limactl restart --tty=false max2tg-acceptance

limactl shell max2tg-acceptance -- bash -lc '
set -euo pipefail
rev=$1
[[ "$rev" =~ ^[0-9a-f]{40}$ ]]
run="$HOME/max2tg-acceptance-run-$rev"
[[ "$(findmnt -T "$run" -n -o FSTYPE)" == ext4 ]]
sudo -n systemd-run --unit="max2tg-journal-${rev:0:12}-verify.service" --wait --pipe --collect \
  --property="User=$(id -un)" --property=NoNewPrivileges=yes \
  --property=PrivateNetwork=yes --property=RuntimeMaxSec=30s \
  --property=TasksMax=16 --property=MemoryMax=128M \
  --property="WorkingDirectory=$run/code" \
  /usr/bin/python3 -B -m tests.guest_journal_persistence verify "$run/max2tg-journal-state"
' bash "$rev"
limactl list
