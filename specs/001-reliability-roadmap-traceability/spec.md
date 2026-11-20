# MAX2TG — reliability and field acceptance linkage

Draft, source main e9a5a7c80f3193630348ed5828ecd3004d49bfc3. No new launch decision, product scope expansion, live message test, account changes or permissions.

- MAX-SDD-R01: Preserve every existing roadmap task, nested marker, partial status, field acceptance condition and source anchor. Native IDs P0/P1/P2 etc. retain existing meaning. Do not infer unique or completed tasks from marker counts.
- MAX-SDD-R02: Reconcile local incident repair history and remote main without overwriting either. Local c7b9c479 and remote e9a5a7c80f3193630348ed5828ecd3004d49bfc3 differ; ancestry and actual runtime state require separate read-only comparison.
- MAX-SDD-R03: Validate bidirectional delivery and restart/reconnect reliability against original roadmap and field acceptance scope, durable outbox, no silent duplication/loss, ambiguous UNKNOWN handling. Do not inject destructive tests or synthetic messages on live personal accounts.
- MAX-SDD-R04: Keep physical P0.5b and multi-user/tenant isolation acceptance separate from existing offline tests. Separate authorized test accounts and forum are required by existing plan; absence is a blocker, not a reason to relax evidence.
- MAX-SDD-R05: SaaS onboarding, legal/security operations and price/economics remain separately gated hypotheses; do not convert checked personal UX tasks into multi-user release approval.

Source: [roadmap](../../docs/ROADMAP.md), [field acceptance](../../docs/P1_FIELD_ACCEPTANCE.md). Company baseline `4b734f04ead196cc76f1940bc50a9910be1d08ef` pins both source files. Original roadmap remains authoritative.
