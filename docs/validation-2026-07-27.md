# Operations hardening validation report — 2026-07-27

## Decision

The recommended hardening sequence is implemented and the local release gate
passes. OpenOPC now has immutable NU dependency identity, a versioned
Task-versus-Company outcome benchmark, quality-aware shadow promotion,
long-horizon canary readiness, immutable Self-Grown runtime activation,
digest-confirmed operator actions, and deterministic role-skill assembly across
the CLI, agent tools, secretary context, channels, and Office UI.

This result does **not** claim production promotion. The new gates correctly
remain blocked until representative actual outcome pairs, independently judged
shadow quality, a 24-hour canary window, and controlled failure drills exist.
No paid provider generation was triggered during this hardening run.

The validated release identity is:

- `opc==0.1.0`;
- `nu-llm-routing-lib==0.2.2` at
  `0f90652bf9ad07f69592430e0fe1df9f9743e3f6`;
- `nu-resource-gen-lib==0.2.2`, stable API `1`, at
  `760e22acbb2c37e20542bcb4fdf782d58cd256e9`.

Source checkouts, installed versions, stable facades, and the checked-in
manifest all matched. CI now resolves these full commit SHAs before checkout
instead of treating a mutable branch or human-readable version as release
identity.

## Completed sequence

| Priority | Delivered contract |
|---|---|
| P0 — reproducible dependencies | `config/nu_release_manifest.json`, source/installed verification, exact-SHA CI checkout, and stable API compatibility validation |
| P1 — outcome evidence | Versioned 12-case software/content/research suite, trusted paired observations, confidence/non-regression gates, measured cost/time/intervention reporting, and simulation exclusion |
| P2 — shadow promotion | Independent/human quality evidence with fingerprints and judge revisions; quality and transport gates remain separate |
| P3 — canary readiness | 24-hour/time-bucket readiness, four controlled failure scenarios, independent evidence checks, and drill exclusion from availability SLO arithmetic |
| P4 — Self-Grown activation | Content-addressed promoted-asset snapshot pinned into each run, organization/role/employee scoping, exact runtime mounting, and future-run-only rollback semantics |
| P5 — Mission Control actions | Allowlisted plan → review → exact SHA-256 confirmation → single-use durable receipt flow, project scope, expiry, operator identity, and failure states |
| P6 — Self-Built execution | Deterministic installed-skill recommendations, capability gaps, explicit role `skill_refs`, Native/external runtime mounting, secretary preview, and fixed channel commands |
| P7 — release surfaces | Schema version 4 migration/backup coverage, Office UI action center, documentation, package data, CI benchmark validation, and generated frontend assets |

Skill recommendation evidence was tightened after an actual catalog run exposed
an incidental-token false positive: a capability word appearing only inside a
long instruction body can no longer justify a recommendation. Discovery now
uses the skill name, description, and explicit metadata. The same run loaded
nine installed skills, made no unsupported mutations or recommendations, and
reported `deployment`, `coding`, and `writing` as honest catalog gaps.

## Actual-use evidence

| Use case | Observed result | Interpretation |
|---|---|---|
| Durable operating loop | 20/20 goals, route contracts, measured usage events, scorecards, canaries, and outbox deliveries completed; all invariants passed; no critical alert or dead letter | The deterministic local operating loop and shadow-only learning proposal are repeatable |
| Existing subscription shadow ledger | One served Codex decision and one Claude Sonnet shadow attempt; both transports succeeded, coverage `1.0`, no transport disagreement | The asynchronous transport path works, but `1 < 20` decisions and zero trusted quality judgments block promotion |
| Existing status-canary store | Codex 6/6 success, availability `1.0`, p95 `568.804 ms`, no model drift | Short SLO attainment passes, but only `0.016 s`, one six-hour bucket, and zero controlled drills make production readiness `false` |
| Outcome benchmark gate | Suite v1 loaded 12 cases across three workloads; digest `b3a8ecd772af04868e78fa71410f28bfd0841a8b5d92a0ad7bb2e7c6a0f8862a` | Contract validation passes; zero actual trusted pairs correctly block quality and product claims |
| Installed role-skill assembly | Nine local skills loaded; recommendation was read-only; unsupported deployment/coding/writing matches were rejected and surfaced as gaps | Role assembly fails honestly instead of inventing capability coverage |
| Packaged release smoke | Wheel contained all new runtime modules, the benchmark JSON, and built Office UI assets; an isolated wheel-only environment loaded all 12 cases | Package data and imports do not depend on the development checkout |

The golden report is
`outputs/operations_hardening_20260727/golden/operations-golden-report.json`.
The three machine-readable promotion reports are:

- `outputs/operations_hardening_20260727/gates/outcome-benchmark-blocked.json`;
- `outputs/operations_hardening_20260727/gates/shadow-promotion-blocked.json`;
- `outputs/operations_hardening_20260727/gates/canary-readiness-blocked.json`.

These runtime outputs are intentionally ignored by Git. The pre-existing
subscription and canary evidence remains under
`outputs/background_shadow_subscription_20260723/` and
`outputs/canary_status_20260723/`.

## Release gate

| Scope | Result |
|---|---:|
| OpenOPC Python | `2089 passed, 17 skipped, 36 subtests passed` |
| Focused final canary/CLI tests | `17 passed` |
| Focused final role-skill/channel/runtime tests | `22 passed` |
| Frontend structural unit runner | `25 scripts passed` |
| Frontend Vitest | `7 passed` |
| Frontend browser E2E | Message scroll contract passed; execution panel `20/20` assertions passed |
| TypeScript typecheck | Passed |
| Frontend production build | Passed |
| Ruff selected correctness checks | Passed |
| Scorecard regression gate | Passed; maximum observed score drop `0.01 <= 0.05` |
| NU release manifest/source/install check | Passed |
| NU stable compatibility check | Passed |
| Versioned benchmark validation | Passed |
| Operations golden loop | `20/20`, all invariants passed |
| OpenOPC wheel and source distribution build | Passed |
| Isolated wheel-only benchmark smoke | Passed |
| `git diff --check` | Passed |

Built artifacts:

- `dist/opc-0.1.0-py3-none-any.whl` —
  SHA-256 `cea53441e8173def2447dd92bdd7b8a8a6fff82cc2f529914c44a4f09a405484`;
- `dist/opc-0.1.0.tar.gz` — source distribution build passed (its contents
  include this report, so the report does not embed a self-referential hash).

The main application bundle is `432.59 kB` (`126.64 kB` gzip). Phaser remains
an isolated `1,207.94 kB` lazy chunk and produces the known Vite size warning;
it does not invalidate the build.

## Promotion gates still blocked

1. Execute and review the complete 36-pair Task-versus-Company matrix
   (12 cases × 3 repetitions). The minimum statistical gate is ten trusted
   pairs in each workload; current trusted pairs are zero.
2. Collect at least 20 durable served decisions for transport evidence and at
   least 30 trusted, independently or human judged shadow observations per
   workload. Current values are one decision and zero quality observations.
3. Extend status canaries to at least 24 hours across four six-hour buckets and
   record independently evidenced credential-expiry, transport-timeout,
   quota-exhaustion, and model-drift drills. Current coverage is one bucket and
   no verified drills.
4. Add or install skills whose discovery metadata explicitly covers the
   deployment, coding, and writing gaps before assigning those capabilities to
   production roles.

Until these evidence thresholds pass, automated promotion and broader product
claims must remain disabled.
