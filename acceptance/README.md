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

## Observed local VM preflight (2026-11-28)

Owner-authorized exact create/start commands exited 0 (ARC job 0c030a6e-873d-4ff5-a21f-ee2ad24f0f28). Lima VZ instance max2tg-acceptance Running, official pinned image downloaded, management SSH loopback 127.0.0.1:64577 verified by host listener. Guest Ubuntu 24.04.5 LTS aarch64, kernel 6.8.0-147-generic, 2 CPUs, 3905 MiB RAM (3556 MiB available), ext4 root 23 GiB usable / 21 GiB free. PID 1 systemd, cgroup2 rw with nsdelegate, cgroup.kill present. No virtiofs/9p/sshfs/NFS mounts, SSH_AUTH_SOCK absent. Only guest SSH and local DNS TCP listeners observed. Default NAT route exists; this is not an air-gapped guest. Python 3.12.3 and cryptography available; pytest absent. No application code, dependencies or messenger credentials transferred. Actual local cgroup and persistence tests pending.

Remote PR #77 delegated-cgroup job 140912072733 succeeded: two owned descendant leaves, recursive kill, empty subtree, supervisor survived. All seven checks passed; merged 9f63eb8529288c01dec2cebd0d89408365108e03. This validates the disposable hosted runner, not local guest or production lifecycle.

## Local delegated cgroup acceptance (2026-11-28)

Owner-authorized git archive of exact main 9f63eb8529288c01dec2cebd0d89408365108e03, transferred into a new guest temporary directory, and the reviewed private systemd service fixture exited 0. Unit max2tg-acceptance-1795856438-1311.service: two owned descendant leaves, recursive kill, empty subtree, supervisor survived. Service runtime 164 ms, CPU 147 ms, peak memory 1.7 MB. Subsequent independent readback: LoadState=not-found, ActiveState=inactive, test cgroup removed. Guest remains Running, Linux 6.8.0-147-generic, ext4, cgroup2 rw. No real credentials or host process control. This establishes the local delegated kernel boundary; production process-tree orchestration, external guardian lifecycle, guest restart/persistent journal acceptance and real account acceptance are still open.

## Next reviewed combined guest session

Run only after explicit owner approval of the exact commit and guest restart: `bash acceptance/run-guest-retirement-persistence.sh <full-reviewed-HEAD>` from the clean acceptance worktree. The script transfers only tracked source to a new private guest directory, exercises durable delegated retirement and prepares synthetic SQLite journals in isolated transient services, gracefully restarts only max2tg-acceptance, checks changed boot ID and verifies exact effects/UNKNOWN preservation, leaves the VM running and evidence retained. No automatic retry, real accounts, host restart, dependency installation or production service launch. Local Bash syntax and same-boot process recovery passed; actual reboot execution pending. See docs/SAAS_CGROUP_RETIREMENT_PERSISTENCE_20261128.md.

## Combined guest acceptance result (2026-11-28)

Owner-approved exact c6de3d9290efeaa3cdaaa0134b297341afdb8084 script, ARC job 5a0e9f69-7609-45a4-bcb9-6bab9ee8f8a0 exited 0. Durable delegated retirement and both journal phases passed. Lima gracefully restarted only max2tg-acceptance, boot ID changed, ext4 verified. Independent read-only SQLite readback returned central [(1,ACKED),(2,UNKNOWN),(3,ACKED)] and worker [(1,SENT,1001),(2,UNKNOWN,None),(3,SENT,1003)]; integrity checks OK; three exclusive effect markers, zero duplicates. All three transient systemd units inactive/not-found and cgroups removed. Guest Running; management SSH 127.0.0.1:54477, loopback listener verified. Old listener closed during normal shutdown; no failure of acceptance. Evidence retained in guest private run directory. PR #78 and #79 all six/seven checks passed and merged, exact tested source tree confirmed at main 94a01284855dbafb6f05a19b4790e5f82198978a. This is process crash plus graceful VM restart, not physical power-loss acceptance. Real account and combined production lifecycle tests remain open.

## Dual-progress cohort guard

After explicit approval of the exact commit, run `bash acceptance/run-guest-cohort-guard.sh <full-reviewed-HEAD>` from a clean worktree. Three bounded private-network delegated guest services simulate independent broker/controller progress loss and verify complete owned group retirement. No restart. The hosted workflow also runs all three scenarios and a separate three-cycle mixed workload rehearsal (not long-duration acceptance). See docs/SAAS_COHORT_GUARD_20261128.md.

## Actual progress and hour rehearsal

The updated cohort guard guest harness uses actual PinnedBroker service-loop progress and refuses any upstream dial. Controller remains synthetic inside this private service. Explicitly approved invocation uses the same exact-commit run-guest-cohort-guard.sh. Separate manual-only Hour synthetic soak acceptance workflow runs four tenants for 40 cycles, 4160 messages; no real account access. It must be explicitly dispatched on a reviewed ref after CI, and results reviewed before claiming acceptance. See docs/SAAS_VERIFIED_PROGRESS_20261128.md.
