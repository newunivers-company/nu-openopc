# Operations validation report — 2026-07-23

## Decision

The recommended operations hardening sequence is complete and passes the local release gate. OpenOPC now governs native and streaming LLM calls, fails closed on incompatible data, recovers durable work, limits subscription calls atomically, records provider evidence, gates resource generation, and exposes a project-scoped Mission Control view.

The validated release set is:

- `opc==0.1.0`;
- `nu-llm-routing-lib==0.2.2`;
- `nu-resource-gen-lib==0.2.2`, using stable API version `1`.

Both NU wheels were installed into a clean Python 3.12 virtual environment before their public facades, packaged router configuration/schema, and packaged resource data were checked. Development-checkout imports were not used as release evidence.

## Actual-use evidence

| Use case | Route | Result | Operational interpretation |
|---|---|---|---|
| Governed subscription completion | Codex subscription, `gpt-5.6-sol` | Returned the exact canary text `OPENOPC_SUBSCRIPTION_OK`; the planned and actual route matched; status canary succeeded | Subscription routing works. Tokens and cost remain `null` because the CLI does not report them; a rolling call quota therefore remains mandatory. |
| Background shadow comparison | Served Codex plus non-serving Claude Sonnet challenger, hard budget `1` | Served response was exactly `BACKGROUND_SHADOW_OK` in 3741.06 ms and returned while the Claude call was still in flight; Claude completed in 4030.50 ms and shutdown drained in 4037.61 ms | The challenger no longer extends serving latency. One durable reservation was completed under the same content-free decision ID; this proves the path, not model promotion. |
| Ampere-local artifact grounding | RF-DETR Nano on RTX 3060 / `sm_86` | Score `92`; `dog` confidence `0.8429`, `person` confidence `0.7708`; both expected classes grounded; cost `0`; benchmark latency `7630.46` ms | This is valid evidence for object presence and prop continuity. It is not evidence for aesthetics, identity fidelity, composition, or narrative quality. |
| Versioned RF-DETR regression | Packaged v1 positive/negative/two-frame corpus, RF-DETR Nano on RTX 3060 | 7/7 actual cases completed; four positive controls passed, three negative controls regenerated; decision accuracy, negative rejection, expected-class recall, and sequence continuity were all `1.0`; p95 `7477.87` ms; cost `0` | Empty negative detections now remain completed inference evidence instead of becoming transport failures. Dry-runs, missing cases, and wrong control decisions fail closed. |
| Governed resource bridge | OpenOPC capability contract → `nu-resource-gen-lib` | Readiness allowed the live local route, planned and actual `local_vlm/RFDETR` matched, quality gate passed with grounded evidence, and usage was measured as local cost `0` | The stable `ResourceGenerator.evaluate_prompt()` facade and the OpenOPC quality contract work end to end without paid generation. |
| Status-canary attainment | Six no-generation Codex status probes, expected model `gpt-5.6-sol` | 6/6 available, zero model drift, p50 `2.284` ms, p95 `568.804` ms; the three-sample current window improved over the previous window; configured sample floor `3` met | The local promotion gate is green. Six connectivity samples are still too small for a production reliability claim. |
| Durable operating loop | 20 golden iterations | 20/20 goals, route contracts, measured usage events, scorecards, canaries, and outbox deliveries completed; all invariants passed; no critical alerts | Goal settlement, delivery idempotency, SLO evidence, and shadow-only learning candidates are repeatable. |
| Mission Control | Live Office UI server, desktop and 390 px viewport | Healthy project snapshot rendered with provider SLO/quota sections, no console errors, and no horizontal overflow | The UI is usable on desktop/mobile and remains deterministic, project-scoped, and model-free. |

Actual RF-DETR regression evidence is intentionally kept outside version control under `../nu-resource-gen-lib/outputs/rfdetr_regression_20260723/`. The subscription shadow ledger is under `outputs/background_shadow_subscription_20260723/` and the persistent canary store is under `outputs/canary_status_20260723/`; these ignored evidence directories contain no product source changes.

## Recovery and safety coverage

- Schema version 3 upgrades the original schema additively and rejects an unknown future schema.
- Backup create, inspect, integrity verification, restore, tamper rejection, and overwrite protection are covered.
- Spawned multi-process compare-and-set tests prove lease/fencing behavior across process boundaries.
- A crash after an external side effect is recovered through an idempotent delivery receipt instead of repeating the side effect.
- Subscription call reservations use `BEGIN IMMEDIATE`, cannot oversubscribe under concurrency, count failed calls conservatively, and expire on the rolling window.
- Shadow call reservations also use `BEGIN IMMEDIATE`, share an immutable experiment ceiling across processes and restarts, and recover stale reservations as spent `abandoned` calls. The served terminal event is flushed before challenger enqueue.
- Background shadow has bounded workers and queue capacity, rejects incompatible signed live-pilot hooks, supports pending cancellation, and performs a timeout-bounded shutdown drain.
- Resource approvals bind approval, operator, key, project, candidate, prompt hash, expiry, and cost ceiling. A verification keyring supports controlled key rotation.
- Request-supplied Python and script paths cannot select a resource executor. Mock VLM output remains schema-test evidence only.

## Release gate

| Scope | Result |
|---|---:|
| OpenOPC Python | `2055 passed, 17 skipped, 36 subtests` |
| NU LLM router Python | `3189 passed` |
| NU resource generator Python | `4695 passed` |
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

1. Publish the two private `0.2.2` packages or repository tags, then pin CI checkouts to immutable tags or commit SHAs. Local commits alone do not create a reproducible remote dependency release.
2. Accumulate a representative, reviewable shadow set across dialogue/coding workloads before any route promotion. The single paid Claude challenger proves bounded asynchronous operation only.
3. Extend status canaries across normal operating periods and failure windows. Six samples clear the configured local floor but do not establish a production availability or tail-latency SLO.
4. Expand the RF-DETR corpus with internally licensed production-domain assets. Keep human or stronger VLM review for identity, aesthetics, composition, and story quality because object grounding cannot prove those axes.
