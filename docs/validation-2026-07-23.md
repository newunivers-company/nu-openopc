# Operations validation report — 2026-07-23

## Decision

The recommended operations hardening sequence is complete and passes the local release gate. OpenOPC now governs native and streaming LLM calls, fails closed on incompatible data, recovers durable work, limits subscription calls atomically, records provider evidence, gates resource generation, and exposes a project-scoped Mission Control view.

The validated release set is:

- `opc==0.1.0`;
- `nu-llm-routing-lib==0.2.1`;
- `nu-resource-gen-lib==0.2.1`, using stable API version `1`.

Both NU wheels were installed into a clean Python 3.12 virtual environment before their public facades, packaged router configuration/schema, and packaged resource data were checked. Development-checkout imports were not used as release evidence.

## Actual-use evidence

| Use case | Route | Result | Operational interpretation |
|---|---|---|---|
| Governed subscription completion | Codex subscription, `gpt-5.6-sol` | Returned the exact canary text `OPENOPC_SUBSCRIPTION_OK`; the planned and actual route matched; status canary succeeded | Subscription routing works. Tokens and cost remain `null` because the CLI does not report them; a rolling call quota therefore remains mandatory. |
| Bounded shadow comparison | Served Codex plus non-serving Claude Sonnet challenger | Served response was `OPENOPC_SHADOW_OK`; Codex completed in 3416.13 ms and the Claude challenger completed in 7055.53 ms; one content-free decision and three lifecycle events were recorded | The evidence path works, but the current synchronous shadow API adds challenger latency. It remains an operator experiment and is not enabled on the user-serving path. |
| Ampere-local artifact grounding | RF-DETR Nano on RTX 3060 / `sm_86` | Score `92`; `dog` confidence `0.8429`, `person` confidence `0.7708`; both expected classes grounded; cost `0`; benchmark latency `7630.46` ms | This is valid evidence for object presence and prop continuity. It is not evidence for aesthetics, identity fidelity, composition, or narrative quality. |
| Governed resource bridge | OpenOPC capability contract → `nu-resource-gen-lib` | Readiness allowed the live local route, planned and actual `local_vlm/RFDETR` matched, quality gate passed with grounded evidence, and usage was measured as local cost `0` | The stable `ResourceGenerator.evaluate_prompt()` facade and the OpenOPC quality contract work end to end without paid generation. |
| Durable operating loop | 20 golden iterations | 20/20 goals, route contracts, measured usage events, scorecards, canaries, and outbox deliveries completed; all invariants passed; no critical alerts | Goal settlement, delivery idempotency, SLO evidence, and shadow-only learning candidates are repeatable. |
| Mission Control | Live Office UI server, desktop and 390 px viewport | Healthy project snapshot rendered with provider SLO/quota sections, no console errors, and no horizontal overflow | The UI is usable on desktop/mobile and remains deterministic, project-scoped, and model-free. |

Actual RF-DETR benchmark evidence is intentionally kept outside version control under `../nu-resource-gen-lib/outputs/openopc_ampere_rfdetr_20260723/`. The bounded shadow experiment is under `../nu-llm-routing-lib/outputs/openopc_shadow_20260723/`; neither directory contains product source changes.

## Recovery and safety coverage

- Schema version 3 upgrades the original schema additively and rejects an unknown future schema.
- Backup create, inspect, integrity verification, restore, tamper rejection, and overwrite protection are covered.
- Spawned multi-process compare-and-set tests prove lease/fencing behavior across process boundaries.
- A crash after an external side effect is recovered through an idempotent delivery receipt instead of repeating the side effect.
- Subscription call reservations use `BEGIN IMMEDIATE`, cannot oversubscribe under concurrency, count failed calls conservatively, and expire on the rolling window.
- Resource approvals bind approval, operator, key, project, candidate, prompt hash, expiry, and cost ceiling. A verification keyring supports controlled key rotation.
- Request-supplied Python and script paths cannot select a resource executor. Mock VLM output remains schema-test evidence only.

## Release gate

| Scope | Result |
|---|---:|
| OpenOPC Python | `2049 passed, 17 skipped, 36 subtests` |
| NU LLM router Python | `3174 passed` |
| NU resource generator Python | `4684 passed` |
| Frontend unit runner | `25 passed` |
| Frontend Vitest | `7 passed` |
| Frontend browser assertions | `20 passed` |
| Ruff selected correctness checks | Passed in all three Python repositories |
| TypeScript typecheck | Passed |
| Frontend production build | Passed |
| Lockfile checks | Passed for OpenOPC and the resource library |
| Isolated NU wheel install/contract check | Passed |
| OpenOPC wheel and source distribution build | Passed |
| Clean three-wheel install and `opc`/`opc ops` entry points | Passed |
| Operations golden loop | 20/20 iterations and all invariants passed |
| Scorecard regression gate | Passed |

Frontend route splitting reduced the initial application JavaScript from approximately 1.30 MB to 431.59 kB (about 66.7%). Phaser remains a deliberate isolated large chunk rather than initial-page payload.

## Remaining promotion gates

These items are not blockers for the local release set, but they are required before broader production automation:

1. Move shadow challengers to a background worker with a durable experiment budget, cancellation policy, and shutdown flush before enabling them on user traffic.
2. Expand RF-DETR validation from one grounding scenario to a versioned positive/negative and multi-frame corpus. Keep human or stronger VLM review for identity, aesthetics, and story quality.
3. Publish the two private `0.2.1` packages or repository tags, then pin CI checkouts to immutable tags or commit SHAs. Local commits alone do not create a reproducible remote dependency release.
4. Accumulate enough real canary samples to evaluate availability and p95 trends; a single successful subscription canary proves connectivity, not production SLO attainment.
