# P2.2b — opt-in aggregate stage profiling

Base main acefc3d (merged PR #37, all six CI checks passed). Canonical roadmap acceptance unchanged. Explicit trusted composition parameter only, default disabled. No per-message traces, dynamic labels, secrets, account identifiers, payloads, URLs, external telemetry or persistent profile storage. Bounded allowlist of stage names and cumulative count/seconds per process, monotonic duration measurements. Fixed group labels for bot initialization, identity, verification, polling, send and topic; separate budget-lock waiting, pacing sleep, API wrapper invocation time and locally deferred count. Actual method invocation count is not necessarily physical HTTP request count (e.g. cached operations and initialize). No policy/rate/concurrency/retry changes. Ingress IPC includes worker handler and MAX response; worker ingress includes handler and ledger. Gateway measures durable polling ingestion commit, fair ingress/egress rounds, forum refresh, lifecycle/drain-lock acquisition and lifecycle work. Worker measures route refresh, ingress lock wait, inbound handler, egress reconciliation and receipt repair. Pending age uses persisted wall timestamps (last RECEIVED timestamp on ingress, created_at on egress), sampled at dispatch attempt, not unique end-to-end message latency; negative/nonfinite values ignored. All other durations are monotonic. Overlapping/nested/concurrent intervals are not additive wall time. Completed spans may straddle measurement boundaries; in-flight spans excluded until completion.

`python3 scripts/test-placed-runtime.py --image <reviewed-image> --native-volumes --load 20 --tenants 16 --profile` uses same actual synthetic encrypted client composition, resource thresholds and delivery acceptance as prior cohort. Fixture-only USR1 signal handler prints cumulative JSON snapshots; collect before burst and after central/worker receipts confirmed, subtract counts/sums, aggregate worker data without IDs. Snapshot times close but not simultaneous across processes; completed spans only, baseline may include in-flight work finishing during load, idle work can continue briefly after drain. No profile signal handler in production modules. Exact send invocation count and receipt verification required. Four-tenant profiled burst added to existing CI. Two deterministic tests cover fixed cardinality, invalid/negative durations, counter regression, aggregation, API pacing and pre-call denial without invocation. 928 full tests, 45 existing PTB warnings, targeted Ruff, runtime preflight, structural Spec Kit gate pass. Oversized ARC command rejected before execution and split. No personal service edits or deployment.

## Observed 16-tenant synthetic profile on Mac mini

Actual encrypted native-volume runtime, 640 new confirmed messages, 48.521 s drain, 13.190 messages/s, p95 TG→MAX 22.614 s and MAX→TG 43.160 s. Exact effects and receipts, zero duplicates/unconfirmed, 23 resource samples, resource bounds respected, cleanup PASS, ARC job exit 0. Candidate image max2tg:profile-candidate is an app source-layer update atop prior verified candidate; no claim of fresh full Linux build until CI. Comparable prior fair-scheduler run: 48.863 s, but this is not a controlled profiler-overhead measurement (receipt index changed too).

| Stage | Completed samples | Aggregate seconds | Mean ms |
|---|---:|---:|---:|
| API send lock acquisition | 320 | 20.325015 | 63.516 |
| API send pacing | 320 | 16.351883 | 51.100 |
| API send call | 320 | 0.958849 | 2.996 |
| Egress total send attempt | 320 | 37.679595 | 117.749 |
| API poll call | 425 | 1.358173 | 3.196 |
| API verify call | 128 | 0.390742 | 3.053 |
| API identity call | 32 | 0.069847 | 2.183 |
| Gateway durable poll ingest commit | 425 | 0.037307 | 0.088 |
| Gateway ingress round | 24 | 4.328870 | 180.370 |
| Gateway egress round | 24 | 39.140021 | 1630.834 |
| Gateway forum refresh | 41 | 11.834901 | 288.656 |
| Gateway lifecycle lock wait | 41 | 28.645819 | 698.679 |
| Ingress IPC | 320 | 14.792138 | 46.225 |
| Ingress pending age | 320 | 3639.641341 | 11373.879 |
| Egress pending age | 320 | 7138.485318 | 22307.767 |
| Workers: route | 1361 | 47.908170 | 35.201 |
| Workers: ingress handler | 320 | 6.879481 | 21.498 |
| Workers: ingress lock | 320 | 4.738505 | 14.808 |
| Workers: egress reconciliation | 721 | 49.781193 | 69.045 |
| Workers: receipt repair | 721 | 1.400526 | 1.942 |

Send lock + pacing account for 36.677 s (~97.3% of the measured send attempt duration); 320 send, 425 poll and 160 forum/identity method invocations share the 20 starts/s budget. This is a synthetic fixture artifact as well as a real scheduling cost: fixture returns empty getUpdates immediately even when requested timeout=20; real Telegram long polling generally waits for new updates/timeout. Therefore the next experiment must model bounded cancellable long polling with event wake-up and compare immediate vs long-poll fixtures at the same cohort and unchanged production rate/security policy. Do not infer real API throughput from this test, raise the rate, weaken verification, or change queue policy solely on this evidence. If long polling leaves a bottleneck, investigate fair budget allocation and worker route/reconciliation costs with measured evidence. P2.2b/P2.2c remain open; no production activation or real accounts.
