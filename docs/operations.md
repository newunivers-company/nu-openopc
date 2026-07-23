# Outcome-Driven Operations

OpenOPC's operating kernel turns the Self-Built → Self-Run → Self-Grown vision into a measurable, recoverable loop. It is additive to the existing task and company runtimes: existing `Task` and `DelegationWorkItem` identities remain authoritative, while operations records state why a run exists, whether it succeeded, how it can recover, and what may safely be learned from it.

## Operating loop

1. A `GoalContract` fixes the objective, non-goals, deliverables, acceptance criteria, evidence requirements, budgets, deadline, and human gates. Updates require the next version and prior versions remain queryable.
2. A `RunManifest` pins the exact goal version, organization, source revision, model/provider/skill versions, and route decisions used for one attempt.
3. Every brokered call persists a `RouteExecutionContract` before execution, then verifies that the actual provider, model, or candidate is the planned primary or an approved fallback.
4. Every completed call persists a `ProviderUsageEvent`. Unknown subscription usage stays explicitly unmeasured with null token and cost fields; it is never converted to zero.
5. The durable kernel records append-only events and transactional outbox deliveries under leases and fencing tokens.
6. A `RunScorecard` deterministically evaluates quality, evidence, budget, reliability, and autonomy, and settles an eligible goal in the same database transaction.
7. Role and route outcomes feed staffing and shadow-policy evidence. Candidate memories, skills, and policies enter a governed learning lifecycle.
8. Mission Control aggregates risks, provider SLOs, unmeasured usage, and recommended actions for the secretary, CLI, and agent tools.

All records live in the existing project-scoped database at `.opc/projects/<project>/tasks.db`. Cross-project writes are rejected.

The executed release evidence and remaining promotion gates are recorded in [the 2026-07-23 validation report](validation-2026-07-23.md).

## Quick operating loop

Create a full contract as JSON:

```json
{
  "goal_id": "release-1",
  "project_id": "demo",
  "title": "Release the demo",
  "objective": "Ship a verified demo without a critical regression.",
  "non_goals": ["Production migration"],
  "deliverables": ["Demo package", "Test report"],
  "acceptance_criteria": [
    {
      "criterion_id": "tests",
      "description": "Required tests pass",
      "weight": 2,
      "minimum_score": 1.0,
      "required": true,
      "evidence_required": true
    }
  ],
  "budget": {
    "max_cost_usd": 2.0,
    "max_duration_seconds": 1800,
    "max_interventions": 1,
    "max_rework_cycles": 2,
    "max_failed_attempts": 0
  },
  "evidence_requirements": ["test_report"]
}
```

Then run the loop:

```bash
uv run opc ops goal create --contract goal.json --project demo
uv run opc ops run start release-1 --run-id release-1-attempt-1 \
  --complete-goal-on-pass --project demo

# Execute work through task or company mode, then close the manifest.
uv run opc ops run finish release-1-attempt-1 --status completed --project demo

# result.json contains criterion_scores, evidence, metrics, and optional role_outcomes.
uv run opc ops evaluate score release-1-attempt-1 --result result.json --project demo
uv run opc ops evaluate gate release-1-attempt-1 --baseline-label main --project demo
uv run opc ops mission brief --project demo
```

`ops evaluate gate` exits non-zero if the current scorecard is not accepted or any component drops more than `maximum_regression` from its baseline.

`--complete-goal-on-pass` declares that this run is allowed to close the goal. A passing scorecard creates a new immutable `completed` goal version only when the run is pinned to the latest goal version and no sibling run remains active. Without that explicit declaration, evaluation never closes a goal implicitly. Operators can also create an audited terminal version directly:

```bash
uv run opc ops goal close release-1 --status completed \
  --reason "release evidence accepted" --project demo
```

To revise a goal, submit the same `goal_id` with exactly the next `version`. In-place rewrites and skipped versions are rejected. Runs already created continue to use their pinned archived version.

## Scorecard semantics

The default total is:

| Component | Weight | Meaning |
| --- | ---: | --- |
| Quality | 0.45 | Weighted acceptance-criterion scores |
| Evidence | 0.20 | Required criterion and goal evidence coverage |
| Budget | 0.15 | Cost, time, token, intervention, rework, and failure ceilings |
| Reliability | 0.10 | Successful attempts and successful resumes |
| Autonomy | 0.10 | Human interventions relative to the declared ceiling |

Required criterion failures, missing required evidence, any budget violation, an unsuccessful terminal status, or a total below the configured threshold fails the gate. A non-terminal run is `review`, never accepted.

Evaluation input example:

```json
{
  "criterion_scores": {"tests": 1.0},
  "evidence": {
    "tests": ["artifact://pytest.xml"],
    "test_report": ["artifact://pytest.xml"]
  },
  "metrics": {
    "cost_usd": 0.4,
    "duration_seconds": 620,
    "tokens": 12000,
    "interventions": 0,
    "rework_cycles": 1,
    "failed_attempts": 0,
    "total_attempts": 1,
    "resume_attempts": 0,
    "resume_successes": 0
  },
  "role_outcomes": [
    {
      "role_id": "qa",
      "employee_id": "employee-qa",
      "quality_score": 0.95,
      "reliability_score": 1.0,
      "domain_scores": {"testing": 0.96},
      "evidence": ["artifact://qa-report"]
    }
  ],
  "baseline_label": "main"
}
```

## Durable events and recovery

Run start and finish transitions commit their manifest, lifecycle event, and optional outbox delivery in one SQLite `BEGIN IMMEDIATE` transaction. General operating events use the same event/outbox atomic boundary. The kernel provides:

- idempotency keys for repeated commands;
- per-aggregate optimistic versions to prevent lost updates;
- run leases with monotonically increasing fencing tokens;
- outbox delivery leases and fenced acknowledgements;
- exponential retry, a bounded attempt count, and `dead_letter` state;
- expired-claim recovery;
- pre-commit budget guards;
- inactivity-based deadlock detection and alert events.

Recovery commands:

```bash
uv run opc ops run recover <run-id> --owner operator-1 --project demo
uv run opc ops outbox list --status pending --status dead_letter --project demo
uv run opc ops outbox recover --project demo
uv run opc ops outbox replay <message-id> --reason "consumer repaired" --project demo
```

Create and verify a consistent SQLite snapshot while OpenOPC is running. Restore always targets an offline destination and refuses to overwrite by default:

```bash
uv run opc ops backup create backups/demo.db --project demo
uv run opc ops backup inspect backups/demo.db
uv run opc ops backup restore backups/demo.db --destination restored/tasks.db
```

Every backup has a sidecar manifest containing the schema version, SHA-256 digest, byte size, and operations-table row counts. Inspect and restore verify the digest and SQLite integrity before accepting the file. Stop OpenOPC before replacing a live project database; `--overwrite` is an explicit operator decision.

A stale worker cannot write with a fencing token after another owner takes over its expired lease. Dead-letter replay is intentionally not automatic: repair the consumer and replay with an explicit reason. Replay resets the delivery attempt budget and appends an `outbox.replayed` audit event in the same transaction.

When the OpenOPC engine is running, the configured outbox dispatcher claims pending deliveries in bounded batches, publishes them to the internal event bus, and acknowledges them with the claim's fencing token. Handler failures are retried with the durable backoff policy and move to `dead_letter` after the configured attempt limit. Shutdown stops the dispatcher before closing the store. Set `system.operations.durable.outbox_dispatcher_enabled: false` only when a separate process owns delivery.

For an independently deployed consumer, use a stable consumer ID. Delivery receipts survive worker restarts and prevent a successfully acknowledged message from being handled twice by that consumer:

```bash
uv run opc ops outbox work --consumer-id audit-v1 --batch-size 50 --project demo
```

The database boundary is at-least-once: a process may perform an external side effect and fail before acknowledging it. External handlers must therefore pass the durable `message_id` as their idempotency key. `outbox_delivery_receipts` provides consumer-side deduplication inside OpenOPC; it cannot make a third-party API atomic.

## Governed Self-Grown assets

Learned memory, skill, and policy assets follow this state machine:

```text
candidate → evaluated → shadow → canary → promoted
     └─────────────── reject on failed evidence ─────┘
promoted → rolled_back (restores the prior promoted version when available)
promoted → retired (superseded or expired)
```

Each evaluation requires a configured minimum score, minimum sample size, evidence, no violations, and no excessive regression from baseline. Offline acceptance also requires source-run provenance. Promotion requires passing offline, shadow, and canary evaluations. Creating a new version records the previous promoted asset; promotion retires it atomically, and rollback restores it.

```bash
uv run opc ops learning create --candidate learning-candidate.json --project demo
uv run opc ops learning evaluate <asset-id> --phase offline --score 0.9 \
  --sample-size 5 --evidence artifact://offline-report --project demo
uv run opc ops learning advance <asset-id> --phase shadow --project demo
# Record shadow evidence, advance to canary, record canary evidence, then:
uv run opc ops learning advance <asset-id> --phase promoted --project demo
uv run opc ops learning rollback <asset-id> --reason "quality incident" --project demo
```

Only promoted and non-expired assets are returned by `operations_active_learning`.

Measured execution outcomes can propose a routing asset, but never activate one automatically:

```bash
uv run opc ops learning propose-routing --min-samples 3 --project demo
```

Eligibility requires enough samples, at least 90% successful execution, passing scorecard evidence, at least 0.8 average quality, and at least 80% measured usage. The result remains a `candidate` with `application_mode=shadow_only`, `automatic_promotion=false`, and the normal offline → shadow → canary release gates. A subscription route whose usage is unknown cannot supply measured evidence by itself.

## Unified capability broker

The broker plans three capability kinds through one audit contract:

- `llm`: NU route diagnostics and current OpenOPC fallback transport;
- `external_agent`: adapters currently reported available by OpenOPC;
- `resource`: installed `nu-resource-gen-lib` candidate IDs and policy dry-runs.

It never invents a provider or candidate. Every route reports `plan_allowed`, `credential_ready`, `transport_ready`, and `live_allowed` separately, so an installed adapter cannot be mistaken for a callable transport. External-agent readiness uses a sanitized deep auth probe. Resource readiness comes from the selected provider rather than package installation alone.

For text-only turns, NU routing can execute authenticated Codex, Claude, and Grok subscription CLIs plus healthy native Ollama/Gemini providers, and falls through the ordered candidates on a provider error. These transports deliberately report no tool-call or native-streaming support through this bridge. Tool turns remain on OpenOPC's configured OpenAI-compatible transport while `llm.nu_routing.apply_to_tool_calls` is false. Grok router calls run in an isolated temporary working directory so repository instructions do not leak into a model-only request.

Tool-use LLM routes require `sandboxed_tools=true` and always carry a deterministic `gpu_free_vram_mib` value. Resource ranking is local/free-first. Unknown cost does not become zero; it is blocked under an explicit ceiling. Live NU generation still requires the existing live switch, candidate allowlist, `confirm_live=true`, OpenOPC approval, and provider policy checks.

The agent tool `operations_capability_plan` is permanently dry-run. CLI requests may describe live intent, but planning does not itself call a provider:

```bash
uv run opc ops capability plan --request capability-request.json --project demo
```

Execution through `OperationsService.execute_llm()` first freezes the selected route, ordered fallbacks, request snapshot, budget, and expiry in a `RouteExecutionContract`. The LLM bridge receives that contract and may only try those candidates in that order. A provider/model outside the contract, an expired contract, or a reported cost above its ceiling fails closed and is recorded as a contract violation or budget exceedance.

Subscription CLIs often do not expose trustworthy token or cost counters. Their successful responses are recorded with `measured=false`, `source=subscription_cli_unreported`, and null token/cost values. This is a valid execution result but not evidence that the call was free.

Because unknown usage cannot enforce a token or dollar ceiling, subscription routes also have an atomic rolling call-count guard. `system.operations.providers.subscription_call_limit` and `subscription_window_seconds` apply to the configured provider families. A reservation is committed immediately before provider I/O under `BEGIN IMMEDIATE`, so concurrent workers cannot oversubscribe the limit. Failed and timed-out calls remain counted conservatively. Mission Control warns at 80% and blocks new calls when the quota is exhausted.

### Bounded shadow experiments

The linked NU router can collect a served outcome and a non-serving challenger under one content-free decision record, but execution requires both an event database and an explicit whole-process call budget. OpenOPC does not automatically enable this path. Current synchronous `chat_with_shadow` collection finishes the challenger before the call returns, so it is suitable for a bounded operator experiment but not the latency-sensitive user-serving path. Production adoption requires background execution plus a durable budget and shutdown flush; until then, keep `max_total_calls` explicit and treat challenger latency as experiment overhead.

## Provider canaries and SLOs

Status canaries persist provider, model, readiness, latency, model drift, and normalized failure category without generating content:

```bash
uv run opc ops capability canary --request capability-request.json \
  --expected-model gpt-5.6-sol --project demo
uv run opc ops capability slo --availability-target 0.95 --project demo
```

SLO summaries report sample count, availability, p50/p95 latency, consecutive failures, and model-drift count by provider. Mission Control raises an alert only after the configured minimum sample count and evaluates availability and p95 latency as separate targets. When the engine is running, a status-only scheduler samples provider readiness at `status_canary_interval_seconds`; it never generates content. Live canaries exist at the service layer only and require both `allow_live=true`, explicit confirmation, and an injected executor; the CLI intentionally exposes only the no-generation canary.

## Approval-gated resource pipeline

`ops resource run` composes the shared NU planner, provider-specific prompt evaluation, candidate readiness, generation policy, and a grounded artifact-quality gate. A request defaults to dry-run and uses the Ampere-compatible `local_rfdetr_detection_nano` candidate for scoped object-presence QA:

```json
{
  "prompt": "A cinematic vertical portrait with consistent character identity",
  "candidate_id": "local_comfyui_krea2_t2i",
  "task_type": "image_generation",
  "allow_live": false,
  "require_free": true,
  "max_cost_usd": 0.0,
  "gpu_free_vram_mib": 12000,
  "hardware_profile": "Ampere sm_86",
  "qa_candidate_id": "local_rfdetr_detection_nano",
  "quality_scope": "object_presence",
  "expected_classes": ["person"]
}
```

```bash
uv run opc ops resource run --request resource-request.json --project demo
```

Live generation requires all of the following gates:

1. `system.nu_resource_gen.allow_live: true` and the exact candidate in `allowed_live_candidates`;
2. a passing shared prompt evaluation and transport/hardware readiness;
3. `allow_live=true` and `confirm_live=true` in the request;
4. a configured approval keyring (or the single-key compatibility secret), with every key containing at least 16 bytes;
5. a short-lived HMAC approval bound to an approval ID, operator ID, key ID, exact project, candidate, prompt hash, and cost ceiling;
6. the underlying provider's credential, billing, identity, publication, and policy checks.

Issue the approval only after reviewing the dry plan, then copy the returned token into the otherwise unchanged request:

```bash
OPENOPC_RESOURCE_APPROVAL_KEYS='{"2026-q3":"<operator-managed-secret>"}' \
OPENOPC_RESOURCE_APPROVAL_ACTIVE_KEY_ID='2026-q3' \
  uv run opc ops resource approve --request resource-request.json \
  --operator-id operator@example.com \
  --expires-in-seconds 300 --project demo
```

`OPENOPC_RESOURCE_APPROVAL_KEYS` is a JSON object of `key_id → secret`. Keep the previous key in the verification keyring during rotation while issuing new approvals with `OPENOPC_RESOURCE_APPROVAL_ACTIVE_KEY_ID`. `OPENOPC_RESOURCE_APPROVAL_SECRET` plus optional `OPENOPC_RESOURCE_APPROVAL_KEY_ID` remains a single-key compatibility path.

Approvals are single-use and their safe audit claims are persisted on consumption. Prompt, candidate, project, expiry, or cost changes invalidate them. Generated artifacts cannot pass automatically without a configured VLM executor returning a score, grounded findings, evidence, and proof that the declared quality scope and expected classes were satisfied; otherwise the result remains `review`. RF-DETR evidence proves object presence or prop continuity only, not aesthetics, identity fidelity, composition, or narrative quality. Simulation candidates, including `local_vlm_lab_mock`, are useful for schema tests only and are never production quality proof. GPU-backed live routes also require explicit hardware profile and free-VRAM evidence and fail closed on architecture or capacity mismatch. Python/script paths supplied by an untrusted request are ignored; only allowlisted candidate runtime metadata may select the executor.

## Staffing evidence and regret

The staffing optimizer combines quality, domain fit, reliability, experience, availability, and cost. Historical `RoleOutcome` rows from scorecards replace cold-start estimates when available. A declared cost ceiling is a hard eligibility constraint, not merely a ranking penalty. The recruiter receives the recommendation; its deterministic fallback selects it directly.

After execution, record the selected employee's observed score and, when available, alternative outcomes. Staffing regret is `max(0, best counterfactual - selected observed)` and becomes evidence for the next decision.

```bash
uv run opc ops staffing recommend --request staffing-request.json --project demo
uv run opc ops staffing observe <decision-id> --observed-score 0.83 \
  --alternatives alternative-scores.json --project demo
```

## Mission Control

Mission Control is deterministic and model-free. It reports active/blocked runs, failed or missing scorecards, pending and dead-letter deliveries, approval checkpoints, deadlines, learning candidates, promoted assets, tracked score and cost, provider SLOs, and unmeasured usage. Alerts are ordered critical → high → medium → low.

It is available through:

- `opc ops mission status|brief`;
- the `operations_mission_control` agent tool;
- the secretary's prompt context and `SecretaryService.mission_brief()`;
- the project-scoped `Mission Control` page in Office UI, with manual refresh and a visibility-aware 30-second polling interval.

The UI displays durable work, gate failures, delivery/approval queues, provider SLOs, subscription call quotas, ordered alerts, and deterministic next actions. A late WebSocket response is discarded after a project switch.

## Schema and migration

`OPCStore.initialize()` creates additive schema version 3 tables:

- `goal_contracts`, `goal_contract_versions`, `run_manifests`, `run_scorecards`;
- `operating_events`, `outbox_messages`, `run_leases`;
- `outbox_delivery_receipts`, `route_execution_contracts`, `provider_usage_events`;
- `provider_canary_results`, `resource_approval_uses`, `provider_call_reservations`;
- `learning_assets`, `learning_asset_evaluations`;
- `capability_attempts`, `staffing_decisions`.

Version 3 adds atomic subscription call reservations plus approval/operator/key audit columns. The migration does not rewrite existing task, work-item, runtime, or memory tables. A database created by a newer operations schema fails closed instead of being opened by older code. `operations_schema` records the installed component version; migration fixtures cover the original schema and current upgrades.

## CI regression gate

CI runs the full test suite and then compares a candidate scorecard with the checked-in baseline:

```bash
uv run python scripts/operations_regression_gate.py \
  --baseline tests/fixtures/operations_scorecard_baseline.json \
  --candidate tests/fixtures/operations_scorecard_candidate.json \
  --maximum-regression 0.05
```

Replace the candidate artifact with a generated benchmark scorecard when a live benchmark pipeline is available; the comparison contract and failure behavior remain the same.

CI also builds both private NU dependencies as wheels and verifies only their public facades in an isolated environment. The repeatable golden loop then executes 20 goal → route contract → measured usage → scorecard → atomic goal settlement → canary → outbox → shadow-learning cycles and fails if any invariant is false:

```bash
uv run python scripts/verify_nu_compatibility.py
uv run python scripts/operations_golden_e2e.py --iterations 20 \
  --output-dir .artifacts/operations-golden
```
