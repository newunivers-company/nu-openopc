# Validation Report — 2026-07-30

## Result

The recommended benchmark hardening sequence is implemented and regression
tested. Live Task/Company canaries now preserve the full acceptance contract,
bounded workspace evidence, exact resource telemetry, and fail-closed expansion
state. Engineering readiness passes; product promotion remains correctly
blocked on trusted judgment and sample floors.

## Implemented controls

- Both arms receive identical deliverables, criterion descriptions and minima,
  required evidence, sealed fixtures, and network policy.
- Every slot uses an isolated project/workspace and captures at most 200 UTF-8
  files, 1 MB per file, and 10 MB total. Runtime state, caches, secrets,
  binaries, and oversized files are excluded.
- Completed unscored historical slots can be backfilled with
  `benchmark refresh-artifacts`; scored or incomplete runs are rejected.
- One absolute deadline covers execution and all checkpoint/continuation
  subprocesses. A POSIX process group receives TERM and then bounded KILL.
- Harness subprocesses and brokered external-agent calls have separate exact
  ceilings. Atomic permit files prevent concurrent Company roles from
  oversubscribing the call budget; failed calls remain charged.
- SQLite retries are limited to two recognized transient lock errors. Other
  operational errors remain immediate failures.
- `campaign-status.batch_expansion` allows one canary pair at a time and sets
  `next_pair_budget=0` while execution, judgment, or observation work is open.
- Pytest collection is restricted to the two product test roots, preventing
  archived benchmark workspaces from becoming accidental repository tests.

## Live canary evidence

Campaign `vision-crossworkload-20260730` used the sealed v1 suite, Codex for the
Task arm, the corporate profile for Company, a 2,700-second absolute deadline,
48 harness invocations, and 32 external-agent calls.

| Workload / arm | Result | Duration | External calls | Harness processes | Snapshot |
| --- | --- | ---: | ---: | ---: | ---: |
| Content / Task | completed | 467.2 s | 1/32 | 1/48 | 9 files, 26,665 B |
| Content / Company | completed | 2,472.0 s | 28/32 | 9/48 | 7 files, 73,282 B |
| Research / Task | completed | 195.6 s | 2/32 | 1/48 | 7 files, 19,415 B |
| Research / Company | completed | 1,869.5 s | 28/32 | 3/48 | 5 files, 39,769 B |

All four final snapshots are complete and have distinct SHA-256 artifact
digests. The Research Task initially hit a post-execution SQLite lock after
362.4 seconds; after bounded lock handling was added, the same sealed slot
completed normally in 195.6 seconds.

A separate strict diagnostic Company canary exhausted exactly 24 of 24 external
calls and the 1,800-second deadline. It failed without exceeding either budget,
and the process group was removed without an orphan. This run is diagnostic
evidence, not a quality sample.

## Judgment pre-screen

Claude Opus produced `llm_draft` scorecards from the captured artifacts. These
scores are useful for triage but carry no independent or human authority.

| Run | Criterion minima | Draft scores | Draft result |
| --- | --- | --- | --- |
| Content / Task | 0.90 / 0.95 / 0.85 | 0.93 / 0.91 / 0.90 | evidence hygiene below minimum |
| Content / Company | 0.90 / 0.95 / 0.85 | 0.80 / 0.76 / 0.88 | consistency and evidence hygiene below minimum |
| Research / Task | 0.85 / 0.95 / 0.90 | 0.93 / 0.96 / 0.92 | draft pass |
| Research / Company | 0.85 / 0.95 / 0.90 | 0.92 / 0.95 / 0.88 | traceability below minimum |

The live campaign therefore reports four pending judgments, zero trusted pairs,
68 not-started slots, `batch_expansion.phase=blocked`,
`next_pair_budget=0`, and `promotion_eligible=false`. No draft was mislabeled
as trusted evidence.

## Self-Grown experiment

The four completed canary runs were used as provenance for a
`release_playbook` candidate. It remains `candidate`, `shadow_only`, confidence
0.6, and explicitly disables automatic promotion.

Experiment `vision-release-playbook-20260730` seals three alternating matched
pairs (six slots), empty activation for control arms, exact asset
id/version/content digest for treated arms, and plan digest
`e595bafcebb9bce2506e467fecbc67933eaeb1b4484e6dbdecc05d837a7f858f`.
The effectiveness gate returns `insufficient_evidence` and exit code 1 under
`--fail-on-blocked` because both arms have 0 of 3 required trusted samples.

## Regression evidence

- Ruff: pass.
- Python: 2,264 passed, 17 skipped, 38 subtests passed.
- Frontend: typecheck pass; 26 unit scripts pass; 23 Vitest tests pass;
  production build pass; 20 E2E scroll assertions pass.
- NU release manifest and installed compatibility: pass
  (`nu-llm-routing-lib` 0.4.0, `nu-resource-gen-lib` 0.2.2).
- Benchmark execution readiness: v1 12/12 and v2 18/18 cases ready.
- Operations regression gate: pass with no violations.
- Operations golden loop: 20/20 goals, route contracts, measured usage events,
  scorecards, canaries, and outbox deliveries; all invariants pass.
- Packaging: source distribution and wheel build successfully.

## Remaining governed work

1. A human or genuinely independent judge must review and confirm or adjust the
   four canary drafts.
2. Confirmed scorecards must be recorded as trusted observations.
3. Only after the expansion gate reopens should the missing Software canary and
   later one-pair batches run.
4. Product promotion still requires at least ten trusted Task/Company pairs per
   workload and the configured non-regression confidence gates.
5. Self-Grown effectiveness still requires three trusted treated/control pairs
   plus normal offline, shadow, and canary lifecycle evaluations.
