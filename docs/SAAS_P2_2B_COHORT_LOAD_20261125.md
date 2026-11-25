# P2.2b — measured cohort scaling (4/8/16 workers)

Base b1e3711 (PR #34), original roadmap and acceptance unchanged. Provider-neutral. Same actual encrypted client composition, synthetic TLS endpoints and native-volume placement as baseline. No actual accounts or host configuration changes. Generalized disposable provisioner, broker, test runner and metric validation to cohorts of 2/4/8/16 tenants. Cohorts above 2 allowed only in native-volume bounded load mode. Warm-up one message in each direction per tenant, then simultaneous burst of 20 per tenant per direction; exact recipients/effects and durable central + worker receipts required. No replay of ambiguous messages. Per-container 256 MiB / 0.5 CPU / 64 pids, aggregate sampled load stop at 1024 MiB or 250% CPU; 60-second drain gate (sampling granularity can overrun by a sample). Stop limits apply during measurement, not all startup/cleanup phases. No claim of instantaneous CPU/RAM peaks. Two-tenant regression maintained; four-tenant CI test added. 921 tests, 45 existing warnings, targeted Ruff, preflight and structural Spec Kit gate pass.

Mac mini, Docker VM ~7.748 GiB RAM, other services remain running. Local pinned source-layer candidate max2tg:poll-recovery-candidate. Latency is time from synthetic burst generation until destination effect; drain includes host inspection overhead. Single runs per new cohort, no sustained or real API capacity claim. Baseline 2-tenant results from prior doc; use run 2 for comparable timing. Peak role memory and CPU are sampled individually, not simultaneous totals.

| Tenants | 2 (baseline run 2) | 4 | 8 |
|---|---:|---:|---:|
| Confirmed new messages | 80 | 160 | 320 |
| Confirmed drain s | 7.010 | 12.619 | 25.253 |
| Confirmed messages/s | 11.412 | 12.679 | 12.672 |
| TG→MAX p95 s | 1.589 | 1.807 | 9.560 |
| MAX→TG p95 s | 5.387 | 10.578 | 23.399 |
| Max sampled worker memory MiB | 45.83 | 45.81 | 45.90 |
| Gateway / broker sampled peak MiB | 49.25/45.28 | 49.36/45.89 | 49.35/46.32 |
| Central pending ingress/egress sampled peak | 0/37 | 3/79 | 118/160 |
| Duplicate or unconfirmed | 0 | 0 | 0 |

Both new 4/8 runs passed, label-scoped resources cleaned. 16-tenant run result recorded below. The throughput plateau and growing queues motivate profiling and improving fair bounded gateway scheduling. Source inspection confirms serial egress drain and a global send lock; exact contributions of IPC, receipt scanning, API pacing, and storage require profiling. No claim of sole causal bottleneck. Do not buy hosting or claim P2.2b/P2.2c accepted. Next: profile central send/receive and worker receipt reconciliation, evaluate bounded fair scheduling under same benchmark; retain all safety gates, never retry UNKNOWN.

16-tenant result: all 640 new messages confirmed, 48.840 s drain, 13.104 messages/s. TG→MAX p50/p95/max 9.780/22.692/35.887 s; MAX→TG 25.267/44.503/46.247 s. Maximum observed worker memory 47.52 MiB; gateway/broker 49.68/49.08 MiB, sampled peak CPU 12.52%/3.97%, sampled pending ingress/egress 289/320. 23 samples, resource stop thresholds not exceeded, zero duplicate/unconfirmed messages, exact cleanup PASS, exit 0. Total new confirmed messages across 4/8/16 runs: 1120. This is a successful bounded burst test, not sustained load or latency acceptance. 16-tenant test ran as one ARC job and was awaited through cleanup.
