# Operations hardening validation report — 2026-07-28

## Decision

The recommended hardening sequence is implemented, exercised against the local
runtime, and packaged. Dependency identity, benchmark collection, shadow review,
provider readiness, skill assembly, and release promotion now meet at one
fail-closed evidence boundary.

This is an implementation and local release-validation pass, not a production
promotion. `promotion-dossier.json` correctly reports `status=blocked` because
actual Task-versus-Company outcomes, independently judged shadow responses, and
a long-running Canary campaign do not yet meet their evidence floors. No
successful content-generation call was made during this validation; subscription
checks were status-only.

Validated dependency identity:

- `opc==0.1.0`;
- `nu-llm-routing-lib==0.2.2` at
  `0f90652bf9ad07f69592430e0fe1df9f9743e3f6`;
- `nu-resource-gen-lib==0.2.2`, stable API `1`, at
  `760e22acbb2c37e20542bcb4fdf782d58cd256e9`.

The LLM checkout was clean. The resource checkout already contained unrelated
user changes in its ComfyUI workflow/provider tests and implementation; they
were preserved and not modified.

## Completed sequence

| Priority | Delivered result |
|---|---|
| P0 — runtime bootstrap | `opc init --yes` now fills only missing top-level templates while preserving existing config bytes; the actual partial `.opc` workspace was repaired and NU routing became active |
| P1 — benchmark collection | Deterministic 36-pair/72-slot campaign plan, unique goal/run identities, artifact directories, 18/18 execution-order counterbalancing, progress accounting, and campaign-bound observations |
| P2 — shadow review | Privacy-safe review queue, exact transport-decision binding, served/challenger artifact SHA-256 requirements, latency/identity checks, and independent quality gate |
| P3 — Canary campaign | Freshness, maximum-gap, dense-bucket, drill-age gates, status-only 24-hour campaign plan, and four operator-coordinated failure contracts |
| P4 — subscription routing | Strict preferred-provider binding, preference-before-candidate-cap selection, no silent fallback, and redacted `subscription_quota`/auth/command status categories |
| P5 — role skills | Legacy flat core skills load alongside packaged `SKILL.md` entries; global capabilities are distributed to relevant roles rather than mounted everywhere |
| P6 — promotion boundary | One dossier combines exact dependency, outcome, shadow, and Canary report digests; every gate must pass |
| P7 — operations surface | Mission Control and Office UI expose SLO misses separately from incomplete long-window readiness evidence |

## Actual-use evidence

### Dependency and runtime configuration

The immutable release verifier passed for both source checkouts and installed
packages. The actual workspace contained organization config but lacked
`system_config.yaml`, `llm_config.yaml`, `agent_config.yaml`, and
`channel_config.yaml`. The repaired initializer installed only those four files,
preserved the existing allowlist and organization files, and loaded
`configs/byteplus.production.json` through the NU Router.

### Subscription providers

| Provider | Status-only result | Interpretation |
|---|---|---|
| Claude Sonnet | `available=true`, `model=sonnet` | Current subscription authentication and transport planning pass |
| Grok | `available=true`, `model=grok-4.5` | Current subscription authentication and transport planning pass after fixing preferred-provider candidate truncation |
| Codex | `available=false`, `error_category=quota` | Login is present, but the configured `gpt-5.6-sol` route is under an upstream subscription quota cooldown; no fallback was used |

The current `demo` store contains five Codex status attempts, all blocked by the
same current quota state. It therefore reports availability `0.0`, one time
bucket, no verified drills, and `production_ready=false`. This does not erase
the separate 2026-07-23 ledger evidence where Codex served successfully; it
means the provider is not ready now.

### Outcome, shadow, Canary, and skills

| Gate | Observed result | Decision |
|---|---|---|
| Outcome campaign | 12 cases, 36 pairs, 72 slots, Task-first 18 / Company-first 18; plan digest `f3efaad269e1a17f2abf726e0215103e0f79e3fbe4564c58c027a9841d61cbb4` | Collection contract passes; 0 observed/trusted slots blocks promotion |
| Shadow transport | One Codex-served / Claude Sonnet-challenger dialogue decision; both transports succeeded; snapshot `ebfac81f8ad63f7f6f791bb1eb0e3678167e831d32698843dd2c56944eacdde6` | Review queue is ready, but `1 < 20` decisions and 0 trusted quality judgments block promotion |
| Canary campaign | Status-only 24-hour plan, 289 expected samples, four explicit drills, no automatic failure injection | Plan passes; current span/buckets/SLO/drill evidence blocks promotion |
| Role skill assembly | 15 installed skills; deployment/coding assigned to `platform-engineer`, writing to `technical-writer`; zero organization capability gaps; no mutation | Actual legacy core skills are now usable and recommendations remain read-only |
| Unified dossier | Dependency gate passed; outcome, shadow, and Canary gates failed with 15 explicit blockers | `promotion_ready=false`, `product_claim_ready=false` |

The deterministic golden workflow completed 20/20 goal → route contract →
measured usage → scorecard → settlement → Canary → outbox → learning iterations.
All invariants passed, all 20 outbox messages were delivered, and no critical
alert or dead letter occurred. Mission Control correctly retained a medium
readiness-evidence alert instead of treating short-window health as production
readiness.

## Release gate

| Scope | Result |
|---|---:|
| OpenOPC Python | `2104 passed, 17 skipped, 36 subtests passed` |
| Focused subscription/capability/Canary regression | `47 passed` |
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
| Wheel and source distribution build | Passed |
| Isolated wheel package-data/import smoke | Passed |
| `git diff --check` | Passed |

The final wheel contains the benchmark suite, all new operations modules,
packaged config templates, and the rebuilt Mission Control bundle:

- `dist/opc-0.1.0-py3-none-any.whl` — SHA-256
  `f3b7986c1330d677fb904ec8e8299504d9a105516a3cf42cf88d5ffae46fa4c0`;
- `dist/opc-0.1.0.tar.gz` — source distribution build passed; no
  self-referential source-archive hash is embedded here.

Vite reports the existing Phaser lazy chunk at `1,207.94 kB` and the main
application bundle at `432.59 kB` (`126.64 kB` gzip). The known chunk-size
warning does not invalidate the build.

## Evidence artifacts

All runtime evidence is intentionally ignored by Git under
`outputs/operations_hardening_20260728/`:

- `gates/dependency-release.json`;
- `gates/outcome-campaign-plan.json`;
- `gates/outcome-campaign-progress.json`;
- `gates/outcome-promotion.json`;
- `gates/shadow-review-queue.json`;
- `gates/shadow-promotion.json`;
- `gates/subscription-{codex,claude,grok}-status.json`;
- `gates/subscription-readiness.json`;
- `gates/canary-campaign-plan.json`;
- `gates/canary-readiness.json`;
- `gates/skill-assembly.json`;
- `gates/promotion-dossier.json`;
- `golden/operations-golden-report.json`.

The source shadow ledger remains at
`outputs/background_shadow_subscription_20260723/events.sqlite3`.

## Required evidence before promotion

1. Execute all 72 planned Task/Company slots and retain at least ten trusted
   pairs per workload with actual artifacts and passing scorecards.
2. Collect at least 20 durable shadow decisions and 30 independently or human
   judged, transport-bound quality observations per workload.
3. Wait for the Codex subscription quota to recover, then complete at least
   24 hours of fresh status samples across four dense buckets without excessive
   gaps.
4. Coordinate and verify credential-expiry, transport-timeout,
   quota-exhaustion, and model-drift drills with durable independent evidence.

Until all four evidence groups pass, production promotion and broader product
claims remain disabled.
