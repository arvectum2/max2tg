# P2.2b — measured synthetic two-tenant baseline

Base 90f79ea (PR #33); original roadmap and acceptance unchanged. Hosting provider not selected: Timeweb Cloud was a preliminary option discussed 29 September, not an architectural dependency. Select hosting later using measured resource requirements, geography/data protection, resilience and total operating cost.

Reproduce: `python3 scripts/test-placed-runtime.py --image <reviewed-image> --native-volumes --load 20`. Bounded 1–25 messages per tenant per direction; two isolated workers, actual MaxClient and PTB Bot over synthetic TLS, real runtime loops and encrypted scoped credentials, network none, pinned image, existing 256 MiB / 0.5 CPU / 64 pid limits each. After 2+2 warmup receipts, synthetic broker emits a burst of 20 distinct MAX events and 20 distinct Telegram updates per worker (80 additional total), verifies exact recipient/message effects and all central ACKED/SENT plus worker SENT states. No real API calls, media, account or credential data. All disposable label-scoped containers and 14 native volumes removed. Benchmark mode uses same placement harness, not a different transport. Added CI run and deterministic metric validation. No new production runtime implementation in this PR.

Mac mini M4 Pro 24 GiB host, Docker VM reports 12 CPUs and 7.748 GiB RAM. Other host services remained running. Candidate image max2tg:poll-recovery-candidate (source-layer update atop previously verified pinned crypto image). Two separate runs, three in-load Docker stats samples each; first run elapsed timer began after trigger command, second improved timer begins before trigger command. Timing is deliberately coarse and includes host inspection overhead and waiting for worker receipts. Broker timestamps measure burst-to-synthetic-destination-effect, not a real network delivery SLA. CPU is Docker sampled percent of one CPU, memory Docker reported usage (not whole-host RAM), per-role maxima are not simultaneous. Central queue values are sampled, not true maxima; zero observed ingress pending does not prove it was never queued. Do not infer maximum capacity from two tenants.

| Metric | Run 1 | Run 2 |
|---|---:|---:|
| New confirmed messages (40 each direction) | 80 | 80 |
| Confirmed drain seconds | 6.219 | 7.010 |
| Confirmed messages/s (both directions) | 12.864 | 11.412 |
| TG→MAX burst-to-effect p50 / p95 / max s | 0.915 / 1.123 / 1.147 | 1.382 / 1.589 / 1.611 |
| MAX→TG burst-to-effect p50 / p95 / max s | 3.231 / 5.060 / 5.270 | 3.611 / 5.387 / 5.613 |
| Gateway baseline / sampled peak MiB | 49.05 / 49.05 | 47.89 / 49.25 |
| Broker baseline / sampled peak MiB | 46.12 / 46.12 | 45.28 / 45.28 |
| Worker 1 baseline / sampled peak MiB | 44.38 / 45.04 | 44.18 / 44.60 |
| Worker 2 baseline / sampled peak MiB | 45.05 / 45.05 | 45.34 / 45.83 |
| Sampled peak CPU % broker/gateway/worker1/worker2 | 2.44/5.38/1.59/2.37 | 2.47/6.83/3.54/4.51 |
| Sampled peak central pending ingress/egress | 0/32 | 0/37 |
| Duplicates / unconfirmed | 0/0 | 0/0 |

All 920 Python tests, targeted Ruff, runtime preflight and Spec Kit structural checks pass; 45 existing PTB deprecation warnings. Two actual-container synthetic load runs pass. PR #33 fresh-image CI was still in progress at last check, so retain dependency if required. No maximum throughput estimate or server order; 20 starts/s gateway budget remains provisional and shared with verification. No host configuration or personal service changes.

Next: generalize bounded synthetic cohort to 4/8/16 workers in staged runs with resource stop limits, sustained workloads and repeat measurements; then investigate receipt scan and fairness bottlenecks using measured evidence. Separate real-account and protected-host acceptance gates remain open. Do not close P2.2b/P2.2c or buy hosting based on this baseline.
