# MAX2TG isolated Linux acceptance

Purpose: close the previously blocked Linux cgroup v2 and physical-runtime acceptance gates without touching the personal bridge. This is an acceptance environment, not a production deployment.

## Local VM (Mac mini)

Reviewed Lima 2.3.0 template: `acceptance/lima-max2tg.yaml`, Ubuntu 24.04 ARM64, 2 CPUs, 4 GiB RAM, 24 GiB virtual disk, Apple Virtualization framework. Pinned official cloud image URL and SHA-256 from the installed Lima image template. No host filesystem mounts, SSH agent forwarding, containerd installation, automatic guest application port forwarding or automatic startup. Plain mode does not eliminate the VM's default outbound networking or its management SSH connection. Image availability and actual isolation must be verified after creation. Do not add real messenger credentials during provisioning.

Host preflight (2026-11-28): Mac mini arm64, 24 GiB RAM, approximately 179 GiB available internal disk, Lima 2.3.0 installed, no Lima instances; Docker Desktop cgroup2 available but read-only inside the existing container. Template validation PASS. A new VM downloads a pinned disk image and allocates guest resources; explicit operator authorization of the following host commands required before execution.

From the reviewed worktree, with no instance named `max2tg-acceptance`:

```sh
limactl validate acceptance/lima-max2tg.yaml
limactl create --tty=false --name=max2tg-acceptance acceptance/lima-max2tg.yaml
limactl start --tty=false max2tg-acceptance
```

Confirm instance status, architecture, kernel, systemd, cgroup2 and disk inside guest. Review actual mounts and forwarding, not merely the YAML. Transfer only a reviewed git archive of the exact acceptance commit into the guest, not .git, working environment, personal state, keys, or host home. Install pinned dependencies inside the guest only. Test all messenger behavior with synthetic endpoints first. Keep the VM isolated from production and do not start services automatically.

Stop with `limactl stop max2tg-acceptance` when the explicit acceptance session is complete. Do not delete the VM or any retained evidence automatically.

## Disposable delegated cgroup acceptance

The separate GitHub Actions workflow `delegated-linux-acceptance.yml` uses a hosted Ubuntu 24.04 runner, a dedicated systemd service with Delegate=yes, unprivileged service user, private network, no new privileges and bounded memory/CPU/tasks/time. The test fixture creates only a fresh nested subtree beneath its own unit, moves two exact owned children into separate leaf groups, checks recursive population, kills only that subtree, verifies both child exit statuses and recursive emptiness, and confirms the supervisor remains outside it. No host group discovery or arbitrary PID/path accepted. Scope and positive/negative evidence must be reviewed from the actual run. This is not production cgroup orchestration or authorization to launch a real broker.

## Remaining acceptance sequence

1. Confirm local guest resource, storage and isolation preflight.
2. Execute delegated-cgroup test in a reviewed private systemd service. Integrate independently proved group retirement into the recovery lifecycle only after success.
3. On an independently retained guest volume, verify real filesystem persistence and controlled guest restart with synthetic message journals; keep UNKNOWN and never replay it. A guest restart is not host power-loss evidence.
4. Perform sustained synthetic workload/idle monitoring, with explicit duration, no real tokens, and resource ceilings.
5. Separate test accounts MAX and Telegram: explicit owner authorization, test forum and account binding, actual delivery/receipt tests, loss and recovery. Production data, personal services and credentials excluded. No GO for SaaS before independent acceptance.
