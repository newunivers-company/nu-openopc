"""CLI surface for the goal-to-learning operating kernel."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar

import typer

from opc.core.config import OPCConfig, get_opc_home, get_project_workplace
from opc.database.store import OPCStore
from opc.integrations.nu_llm_routing import NULlmRoutingBridge
from opc.integrations.nu_resource_gen import NUResourceGenBridge
from opc.operations.models import (
    AcceptanceCriterion,
    CapabilityRequest,
    GoalContract,
    GoalContractStatus,
    LearningAssetStatus,
    ResourceBudget,
    RoleOutcome,
    RunManifest,
    RunMetrics,
    RunStatus,
    StaffingCandidate,
    utc_now,
)
from opc.operations.canary import (
    build_drill_result_template,
    build_readiness_campaign_plan,
    run_status_canary_loop,
)
from opc.operations.shadow_artifacts import ShadowArtifactStore
from opc.operations.shadow_traffic import drive_shadow_prompts, load_prompts
from opc.operations.backup import OperationsBackupManager
from opc.operations.benchmarks import (
    DEFAULT_SUITE_PATH,
    build_campaign_plan,
    campaign_progress,
    evaluate_outcomes,
    load_observations,
    load_suite,
    observation_from_run,
    suite_execution_readiness,
)
from opc.operations.campaign_runner import (
    CampaignBudget,
    CampaignRunner,
    CampaignSlotRunner,
    SubprocessExecutorConfig,
    SubprocessSlotExecutor,
    build_slot_command,
    campaign_status,
    find_slot,
    pending_judgment_slots,
    refresh_workspace_artifacts,
    slot_execution_project_id,
    subprocess_executor_preflight,
    verify_plan,
)
from opc.operations.judging import (
    JudgmentRubric,
    build_draft_prompt,
    confirm_draft,
    draft_for_goal,
    parse_draft_response,
)
from opc.operations.learning_effectiveness import LearningEffectivenessPolicy
from opc.operations.mode_advisor import (
    ModeAssessmentRequest,
    assess_execution_mode,
)
from opc.operations.experiments import (
    ShadowQualityObservation,
    bind_shadow_observation_to_transport,
    evaluate_shadow_promotion,
    load_shadow_observations,
    shadow_review_queue,
    shadow_transport_report,
)
from opc.operations.service import OperationsService
from opc.operations.promotion import build_promotion_dossier
from opc.operations.outbox import IndependentOutboxWorker, audit_outbox_handler
from opc.operations.resource_pipeline import ResourcePipelineRequest
from opc.layer5_memory.skill_library import SkillLibrary


T = TypeVar("T")


def register_operations_cli(app: typer.Typer) -> None:
    ops_app = typer.Typer(help="Outcome contracts, durable runs, learning, and Mission Control")
    goal_app = typer.Typer(help="Manage goal contracts")
    run_app = typer.Typer(help="Manage run manifests and recovery")
    evaluation_app = typer.Typer(help="Score runs and enforce regression gates")
    learning_app = typer.Typer(help="Govern self-grown learning assets")
    capability_app = typer.Typer(help="Plan unified capability routes")
    staffing_app = typer.Typer(help="Recommend staff and record staffing regret")
    outbox_app = typer.Typer(help="Inspect and recover durable deliveries")
    mission_app = typer.Typer(help="Inspect the secretary Mission Control view")
    resource_app = typer.Typer(help="Run approval-gated NU resource pipelines")
    backup_app = typer.Typer(help="Create, inspect, and restore verified SQLite snapshots")
    benchmark_app = typer.Typer(help="Run versioned Task-versus-Company outcome benchmarks")
    mode_app = typer.Typer(help="Assess Task versus Company execution fit")
    skills_app = typer.Typer(help="Assemble installed skills for explicit role capabilities")
    judge_app = typer.Typer(
        help="LLM-drafted, human-confirmed judgment for benchmark scorecards"
    )

    app.add_typer(ops_app, name="ops")
    ops_app.add_typer(goal_app, name="goal")
    ops_app.add_typer(run_app, name="run")
    ops_app.add_typer(evaluation_app, name="evaluate")
    ops_app.add_typer(learning_app, name="learning")
    ops_app.add_typer(capability_app, name="capability")
    ops_app.add_typer(staffing_app, name="staffing")
    ops_app.add_typer(outbox_app, name="outbox")
    ops_app.add_typer(mission_app, name="mission")
    ops_app.add_typer(resource_app, name="resource")
    ops_app.add_typer(backup_app, name="backup")
    ops_app.add_typer(benchmark_app, name="benchmark")
    ops_app.add_typer(mode_app, name="mode")
    ops_app.add_typer(skills_app, name="skills")
    ops_app.add_typer(judge_app, name="judge")

    @judge_app.command("draft")
    def judge_draft(
        run_id: str = typer.Argument(...),
        artifacts_dir: Path = typer.Option(..., "--artifacts", help="Slot artifact directory"),
        output: Path = typer.Option(..., "--output", help="Draft judgment JSON path"),
        emit_prompt: Optional[Path] = typer.Option(
            None,
            "--emit-prompt",
            help="Write the draft prompt messages instead of calling an LLM",
        ),
        max_artifact_chars: int = typer.Option(24_000, "--max-artifact-chars", min=1_000),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Produce an LLM DRAFT of criterion scores. Drafts carry no authority."""

        artifacts: dict[str, str] = {}
        for path in sorted(Path(artifacts_dir).rglob("*")):
            if not path.is_file() or path.name in {
                "artifact-index.json",
                "result-skeleton.json",
            }:
                continue
            artifacts[str(path.relative_to(artifacts_dir))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        if not artifacts:
            raise ValueError(f"no artifacts found under {artifacts_dir}")

        async def action(service: OperationsService) -> dict[str, Any]:
            manifest = await service.repository.get_manifest(run_id)
            if manifest is None:
                raise KeyError(f"run manifest not found: {run_id}")
            goal = await service.repository.get_goal(manifest.goal_id)
            if goal is None:
                raise KeyError(f"goal contract not found: {manifest.goal_id}")
            rubric = JudgmentRubric.from_goal(goal.to_dict())
            messages = build_draft_prompt(
                rubric,
                artifacts=artifacts,
                max_artifact_chars=max_artifact_chars,
            )
            if emit_prompt is not None:
                emit_prompt.parent.mkdir(parents=True, exist_ok=True)
                emit_prompt.write_text(
                    json.dumps(messages, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return {
                    "run_id": run_id,
                    "rubric_digest": rubric.digest,
                    "prompt_path": str(emit_prompt),
                    "note": "prompt emitted; no LLM was called",
                }
            from opc.llm.provider import LLMProvider

            opc_home = get_opc_home()
            config_dir = opc_home / "config"
            config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
            provider = LLMProvider(config.llm, opc_home=opc_home)
            response = await provider.simple_chat(
                messages[1]["content"], system=messages[0]["content"]
            )
            draft = parse_draft_response(
                rubric,
                run_id=run_id,
                judge_model=_served_judge_model(provider, config.llm.default_model),
                response_text=response,
            )
            return draft.to_dict()

        draft_payload = _run(project, action)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(draft_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _emit(draft_payload)

    @judge_app.command("confirm")
    def judge_confirm(
        draft_path: Path = typer.Option(..., "--draft", help="Draft judgment JSON"),
        operator: str = typer.Option(..., "--operator", help="Confirming human operator id"),
        authority: str = typer.Option(
            "human_confirmed",
            "--authority",
            help="human_confirmed or independent_judge",
        ),
        adjust_score: list[str] = typer.Option(
            [],
            "--adjust-score",
            help="Override a draft score as CRITERION=SCORE; repeatable",
        ),
        skeleton: Optional[Path] = typer.Option(
            None,
            "--skeleton",
            help="result-skeleton.json to copy evidence and metrics from",
        ),
        output: Path = typer.Option(..., "--output", help="evaluate-score result JSON"),
    ) -> None:
        """Confirm a reviewed draft into an ``evaluate score`` result payload."""

        draft = _load_mapping(draft_path)
        adjustments: dict[str, float] = {}
        for item in adjust_score:
            criterion_id, separator, raw = str(item).partition("=")
            if not separator:
                raise ValueError("--adjust-score must use CRITERION=SCORE format")
            adjustments[criterion_id.strip()] = float(raw)
        skeleton_payload = _load_mapping(skeleton) if skeleton is not None else {}
        result = confirm_draft(
            draft,
            operator_id=operator,
            authority=authority,
            adjusted_scores=adjustments,
            evidence=skeleton_payload.get("evidence", {}),
        )
        if skeleton_payload.get("metrics"):
            result["metrics"] = skeleton_payload["metrics"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _emit(result)

    @skills_app.command("recommend")
    def skills_recommend(
        request_json: Path = typer.Option(..., "--request"),
        output: Optional[Path] = typer.Option(None, "--output"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            payload = _load_mapping(request_json)
            library = SkillLibrary(get_opc_home())
            library.load_all(project)
            service.bind_skill_library(library)
            return service.skill_assembly.recommend(
                goal=str(payload.get("goal", "")),
                roles=[
                    dict(item)
                    for item in payload.get("roles", []) or []
                    if isinstance(item, Mapping)
                ],
                required_capabilities=[
                    str(item)
                    for item in payload.get("required_capabilities", []) or []
                ],
                project_id=project,
                max_additions_per_role=int(
                    payload.get("max_additions_per_role", 4) or 4
                ),
            )

        report = _run(project, action)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @mode_app.command("assess")
    def mode_assess(
        request_json: Path = typer.Option(..., "--request"),
        output: Optional[Path] = typer.Option(None, "--output"),
    ) -> None:
        """Recommend Task, Company, or clarification from explicit work evidence."""

        report = assess_execution_mode(
            ModeAssessmentRequest.from_dict(_load_mapping(request_json))
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @benchmark_app.command("validate")
    def benchmark_validate(
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        require_execution_ready: bool = typer.Option(
            False,
            "--require-execution-ready",
            help="Exit non-zero when fixtures or evidence contracts are incomplete",
        ),
    ) -> None:
        suite = load_suite(suite_path)
        preflight = suite_execution_readiness(suite)
        report = {
            "valid": True,
            "execution_ready": preflight["execution_ready"],
            "suite_id": suite.suite_id,
            "version": suite.version,
            "status": suite.status,
            "digest": suite.digest,
            "cases": len(suite.cases),
            "workloads": list(suite.workloads),
            "repetitions": suite.repetitions,
            "minimum_paired_samples_per_workload": (
                suite.minimum_paired_samples_per_workload
            ),
            "execution_preflight": preflight,
        }
        _emit(report)
        if require_execution_ready and not preflight["execution_ready"]:
            raise typer.Exit(code=1)

    @benchmark_app.command("plan")
    def benchmark_plan(
        campaign_id: str = typer.Option(..., "--campaign-id"),
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        output: Optional[Path] = typer.Option(None, "--output"),
        allow_unready: bool = typer.Option(
            False,
            "--allow-unready",
            help="Seal a diagnostic plan even when input preflight is blocked",
        ),
    ) -> None:
        report = build_campaign_plan(
            load_suite(suite_path),
            campaign_id=campaign_id,
            require_execution_ready=not allow_unready,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @benchmark_app.command("progress")
    def benchmark_progress(
        campaign_id: str = typer.Option(..., "--campaign-id"),
        observations: Path = typer.Option(..., "--observations"),
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
    ) -> None:
        report = campaign_progress(
            load_suite(suite_path),
            load_observations(observations),
            campaign_id=campaign_id,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if fail_on_blocked and not report["promotion_eligible"]:
            raise typer.Exit(code=1)

    @benchmark_app.command("evaluate")
    def benchmark_evaluate(
        observations: Path = typer.Option(..., "--observations"),
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
    ) -> None:
        suite = load_suite(suite_path)
        report = evaluate_outcomes(suite, load_observations(observations))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if fail_on_blocked and not report["promotion_eligible"]:
            raise typer.Exit(code=1)

    @benchmark_app.command("observe-run")
    def benchmark_observe_run(
        case_id: str = typer.Argument(...),
        run_id: str = typer.Argument(...),
        mode: str = typer.Option(..., "--mode", help="task or company"),
        repetition: int = typer.Option(..., "--repetition", min=1),
        authority: str = typer.Option(
            ...,
            "--authority",
            help="human_confirmed or independent_judge",
        ),
        artifact_digest: str = typer.Option(..., "--artifact-digest"),
        campaign_id: str = typer.Option("", "--campaign-id"),
        observations: Path = typer.Option(..., "--observations"),
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        suite = load_suite(suite_path)

        async def action(service: OperationsService) -> dict[str, Any]:
            manifest = await service.repository.get_manifest(run_id)
            scorecard = await service.repository.get_scorecard(run_id)
            if manifest is None:
                raise KeyError(f"run manifest not found: {run_id}")
            if scorecard is None:
                raise KeyError(f"run scorecard not found: {run_id}")
            row = observation_from_run(
                suite,
                case_id=case_id,
                mode=mode,
                repetition=repetition,
                manifest=manifest,
                scorecard=scorecard,
                authority=authority,
                artifact_digest=artifact_digest,
                campaign_id=campaign_id,
            )
            if not row.trusted:
                raise ValueError(
                    "observation lacks trusted authority, evidence, or a SHA-256 artifact digest"
                )
            return row.to_dict()

        row = _run(project, action)
        observations.parent.mkdir(parents=True, exist_ok=True)
        existing = load_observations(observations) if observations.exists() else []
        slot = (case_id, mode, repetition)
        if any(
            (item.case_id, item.mode, item.repetition) == slot
            for item in existing
        ):
            raise ValueError(
                f"benchmark slot already exists: {case_id}/{mode}/{repetition}"
            )
        with observations.open("a", encoding="utf-8") as output_file:
            output_file.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )
        _emit(row)

    @benchmark_app.command("run-slot")
    def benchmark_run_slot(
        slot_id: str = typer.Argument(...),
        plan_path: Path = typer.Option(..., "--plan", help="Sealed campaign plan JSON"),
        artifacts_root: Path = typer.Option(
            Path("outputs/benchmark"), "--artifacts-root"
        ),
        force: bool = typer.Option(False, "--force", help="Re-run an existing run_id"),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the executor command without running"
        ),
        timeout_seconds: float = typer.Option(3600.0, "--timeout-seconds", min=1),
        max_continuations: int = typer.Option(
            12, "--max-continuations", min=0
        ),
        max_process_invocations: int = typer.Option(
            48, "--max-process-invocations", min=1
        ),
        max_external_agent_calls: int = typer.Option(
            24, "--max-external-agent-calls", min=1
        ),
        task_agent: str = typer.Option(
            "native",
            "--task-agent",
            help="Pinned Task arm executor: native, codex, claude_code, cursor, or opencode",
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Execute one campaign slot end to end and emit its scoring skeleton."""

        plan = verify_plan(_load_mapping(plan_path))
        executor_config = _benchmark_executor_config(
            task_agent=task_agent,
            timeout_seconds=timeout_seconds,
            max_continuations=max_continuations,
            max_process_invocations=max_process_invocations,
            max_external_agent_calls=max_external_agent_calls,
        )
        if dry_run:
            slot = find_slot(plan, slot_id)
            execution_project_id = slot_execution_project_id(project, slot)
            _emit(
                {
                    "dry_run": True,
                    "slot_id": slot_id,
                    "run_id": slot["run_id"],
                    "execution_project_id": execution_project_id,
                    "command": build_slot_command(
                        slot,
                        project_id=execution_project_id,
                        config=executor_config,
                    ),
                    "artifact_directory": str(
                        artifacts_root / slot["artifact_directory"]
                    ),
                    "budgets": {
                        "slot_timeout_seconds": timeout_seconds,
                        "max_continuations": max_continuations,
                        "max_process_invocations": max_process_invocations,
                        "max_external_agent_calls": max_external_agent_calls,
                    },
                }
            )
            return

        async def action(service: OperationsService) -> dict[str, Any]:
            preflight = subprocess_executor_preflight(
                executor_config,
                native_transport_ready=(
                    service.capabilities.default_llm_transport_ready
                ),
            )
            if not preflight["execution_ready"]:
                raise ValueError(
                    "benchmark executor preflight failed: "
                    + "; ".join(preflight["blockers"])
                )
            runner = CampaignSlotRunner(
                service,
                SubprocessSlotExecutor(
                    project_id=project,
                    config=executor_config,
                ),
                project_id=project,
                artifacts_root=artifacts_root,
            )
            result = await runner.run_slot(plan, slot_id, force=force)
            return result.to_dict()

        result = _run(project, action)
        _emit(result)
        if result["status"] == "failed":
            raise typer.Exit(code=1)

    @benchmark_app.command("refresh-artifacts")
    def benchmark_refresh_artifacts(
        slot_id: str = typer.Argument(...),
        plan_path: Path = typer.Option(..., "--plan", help="Sealed campaign plan JSON"),
        artifacts_root: Path = typer.Option(
            Path("outputs/benchmark"), "--artifacts-root"
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Backfill an unscored completed slot with isolated workspace files."""

        plan = verify_plan(_load_mapping(plan_path))
        slot = find_slot(plan, slot_id)
        artifact_dir = artifacts_root / str(slot["artifact_directory"])

        async def action(service: OperationsService) -> dict[str, Any]:
            run_id = str(slot["run_id"])
            manifest = await service.repository.get_manifest(run_id)
            if manifest is None or manifest.status != RunStatus.COMPLETED:
                raise ValueError("artifact refresh requires a completed run")
            if await service.repository.get_scorecard(run_id) is not None:
                raise ValueError("artifact refresh is forbidden after scoring")
            skeleton = _load_mapping(artifact_dir / "result-skeleton.json")
            executor = dict(
                dict(skeleton.get("metadata", {}) or {}).get("executor", {}) or {}
            )
            execution_project_id = str(
                executor.get("execution_project_id", "")
                or slot_execution_project_id(project, slot)
            )
            report = refresh_workspace_artifacts(
                artifact_dir,
                get_project_workplace(execution_project_id),
            )
            manifest.metadata = {
                **dict(manifest.metadata),
                "benchmark_artifact_digest": report["artifact_digest"],
                "benchmark_workspace_snapshot": report["workspace_snapshot"],
            }
            await service.repository.save_manifest(manifest)
            return {
                "slot_id": slot_id,
                "run_id": run_id,
                "execution_project_id": execution_project_id,
                **report,
            }

        _emit(_run(project, action, run_startup_maintenance=False))

    @benchmark_app.command("run-campaign")
    def benchmark_run_campaign(
        plan_path: Path = typer.Option(..., "--plan", help="Sealed campaign plan JSON"),
        artifacts_root: Path = typer.Option(
            Path("outputs/benchmark"), "--artifacts-root"
        ),
        max_slots: Optional[int] = typer.Option(None, "--max-slots", min=1),
        max_pairs: Optional[int] = typer.Option(
            None,
            "--max-pairs",
            min=1,
            help="Start at most this many incomplete Task/Company pairs; "
            "completed pairs do not consume the budget",
        ),
        max_failures: int = typer.Option(3, "--max-failures", min=0),
        max_cost_usd: Optional[float] = typer.Option(
            None,
            "--max-cost-usd",
            min=0.000001,
            help="Halt when MEASURED usage cost reaches this ceiling "
            "(subscription CLI calls are unmeasured and governed by call quotas)",
        ),
        max_hours: Optional[float] = typer.Option(
            None,
            "--max-hours",
            min=0.01,
            help="Halt before starting a new slot once this wall-clock "
            "budget is exhausted (unattended overnight guard)",
        ),
        workload: list[str] = typer.Option([], "--workload"),
        mode: list[str] = typer.Option([], "--mode"),
        stop_on_failure: bool = typer.Option(False, "--stop-on-failure"),
        timeout_seconds: float = typer.Option(3600.0, "--timeout-seconds", min=1),
        max_continuations: int = typer.Option(
            12, "--max-continuations", min=0
        ),
        max_process_invocations: int = typer.Option(
            48, "--max-process-invocations", min=1
        ),
        max_external_agent_calls: int = typer.Option(
            24, "--max-external-agent-calls", min=1
        ),
        task_agent: str = typer.Option(
            "native",
            "--task-agent",
            help="Pinned Task arm executor: native, codex, claude_code, cursor, or opencode",
        ),
        output: Optional[Path] = typer.Option(None, "--output"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Drive campaign slots in sequence with budget ceilings; resumable."""

        plan = verify_plan(_load_mapping(plan_path))
        executor_config = _benchmark_executor_config(
            task_agent=task_agent,
            timeout_seconds=timeout_seconds,
            max_continuations=max_continuations,
            max_process_invocations=max_process_invocations,
            max_external_agent_calls=max_external_agent_calls,
        )

        async def action(service: OperationsService) -> dict[str, Any]:
            preflight = subprocess_executor_preflight(
                executor_config,
                native_transport_ready=(
                    service.capabilities.default_llm_transport_ready
                ),
            )
            if not preflight["execution_ready"]:
                raise ValueError(
                    "benchmark executor preflight failed: "
                    + "; ".join(preflight["blockers"])
                )
            campaign = CampaignRunner(
                CampaignSlotRunner(
                    service,
                    SubprocessSlotExecutor(
                        project_id=project,
                        config=executor_config,
                    ),
                    project_id=project,
                    artifacts_root=artifacts_root,
                )
            )
            report = await campaign.run_campaign(
                plan,
                budget=CampaignBudget(
                    max_slots=max_slots,
                    max_pairs=max_pairs,
                    max_failures=max_failures,
                    max_cost_usd=max_cost_usd,
                    max_wall_clock_seconds=(
                        max_hours * 3600.0 if max_hours is not None else None
                    ),
                ),
                workloads=workload or None,
                modes=mode or None,
                stop_on_failure=stop_on_failure,
            )
            report["executor_preflight"] = preflight
            return report

        report = _run(project, action)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if report["failed"]:
            raise typer.Exit(code=1)

    @benchmark_app.command("campaign-status")
    def benchmark_campaign_status(
        plan_path: Path = typer.Option(..., "--plan", help="Sealed campaign plan JSON"),
        observations: Optional[Path] = typer.Option(None, "--observations"),
        suite_path: Path = typer.Option(DEFAULT_SUITE_PATH, "--suite"),
        output: Optional[Path] = typer.Option(None, "--output"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """One view of a campaign: execution, judgment backlog, gate distance."""

        plan = verify_plan(_load_mapping(plan_path))
        suite = load_suite(suite_path)
        rows = (
            load_observations(observations)
            if observations is not None and observations.exists()
            else []
        )

        async def action(service: OperationsService) -> dict[str, Any]:
            return await campaign_status(service, suite, plan, rows)

        report = _run(
            project,
            action,
            run_startup_maintenance=False,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @benchmark_app.command("shadow-gate")
    def benchmark_shadow_gate(
        observations: Path = typer.Option(..., "--observations"),
        experiment_id: str = typer.Option(..., "--experiment-id"),
        transport_db: Optional[Path] = typer.Option(None, "--transport-db"),
        minimum_samples_per_workload: int = typer.Option(
            30,
            "--minimum-samples-per-workload",
            min=2,
        ),
        minimum_transport_decisions: int = typer.Option(
            20,
            "--minimum-transport-decisions",
            min=1,
        ),
        maximum_quality_regression: float = typer.Option(
            0.02,
            "--maximum-quality-regression",
            min=0,
            max=1,
        ),
        maximum_success_regression: float = typer.Option(
            0.02,
            "--maximum-success-regression",
            min=0,
            max=1,
        ),
        minimum_quality_improvement: float = typer.Option(
            0.0,
            "--minimum-quality-improvement",
            min=0,
            max=1,
        ),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
    ) -> None:
        transport = (
            shadow_transport_report(
                transport_db,
                minimum_decisions=minimum_transport_decisions,
            )
            if transport_db is not None
            else None
        )
        report = evaluate_shadow_promotion(
            load_shadow_observations(observations),
            experiment_id=experiment_id,
            minimum_samples_per_workload=minimum_samples_per_workload,
            maximum_quality_regression=maximum_quality_regression,
            maximum_success_regression=maximum_success_regression,
            minimum_quality_improvement=minimum_quality_improvement,
            transport_report=transport,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if fail_on_blocked and not report["promotion_ready"]:
            raise typer.Exit(code=1)

    @benchmark_app.command("shadow-review-queue")
    def benchmark_shadow_review_queue(
        experiment_id: str = typer.Option(..., "--experiment-id"),
        transport_db: Path = typer.Option(..., "--transport-db"),
        observations: Optional[Path] = typer.Option(None, "--observations"),
        output: Optional[Path] = typer.Option(None, "--output"),
    ) -> None:
        rows = (
            load_shadow_observations(observations)
            if observations is not None and observations.exists()
            else []
        )
        report = shadow_review_queue(
            transport_db,
            experiment_id=experiment_id,
            observations=rows,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @benchmark_app.command("shadow-artifacts-add")
    def benchmark_shadow_artifacts_add(
        decision_id: str = typer.Argument(...),
        served: Path = typer.Option(..., "--served", help="Served response text file"),
        challenger: Path = typer.Option(..., "--challenger", help="Challenger response text file"),
        store: Path = typer.Option(..., "--store", help="Artifact store directory"),
        overwrite: bool = typer.Option(False, "--overwrite"),
    ) -> None:
        """Store a served/challenger pair so judges never re-capture texts."""

        index = ShadowArtifactStore(store).add(
            decision_id,
            served_text=served.read_text(encoding="utf-8"),
            challenger_text=challenger.read_text(encoding="utf-8"),
            overwrite=overwrite,
        )
        _emit(index)

    @benchmark_app.command("shadow-artifacts-show")
    def benchmark_shadow_artifacts_show(
        decision_id: str = typer.Argument(...),
        store: Path = typer.Option(..., "--store"),
    ) -> None:
        """Show a stored pair's digests after verifying it was not altered."""

        _emit(ShadowArtifactStore(store).get(decision_id))

    @benchmark_app.command("shadow-observe")
    def benchmark_shadow_observe(
        observation_json: Path = typer.Option(..., "--observation"),
        observations: Path = typer.Option(..., "--observations"),
        transport_db: Path = typer.Option(..., "--transport-db"),
        artifact_store: Optional[Path] = typer.Option(
            None,
            "--artifact-store",
            help="Fill missing served/challenger digests from this store",
        ),
        latency_tolerance_ms: float = typer.Option(
            1.0,
            "--latency-tolerance-ms",
            min=0,
        ),
    ) -> None:
        payload = _load_mapping(observation_json)
        if artifact_store is not None:
            digests = ShadowArtifactStore(artifact_store).digests(
                str(payload.get("decision_id", ""))
            )
            for key, value in digests.items():
                if not str(payload.get(key, "") or "").strip():
                    payload[key] = value
        row = bind_shadow_observation_to_transport(
            ShadowQualityObservation.from_dict(payload),
            transport_db,
            latency_tolerance_ms=latency_tolerance_ms,
        )
        existing = (
            load_shadow_observations(observations)
            if observations.exists()
            else []
        )
        if any(item.decision_id == row.decision_id for item in existing):
            raise ValueError(
                f"shadow decision already observed: {row.decision_id}"
            )
        observations.parent.mkdir(parents=True, exist_ok=True)
        with observations.open("a", encoding="utf-8") as output_file:
            output_file.write(
                json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True)
                + "\n"
            )
        _emit(row.to_dict())

    @benchmark_app.command("promotion-dossier")
    def benchmark_promotion_dossier(
        dependency_report: Path = typer.Option(
            ...,
            "--dependency-report",
        ),
        outcome_report: Path = typer.Option(..., "--outcome-report"),
        shadow_report: Path = typer.Option(..., "--shadow-report"),
        canary_report: Path = typer.Option(..., "--canary-report"),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
    ) -> None:
        report = build_promotion_dossier(
            dependency_report=_load_mapping(dependency_report),
            outcome_report=_load_mapping(outcome_report),
            shadow_report=_load_mapping(shadow_report),
            canary_report=_load_mapping(canary_report),
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if fail_on_blocked and not report["promotion_ready"]:
            raise typer.Exit(code=1)

    @capability_app.command("shadow-report")
    def capability_shadow_report(
        minimum_decisions: int = typer.Option(20, "--minimum-decisions", min=1),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            report = getattr(service.llm_router, "shadow_evidence_report", None)
            if not callable(report):
                return {
                    "available": False,
                    "blockers": ["NU LLM shadow bridge is unavailable"],
                }
            return report(minimum_decisions=minimum_decisions)

        _emit(_run(project, action, integrations=True))

    @capability_app.command("record-drill")
    def capability_record_drill(
        provider: str = typer.Option(..., "--provider"),
        scenario: str = typer.Option(..., "--scenario"),
        result_json: Path = typer.Option(..., "--result"),
        model: str = typer.Option("", "--model"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        result = _load_mapping(result_json)

        async def action(service: OperationsService) -> dict[str, Any]:
            row = await service.canaries.record_failure_drill(
                project_id=project,
                provider=provider,
                scenario=scenario,
                result=result,
                model=model,
            )
            return row.to_dict()

        _emit(_run(project, action))

    @judge_app.command("agreement")
    def judge_agreement(
        result_a: Path = typer.Option(..., "--result-a", help="First confirmed result JSON"),
        result_b: Path = typer.Option(..., "--result-b", help="Second confirmed result JSON"),
        goal_contract: Path = typer.Option(
            ..., "--goal-contract", help="GoalContract JSON providing criterion minimums"
        ),
        output: Optional[Path] = typer.Option(None, "--output"),
    ) -> None:
        """Measure inter-judge agreement (Cohen's kappa) for one run."""

        from opc.operations.judging import judgment_agreement

        goal = _load_mapping(goal_contract)
        minimums = {
            str(item["criterion_id"]): float(item.get("minimum_score", 0.0) or 0.0)
            for item in goal.get("acceptance_criteria", []) or []
        }
        report = judgment_agreement(
            _load_mapping(result_a),
            _load_mapping(result_b),
            minimum_scores=minimums,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @judge_app.command("draft-campaign")
    def judge_draft_campaign(
        plan_path: Path = typer.Option(..., "--plan", help="Sealed campaign plan JSON"),
        artifacts_root: Path = typer.Option(
            Path("outputs/benchmark"), "--artifacts-root"
        ),
        output_dir: Path = typer.Option(..., "--output-dir", help="Draft JSONs directory"),
        emit_prompts: bool = typer.Option(
            False,
            "--emit-prompts",
            help="Write draft prompts per run instead of calling an LLM",
        ),
        max_artifact_chars: int = typer.Option(24_000, "--max-artifact-chars", min=1_000),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Draft every completed-but-unscored run of a campaign in one pass.

        Humans still confirm each draft individually; this only batches the
        LLM pre-screening step so judgment sessions start from drafts.
        """

        plan = verify_plan(_load_mapping(plan_path))

        def _slot_artifacts(slot: Mapping[str, Any]) -> dict[str, str]:
            directory = artifacts_root / str(slot["artifact_directory"])
            artifacts: dict[str, str] = {}
            if directory.exists():
                for path in sorted(directory.rglob("*")):
                    if not path.is_file() or path.name in {
                        "artifact-index.json",
                        "result-skeleton.json",
                    }:
                        continue
                    artifacts[str(path.relative_to(directory))] = path.read_text(
                        encoding="utf-8", errors="replace"
                    )
            return artifacts

        async def action(service: OperationsService) -> dict[str, Any]:
            pending = await pending_judgment_slots(service, plan)
            drafted: list[dict[str, Any]] = []
            failures: list[dict[str, str]] = []
            output_dir.mkdir(parents=True, exist_ok=True)
            chat = None
            judge_model = ""
            if not emit_prompts and pending:
                from opc.llm.provider import LLMProvider

                opc_home = get_opc_home()
                config_dir = opc_home / "config"
                config = (
                    OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
                )
                provider = LLMProvider(config.llm, opc_home=opc_home)
                judge_model = config.llm.default_model

                async def chat(user: str, system: str) -> str:  # noqa: F811
                    return await provider.simple_chat(user, system=system)

            for slot in pending:
                run_id = str(slot["run_id"])
                try:
                    goal = await service.repository.get_goal(
                        str(slot["goal"]["goal_id"])
                    )
                    if goal is None:
                        raise KeyError("goal contract not found")
                    artifacts = _slot_artifacts(slot)
                    if not artifacts:
                        raise ValueError("no artifacts found for slot")
                    if emit_prompts:
                        rubric = JudgmentRubric.from_goal(goal.to_dict())
                        prompt_path = output_dir / f"{run_id}.prompt.json"
                        prompt_path.write_text(
                            json.dumps(
                                build_draft_prompt(
                                    rubric,
                                    artifacts=artifacts,
                                    max_artifact_chars=max_artifact_chars,
                                ),
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        drafted.append(
                            {"run_id": run_id, "prompt_path": str(prompt_path)}
                        )
                        continue
                    draft = await draft_for_goal(
                        chat,
                        goal=goal.to_dict(),
                        artifacts=artifacts,
                        run_id=run_id,
                        judge_model=judge_model,
                        max_artifact_chars=max_artifact_chars,
                    )
                    draft.judge_model = _served_judge_model(provider, judge_model)
                    draft_path = output_dir / f"{run_id}.draft.json"
                    draft_path.write_text(
                        json.dumps(
                            draft.to_dict(),
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    drafted.append({"run_id": run_id, "draft_path": str(draft_path)})
                except Exception as exc:  # noqa: BLE001 - one slot must not stop the batch
                    failures.append(
                        {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]}
                    )
            return {
                "pending": len(pending),
                "drafted": drafted,
                "failed": failures,
            }

        _emit(_run(project, action))

    @capability_app.command("drill-template")
    def capability_drill_template(
        scenario: str = typer.Argument(...),
        provider: str = typer.Option("", "--provider"),
        model: str = typer.Option("", "--model"),
        output: Optional[Path] = typer.Option(None, "--output"),
    ) -> None:
        """Emit a record-drill result skeleton with the scenario runbook."""

        template = build_drill_result_template(
            scenario, provider=provider, model=model
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(template, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(template)

    @capability_app.command("shadow-drive")
    def capability_shadow_drive(
        prompts_path: Path = typer.Option(..., "--prompts", help="JSON/JSONL prompts"),
        max_calls: int = typer.Option(..., "--max-calls", min=1, max=500),
        task_type: str = typer.Option("dialogue", "--task-type"),
        delay_seconds: float = typer.Option(0.0, "--delay-seconds", min=0),
        confirm_live: bool = typer.Option(
            False,
            "--confirm-live",
            help="Required: driving shadow traffic makes real provider calls",
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Replay curated text turns through the routed serving path so shadow
        decisions accumulate as genuine transport calls."""

        prompts = load_prompts(prompts_path)
        if not prompts:
            raise ValueError(f"no prompts found in {prompts_path}")

        async def action(service: OperationsService) -> dict[str, Any]:
            from opc.llm.provider import LLMProvider

            opc_home = get_opc_home()
            config_dir = opc_home / "config"
            config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
            provider = LLMProvider(config.llm, opc_home=opc_home)
            provider.bind_operations_service(service, project_id=project)
            shadow_status = provider.nu_router.start_background_shadow()
            if not shadow_status.get("running"):
                raise ValueError(
                    "background shadow is not running; configure "
                    "shadow_experiment_id and shadow_max_total_calls first: "
                    f"{shadow_status.get('error') or shadow_status}"
                )
            try:
                report = await drive_shadow_prompts(
                    lambda prompt: provider.simple_chat(prompt, task_type=task_type),
                    prompts,
                    max_calls=max_calls,
                    confirm_live=confirm_live,
                    delay_seconds=delay_seconds,
                )
            finally:
                shutdown = provider.nu_router.stop_background_shadow()
            report["shadow_status"] = shutdown
            return report

        _emit(_run(project, action, integrations=True))

    @capability_app.command("campaign-plan")
    def capability_campaign_plan(
        campaign_id: str = typer.Option(..., "--campaign-id"),
        provider: str = typer.Option(..., "--provider"),
        model: str = typer.Option("", "--model"),
        start_at: str = typer.Option("", "--start-at"),
        output: Optional[Path] = typer.Option(None, "--output"),
    ) -> None:
        config_dir = get_opc_home() / "config"
        config = (
            OPCConfig.load(config_dir)
            if config_dir.exists()
            else OPCConfig()
        ).system.operations.providers
        if start_at:
            parsed_start = datetime.fromisoformat(
                start_at.replace("Z", "+00:00")
            )
        else:
            parsed_start = datetime.now(timezone.utc)
        report = build_readiness_campaign_plan(
            campaign_id=campaign_id,
            provider=provider,
            model=model,
            start_at=parsed_start,
            interval_seconds=config.status_canary_interval_seconds,
            observation_seconds=config.readiness_min_observation_seconds,
            time_bucket_seconds=config.readiness_time_bucket_seconds,
            minimum_time_buckets=config.readiness_min_time_buckets,
            maximum_sample_age_seconds=(
                config.readiness_max_sample_age_seconds
            ),
            maximum_gap_seconds=config.readiness_max_gap_seconds,
            failure_drill_max_age_seconds=(
                config.readiness_failure_drill_max_age_seconds
            ),
            required_failure_scenarios=(
                config.readiness_required_failure_scenarios
            ),
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @capability_app.command("readiness")
    def capability_readiness(
        provider: str = typer.Option("", "--provider"),
        minimum_samples: Optional[int] = typer.Option(
            None,
            "--minimum-samples",
            min=1,
        ),
        trend_window_samples: Optional[int] = typer.Option(
            None,
            "--trend-window-samples",
            min=1,
        ),
        minimum_observation_seconds: Optional[int] = typer.Option(
            None,
            "--minimum-observation-seconds",
            min=0,
        ),
        time_bucket_seconds: Optional[int] = typer.Option(
            None,
            "--time-bucket-seconds",
            min=60,
        ),
        minimum_time_buckets: Optional[int] = typer.Option(
            None,
            "--minimum-time-buckets",
            min=1,
        ),
        minimum_samples_per_bucket: Optional[int] = typer.Option(
            None,
            "--minimum-samples-per-bucket",
            min=1,
        ),
        maximum_sample_age_seconds: Optional[int] = typer.Option(
            None,
            "--maximum-sample-age-seconds",
            min=0,
        ),
        maximum_gap_seconds: Optional[int] = typer.Option(
            None,
            "--maximum-gap-seconds",
            min=60,
        ),
        failure_drill_max_age_seconds: Optional[int] = typer.Option(
            None,
            "--failure-drill-max-age-seconds",
            min=60,
        ),
        required_drill: list[str] = typer.Option(
            [],
            "--required-drill",
        ),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            config = service.config.providers
            return await service.canaries.readiness_summary(
                project_id=project,
                provider=provider or None,
                availability_target=config.slo_availability_target,
                p95_latency_target_ms=config.slo_p95_latency_target_ms,
                minimum_samples=(
                    minimum_samples
                    if minimum_samples is not None
                    else config.slo_min_samples
                ),
                trend_window_samples=(
                    trend_window_samples
                    if trend_window_samples is not None
                    else config.slo_trend_window_samples
                ),
                minimum_observation_seconds=(
                    minimum_observation_seconds
                    if minimum_observation_seconds is not None
                    else config.readiness_min_observation_seconds
                ),
                time_bucket_seconds=(
                    time_bucket_seconds
                    if time_bucket_seconds is not None
                    else config.readiness_time_bucket_seconds
                ),
                minimum_time_buckets=(
                    minimum_time_buckets
                    if minimum_time_buckets is not None
                    else config.readiness_min_time_buckets
                ),
                minimum_samples_per_bucket=(
                    minimum_samples_per_bucket
                    if minimum_samples_per_bucket is not None
                    else config.readiness_min_samples_per_bucket
                ),
                maximum_sample_age_seconds=(
                    maximum_sample_age_seconds
                    if maximum_sample_age_seconds is not None
                    else config.readiness_max_sample_age_seconds
                ),
                maximum_gap_seconds=(
                    maximum_gap_seconds
                    if maximum_gap_seconds is not None
                    else config.readiness_max_gap_seconds
                ),
                failure_drill_max_age_seconds=(
                    failure_drill_max_age_seconds
                    if failure_drill_max_age_seconds is not None
                    else config.readiness_failure_drill_max_age_seconds
                ),
                required_failure_scenarios=(
                    required_drill
                    or config.readiness_required_failure_scenarios
                ),
            )

        report = _run(project, action)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        providers = report.get("providers", {})
        if fail_on_blocked and (
            not providers
            or not all(
                bool(item.get("production_ready"))
                for item in providers.values()
            )
        ):
            raise typer.Exit(code=1)

    @goal_app.command("create")
    def goal_create(
        title: str = typer.Option("", "--title", help="Goal title"),
        objective: str = typer.Option("", "--objective", help="Measurable objective"),
        criterion: list[str] = typer.Option(
            [],
            "--criterion",
            help="Repeat ID=DESCRIPTION; use --contract for advanced criteria",
        ),
        contract: Optional[Path] = typer.Option(None, "--contract", help="Full GoalContract JSON"),
        max_cost_usd: Optional[float] = typer.Option(None, "--max-cost-usd", min=0),
        max_duration_seconds: Optional[float] = typer.Option(None, "--max-duration-seconds", min=0),
        max_interventions: Optional[int] = typer.Option(None, "--max-interventions", min=0),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            if contract is not None:
                goal = GoalContract.from_dict(_load_mapping(contract))
                if goal.project_id == "default" and project != "default":
                    goal.project_id = project
            else:
                criteria = [_parse_criterion(item) for item in criterion]
                if not criteria:
                    raise ValueError("provide at least one --criterion or a --contract JSON file")
                goal = GoalContract(
                    project_id=project,
                    title=title,
                    objective=objective,
                    acceptance_criteria=criteria,
                    budget=ResourceBudget(
                        max_cost_usd=max_cost_usd,
                        max_duration_seconds=max_duration_seconds,
                        max_interventions=max_interventions,
                    ),
                )
            return (await service.repository.save_goal(goal)).to_dict()

        _emit(_run(project, action))

    @goal_app.command("list")
    def goal_list(
        status: Optional[str] = typer.Option(None, "--status"),
        limit: int = typer.Option(100, "--limit", "-n", min=1, max=1000),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> list[dict[str, Any]]:
            values = await service.repository.list_goals(
                project_id=project,
                status=status,
                limit=limit,
            )
            return [item.to_dict() for item in values]

        _emit(_run(project, action))

    @goal_app.command("show")
    def goal_show(
        goal_id: str = typer.Argument(...),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            value = await service.repository.get_goal(goal_id)
            if value is None:
                raise KeyError(f"goal contract not found: {goal_id}")
            return value.to_dict()

        _emit(_run(project, action))

    @goal_app.command("close")
    def goal_close(
        goal_id: str = typer.Argument(...),
        status: str = typer.Option("completed", "--status", help="completed or cancelled"),
        reason: str = typer.Option(..., "--reason"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            goal = await service.repository.get_goal(goal_id)
            if goal is None or goal.project_id != project:
                raise KeyError(f"goal not found: {goal_id}")
            try:
                terminal = GoalContractStatus(str(status).strip().lower())
            except ValueError as exc:
                raise ValueError("goal close status must be completed or cancelled") from exc
            if terminal not in {GoalContractStatus.COMPLETED, GoalContractStatus.CANCELLED}:
                raise ValueError("goal close status must be completed or cancelled")
            if goal.status in {GoalContractStatus.COMPLETED, GoalContractStatus.CANCELLED}:
                return goal.to_dict()
            updated = GoalContract.from_dict(goal.to_dict())
            updated.version = goal.version + 1
            updated.status = terminal
            updated.metadata = {
                **dict(goal.metadata),
                "closure": {
                    "source": "operator",
                    "reason": str(reason).strip(),
                    "closed_at": utc_now().isoformat(),
                },
            }
            if not updated.metadata["closure"]["reason"]:
                raise ValueError("goal close reason is required")
            return (await service.repository.save_goal(updated)).to_dict()

        _emit(_run(project, action))

    @run_app.command("start")
    def run_start(
        goal_id: str = typer.Argument(...),
        run_id: str = typer.Option("", "--run-id"),
        manifest_json: Optional[Path] = typer.Option(None, "--manifest", help="RunManifest JSON"),
        complete_goal_on_pass: bool = typer.Option(
            False,
            "--complete-goal-on-pass",
            help="Close the latest goal version after this run passes with no active sibling run",
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            if manifest_json is not None:
                manifest = RunManifest.from_dict(_load_mapping(manifest_json))
                manifest.goal_id = goal_id or manifest.goal_id
                if manifest.project_id == "default" and project != "default":
                    manifest.project_id = project
            else:
                manifest = RunManifest(
                    run_id=run_id or RunManifest(goal_id=goal_id).run_id,
                    goal_id=goal_id,
                    project_id=project,
                    status=RunStatus.RUNNING,
                    started_at=utc_now(),
                    metadata={"complete_goal_on_pass": complete_goal_on_pass},
                )
            if complete_goal_on_pass:
                manifest.metadata["complete_goal_on_pass"] = True
            saved, _ = await service.start_run(manifest)
            return saved.to_dict()

        _emit(_run(project, action))

    @run_app.command("finish")
    def run_finish(
        run_id: str = typer.Argument(...),
        status: str = typer.Option("completed", "--status", help="completed, failed, or cancelled"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            target = RunStatus(status)
            saved, _ = await service.durable.finish_run(run_id, status=target)
            return saved.to_dict()

        _emit(_run(project, action))

    @run_app.command("show")
    def run_show(
        run_id: str = typer.Argument(...),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            manifest = await service.repository.get_manifest(run_id)
            if manifest is None:
                raise KeyError(f"run manifest not found: {run_id}")
            scorecard = await service.repository.get_scorecard(run_id)
            events = await service.repository.list_events(run_id)
            return {
                "manifest": manifest.to_dict(),
                "scorecard": scorecard.to_dict() if scorecard else None,
                "events": events,
            }

        _emit(_run(project, action))

    @run_app.command("recover")
    def run_recover(
        run_id: str = typer.Argument(...),
        owner: str = typer.Option("opc-cli", "--owner"),
        metrics_json: Optional[Path] = typer.Option(None, "--metrics"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            metrics = RunMetrics.from_dict(_load_mapping(metrics_json)) if metrics_json else None
            return (
                await service.durable.recover_run(run_id, owner=owner, metrics=metrics)
            ).to_dict()

        _emit(_run(project, action))

    @evaluation_app.command("score")
    def evaluation_score(
        run_id: str = typer.Argument(...),
        result_json: Path = typer.Option(..., "--result", help="Evaluation input JSON"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            payload = _load_mapping(result_json)
            scorecard = await service.evaluator.evaluate_run(
                run_id,
                criterion_scores=dict(payload.get("criterion_scores", {}) or {}),
                evidence=dict(payload.get("evidence", {}) or {}),
                criterion_notes=dict(payload.get("criterion_notes", {}) or {}),
                metrics=RunMetrics.from_dict(payload.get("metrics")),
                role_outcomes=[
                    RoleOutcome.from_dict(item)
                    for item in payload.get("role_outcomes", []) or []
                    if isinstance(item, Mapping)
                ],
                baseline_label=str(payload.get("baseline_label", "") or ""),
                metadata=dict(payload.get("metadata", {}) or {}),
            )
            return scorecard.to_dict()

        _emit(_run(project, action))

    @evaluation_app.command("gate")
    def evaluation_gate(
        run_id: str = typer.Argument(...),
        baseline_run: Optional[str] = typer.Option(None, "--baseline-run"),
        baseline_label: Optional[str] = typer.Option(None, "--baseline-label"),
        maximum_regression: Optional[float] = typer.Option(None, "--maximum-regression", min=0),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            return (
                await service.evaluator.gate_run(
                    run_id,
                    baseline_run_id=baseline_run,
                    baseline_label=baseline_label,
                    maximum_regression=maximum_regression,
                )
            ).to_dict()

        result = _run(project, action)
        _emit(result)
        if not result["passed"]:
            raise typer.Exit(code=1)

    @learning_app.command("create")
    def learning_create(
        candidate_json: Path = typer.Option(..., "--candidate"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            payload = _load_mapping(candidate_json)
            asset = await service.learning.create_candidate(
                name=str(payload.get("name", "")),
                kind=str(payload.get("kind", "")),
                content=dict(payload.get("content", {}) or {}),
                project_id=project,
                organization_id=str(payload.get("organization_id", "") or ""),
                employee_id=str(payload.get("employee_id", "") or ""),
                role_id=str(payload.get("role_id", "") or ""),
                source_run_ids=list(payload.get("source_run_ids", []) or []),
                confidence=float(payload.get("confidence", 0.0) or 0.0),
                metadata=dict(payload.get("metadata", {}) or {}),
            )
            return asset.to_dict()

        _emit(_run(project, action))

    @learning_app.command("evaluate")
    def learning_evaluate(
        asset_id: str = typer.Argument(...),
        phase: str = typer.Option(..., "--phase"),
        score: float = typer.Option(..., "--score", min=0, max=1),
        sample_size: int = typer.Option(..., "--sample-size", min=0),
        evidence: list[str] = typer.Option([], "--evidence"),
        baseline_score: Optional[float] = typer.Option(None, "--baseline-score", min=0, max=1),
        violation: list[str] = typer.Option([], "--violation"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            value = await service.learning.record_evaluation(
                asset_id,
                phase=phase,
                score=score,
                baseline_score=baseline_score,
                sample_size=sample_size,
                evidence=evidence,
                violations=violation,
            )
            return value.to_dict()

        _emit(_run(project, action))

    @learning_app.command("advance")
    def learning_advance(
        asset_id: str = typer.Argument(...),
        phase: str = typer.Option(..., "--phase", help="shadow, canary, or promoted"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            normalized = phase.strip().lower()
            if normalized == "shadow":
                value = await service.learning.enter_shadow(asset_id)
            elif normalized == "canary":
                value = await service.learning.enter_canary(asset_id)
            elif normalized in {"promote", "promoted"}:
                value = await service.learning.promote(asset_id)
            else:
                raise ValueError("phase must be shadow, canary, or promoted")
            return value.to_dict()

        _emit(_run(project, action))

    @learning_app.command("rollback")
    def learning_rollback(
        asset_id: str = typer.Argument(...),
        reason: str = typer.Option(..., "--reason"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            rolled_back, restored = await service.learning.rollback(asset_id, reason=reason)
            return {
                "rolled_back": rolled_back.to_dict(),
                "restored": restored.to_dict() if restored else None,
            }

        _emit(_run(project, action))

    @learning_app.command("list")
    def learning_list(
        status: list[str] = typer.Option([], "--status"),
        active_only: bool = typer.Option(False, "--active-only"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> list[dict[str, Any]]:
            if active_only:
                values = await service.learning.list_active(project_id=project)
            else:
                normalized = [LearningAssetStatus(item).value for item in status]
                values = await service.repository.list_learning_assets(
                    project_id=project,
                    statuses=normalized,
                    limit=1000,
                )
            return [item.to_dict() for item in values]

        _emit(_run(project, action))

    @learning_app.command("propose-routing")
    def learning_propose_routing(
        name: str = typer.Option("outcome-routing-policy", "--name"),
        min_samples: int = typer.Option(3, "--min-samples", min=1),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Create a shadow-only routing candidate from passing measured outcomes."""

        async def action(service: OperationsService) -> dict[str, Any]:
            asset, summary = await service.routing_outcomes.propose_learning_candidate(
                project_id=project,
                name=name,
                min_samples=min_samples,
            )
            return {
                "created": asset is not None,
                "asset": asset.to_dict() if asset else None,
                "summary": summary,
            }

        _emit(_run(project, action))

    @learning_app.command("effectiveness")
    def learning_effectiveness(
        asset_id: str = typer.Argument(...),
        cohort_key: str = typer.Option(
            "",
            "--cohort-key",
            help="Optional manifest/scorecard metadata key used to match runs",
        ),
        minimum_samples: int = typer.Option(
            3,
            "--minimum-samples",
            min=1,
            help="Required treated and control samples",
        ),
        maximum_regression: float = typer.Option(
            0.02,
            "--maximum-regression",
            min=0,
            max=1,
        ),
        minimum_improvement: float = typer.Option(
            0.0,
            "--minimum-improvement",
            min=0,
            max=1,
        ),
        output: Optional[Path] = typer.Option(None, "--output"),
        fail_on_blocked: bool = typer.Option(False, "--fail-on-blocked"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Compare runs with an exact pinned learning asset to matched controls."""

        async def action(service: OperationsService) -> dict[str, Any]:
            return await service.learning_effectiveness.report(
                asset_id,
                project_id=project,
                cohort_key=cohort_key,
                policy=LearningEffectivenessPolicy(
                    minimum_samples_per_arm=minimum_samples,
                    maximum_quality_regression=maximum_regression,
                    minimum_quality_improvement=minimum_improvement,
                ),
            )

        report = _run(project, action)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)
        if fail_on_blocked and not report["promotion_evidence_eligible"]:
            raise typer.Exit(code=1)

    @learning_app.command("release-playbook-plan")
    def learning_release_playbook_plan(
        asset_id: str = typer.Argument(...),
        experiment_id: str = typer.Option(..., "--experiment-id"),
        pairs: int = typer.Option(5, "--pairs", min=3),
        output: Optional[Path] = typer.Option(None, "--output"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Seal a paired release-playbook experiment without promoting the asset."""

        async def action(service: OperationsService) -> dict[str, Any]:
            return await service.learning_effectiveness.release_playbook_plan(
                asset_id,
                project_id=project,
                experiment_id=experiment_id,
                pairs=pairs,
            )

        plan = _run(project, action)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(plan)

    @capability_app.command("plan")
    def capability_plan(
        request_json: Path = typer.Option(..., "--request"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            request = CapabilityRequest.from_dict(_load_mapping(request_json))
            if request.project_id not in {"default", project}:
                raise ValueError(
                    f"capability request project {request.project_id!r} does not match "
                    f"CLI project {project!r}"
                )
            request.project_id = project
            return (await service.capabilities.plan(request)).to_dict()

        _emit(_run(project, action, integrations=True))

    @capability_app.command("canary")
    def capability_canary(
        request_json: Path = typer.Option(..., "--request"),
        expected_model: str = typer.Option("", "--expected-model"),
        output: Optional[Path] = typer.Option(None, "--output"),
        loop: bool = typer.Option(
            False,
            "--loop",
            help="Keep sampling on an interval so readiness windows fill "
            "without a resident engine (cron/systemd friendly)",
        ),
        interval_seconds: float = typer.Option(
            300.0, "--interval-seconds", min=1.0
        ),
        iterations: int = typer.Option(
            0,
            "--iterations",
            min=0,
            help="With --loop: stop after N samples; 0 runs until interrupted",
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Run a no-generation readiness canary and persist its SLO evidence."""

        async def action(service: OperationsService) -> dict[str, Any]:
            request = CapabilityRequest.from_dict(_load_mapping(request_json))
            request.project_id = project
            request.allow_live = False
            request.require_preferred_provider = bool(
                request.preferred_providers
            )
            if not loop:
                return (
                    await service.canaries.status_canary(
                        request, expected_model=expected_model
                    )
                ).to_dict()
            return await run_status_canary_loop(
                service.canaries,
                request,
                expected_model=expected_model,
                interval_seconds=interval_seconds,
                iterations=iterations,
                on_result=lambda sample: typer.echo(
                    json.dumps(sample, ensure_ascii=False, sort_keys=True, default=str)
                ),
            )

        report = _run(project, action, integrations=True)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        _emit(report)

    @capability_app.command("slo")
    def capability_slo(
        provider: Optional[str] = typer.Option(None, "--provider"),
        limit: int = typer.Option(100, "--limit", min=1, max=5000),
        minimum_samples: int = typer.Option(3, "--minimum-samples", min=1, max=10_000),
        trend_window_samples: int = typer.Option(
            3, "--trend-window-samples", min=1, max=10_000
        ),
        availability_target: float = typer.Option(0.95, "--availability-target", min=0, max=1),
        p95_latency_target_ms: float = typer.Option(
            30_000.0, "--p95-latency-target-ms", min=1
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        _emit(
            _run(
                project,
                lambda service: service.canaries.slo_summary(
                    project_id=project,
                    provider=provider,
                    limit=limit,
                    minimum_samples=minimum_samples,
                    trend_window_samples=trend_window_samples,
                    availability_target=availability_target,
                    p95_latency_target_ms=p95_latency_target_ms,
                ),
            )
        )

    @staffing_app.command("recommend")
    def staffing_recommend(
        request_json: Path = typer.Option(..., "--request"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            payload = _load_mapping(request_json)
            decision = await service.staffing.recommend(
                role_id=str(payload.get("role_id", "")),
                candidates=[
                    StaffingCandidate.from_dict(item)
                    for item in payload.get("candidates", []) or []
                    if isinstance(item, Mapping)
                ],
                required_domains=list(payload.get("required_domains", []) or []),
                project_id=project,
                run_id=str(payload.get("run_id", "") or ""),
                max_cost_usd=(
                    None
                    if payload.get("max_cost_usd") is None
                    else float(payload["max_cost_usd"])
                ),
                metadata=dict(payload.get("metadata", {}) or {}),
            )
            return decision.to_dict()

        _emit(_run(project, action))

    @resource_app.command("run")
    def resource_pipeline_run(
        request_json: Path = typer.Option(..., "--request"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            if service.resource_pipeline is None:
                raise RuntimeError("NU resource pipeline is unavailable")
            request = ResourcePipelineRequest.from_dict(_load_mapping(request_json))
            request.project_id = project
            return (await service.resource_pipeline.run(request)).to_dict()

        _emit(_run(project, action, integrations=True))

    @resource_app.command("approve")
    def resource_pipeline_approve(
        request_json: Path = typer.Option(..., "--request"),
        operator_id: str = typer.Option(..., "--operator-id"),
        expires_in_seconds: float = typer.Option(300.0, "--expires-in-seconds", min=1, max=3600),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Issue a short-lived approval bound to the exact prompt and cost ceiling."""

        async def action(service: OperationsService) -> dict[str, Any]:
            if service.resource_pipeline is None or service.resource_pipeline.approval_issuer is None:
                raise RuntimeError(
                    "set OPENOPC_RESOURCE_APPROVAL_KEYS and its active key ID, or "
                    "OPENOPC_RESOURCE_APPROVAL_SECRET (at least 16 bytes), before issuing approvals"
                )
            request = ResourcePipelineRequest.from_dict(_load_mapping(request_json))
            request.project_id = project
            request.validate()
            token = service.resource_pipeline.approval_issuer.issue(
                project_id=project,
                candidate_id=request.candidate_id,
                prompt=request.prompt,
                max_cost_usd=request.max_cost_usd,
                operator_id=operator_id,
                expires_in_seconds=expires_in_seconds,
            )
            claims = service.resource_pipeline.approval_issuer.inspect(token)
            return {
                "approval_token": token,
                "approval_id": claims["approval_id"],
                "operator_id": claims["operator_id"],
                "key_id": claims["key_id"],
                "project_id": project,
                "candidate_id": request.candidate_id,
                "expires_in_seconds": expires_in_seconds,
            }

        _emit(_run(project, action, integrations=True))

    @staffing_app.command("observe")
    def staffing_observe(
        decision_id: str = typer.Argument(...),
        observed_score: float = typer.Option(..., "--observed-score", min=0, max=1),
        alternatives_json: Optional[Path] = typer.Option(None, "--alternatives"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            alternatives = _load_mapping(alternatives_json) if alternatives_json else {}
            decision = await service.staffing.observe(
                decision_id,
                observed_score=observed_score,
                alternative_observed_scores={str(k): float(v) for k, v in alternatives.items()},
            )
            return decision.to_dict()

        _emit(_run(project, action))

    @outbox_app.command("list")
    def outbox_list(
        status: list[str] = typer.Option([], "--status"),
        run_id: Optional[str] = typer.Option(None, "--run-id"),
        limit: int = typer.Option(100, "--limit", "-n", min=1, max=5000),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> list[dict[str, Any]]:
            values = await service.repository.list_outbox(
                statuses=status,
                run_id=run_id,
                limit=limit,
            )
            return [item.to_dict() for item in values]

        _emit(_run(project, action))

    @outbox_app.command("recover")
    def outbox_recover(
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            recovered = await service.durable.recover_expired_outbox()
            return {"recovered": recovered}

        _emit(_run(project, action))

    @outbox_app.command("replay")
    def outbox_replay(
        message_id: str = typer.Argument(...),
        reason: str = typer.Option(..., "--reason"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            message, event = await service.durable.replay_dead_letter(
                message_id,
                reason=reason,
            )
            return {"message": message.to_dict(), "audit_event": event.to_dict()}

        _emit(_run(project, action))

    @outbox_app.command("work")
    def outbox_work(
        consumer_id: str = typer.Option("openopc-audit", "--consumer-id"),
        batch_size: int = typer.Option(50, "--batch-size", min=1, max=500),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Run one independently deployable, idempotent audit-consumer cycle."""

        async def action(service: OperationsService) -> dict[str, Any]:
            worker = IndependentOutboxWorker(
                service.durable,
                audit_outbox_handler,
                consumer_id=consumer_id,
                batch_size=batch_size,
            )
            return (await worker.dispatch_once()).to_dict()

        _emit(_run(project, action))

    @mission_app.command("status")
    def mission_status(
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        _emit(
            _run(
                project,
                lambda service: service.mission_control.summary(project_id=project),
            )
        )

    @mission_app.command("brief")
    def mission_brief(
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        value = _run(
            project,
            lambda service: service.mission_control.daily_brief(project_id=project),
        )
        typer.echo(value)

    @mission_app.command("plan-action")
    def mission_plan_action(
        kind: str = typer.Argument(...),
        target_id: str = typer.Argument(...),
        reason: str = typer.Option(..., "--reason"),
        idempotency_key: str = typer.Option("", "--idempotency-key"),
        expires_in_seconds: int = typer.Option(
            300,
            "--expires-in-seconds",
            min=30,
            max=3600,
        ),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        _emit(
            _run(
                project,
                lambda service: service.operator_actions.plan(
                    project_id=project,
                    kind=kind,
                    target_id=target_id,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    expires_in_seconds=expires_in_seconds,
                ),
            )
        )

    @mission_app.command("execute-action")
    def mission_execute_action(
        action_id: str = typer.Argument(...),
        plan_digest: str = typer.Option(..., "--plan-digest"),
        operator_id: str = typer.Option(..., "--operator-id"),
        confirm: bool = typer.Option(False, "--confirm"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        _emit(
            _run(
                project,
                lambda service: service.operator_actions.execute(
                    project_id=project,
                    action_id=action_id,
                    plan_digest=plan_digest,
                    operator_id=operator_id,
                    confirmed=confirm,
                ),
            )
        )

    @mission_app.command("actions")
    def mission_actions(
        status: list[str] = typer.Option([], "--status"),
        limit: int = typer.Option(100, "--limit", "-n", min=1, max=1000),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        _emit(
            _run(
                project,
                lambda service: service.operator_actions.list(
                    project_id=project,
                    statuses=status,
                    limit=limit,
                ),
            )
        )

    @backup_app.command("create")
    def backup_create(
        destination: Path = typer.Argument(...),
        overwrite: bool = typer.Option(False, "--overwrite"),
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        async def action(service: OperationsService) -> dict[str, Any]:
            manager = OperationsBackupManager(service.repository.store.db_path)
            return await manager.create_backup(destination, overwrite=overwrite)

        _emit(_run(project, action))

    @backup_app.command("inspect")
    def backup_inspect(backup_path: Path = typer.Argument(...)) -> None:
        _emit(asyncio.run(OperationsBackupManager.inspect_backup(backup_path)))

    @backup_app.command("restore")
    def backup_restore(
        backup_path: Path = typer.Argument(...),
        destination: Path = typer.Option(..., "--destination"),
        overwrite: bool = typer.Option(False, "--overwrite"),
    ) -> None:
        """Restore to an offline destination; stop OpenOPC before replacing a live DB."""

        _emit(
            asyncio.run(
                OperationsBackupManager.restore_backup(
                    backup_path,
                    destination,
                    overwrite=overwrite,
                )
            )
        )


def _run(
    project_id: str,
    action: Callable[[OperationsService], Awaitable[T]],
    *,
    integrations: bool = False,
    run_startup_maintenance: bool = True,
) -> T:
    return asyncio.run(
        _run_async(
            project_id,
            action,
            integrations=integrations,
            run_startup_maintenance=run_startup_maintenance,
        )
    )


async def _run_async(
    project_id: str,
    action: Callable[[OperationsService], Awaitable[T]],
    *,
    integrations: bool,
    run_startup_maintenance: bool = True,
) -> T:
    project = _clean_project_id(project_id)
    opc_home = get_opc_home()
    config_dir = opc_home / "config"
    config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
    db_path = opc_home / "projects" / project / "tasks.db"
    store = OPCStore(db_path)
    await store.initialize(
        run_startup_maintenance=run_startup_maintenance
    )
    try:
        llm_router = None
        resource_bridge = None
        if integrations:
            llm_router = NULlmRoutingBridge(config.llm.nu_routing, opc_home=opc_home)
            resource_bridge = NUResourceGenBridge(
                config.system.nu_resource_gen,
                opc_home=opc_home,
                project_id=project,
            )
        default_llm_readiness = config.llm.transport_readiness()
        service = OperationsService(
            store,
            config.system.operations,
            llm_router=llm_router,
            resource_bridge=resource_bridge,
            default_llm_model=config.llm.default_model,
            default_llm_api_base=config.llm.api_base,
            default_llm_credential_ready=default_llm_readiness["credential_ready"],
            default_llm_transport_ready=default_llm_readiness["transport_ready"],
        )
        return await action(service)
    finally:
        await store.close()


def _clean_project_id(value: str) -> str:
    project = str(value or "default").strip() or "default"
    if project in {".", ".."} or Path(project).name != project or "/" in project or "\\" in project:
        raise ValueError(f"invalid project id: {project!r}")
    return project


def _benchmark_executor_config(
    *,
    task_agent: str,
    timeout_seconds: float,
    max_continuations: int,
    max_process_invocations: int,
    max_external_agent_calls: int,
) -> SubprocessExecutorConfig:
    normalized_agent = str(task_agent or "").strip().lower()
    allowed_agents = {
        "native",
        "codex",
        "claude_code",
        "cursor",
        "opencode",
    }
    if normalized_agent not in allowed_agents:
        raise ValueError(
            "task_agent must be native, codex, claude_code, cursor, or opencode"
        )
    return SubprocessExecutorConfig(
        task_args=("--mode", "task", "--agent", normalized_agent),
        timeout_seconds=timeout_seconds,
        max_continuations=max_continuations,
        max_process_invocations=max_process_invocations,
        max_external_agent_calls=max_external_agent_calls,
    )


def _served_judge_model(provider: Any, fallback: str) -> str:
    """Label drafts with the provider/model that actually served the call.

    The configured default model name is only nominal once NU routing serves
    the turn; the audit trail should record the real transport.
    """

    target = getattr(provider, "_last_route_target", None)
    if target is not None:
        provider_name = str(getattr(target, "provider", "") or "")
        model = str(getattr(target, "model", "") or "")
        label = ":".join(part for part in (provider_name, model) if part)
        if label:
            return label
    return fallback


def _load_mapping(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"expected a JSON object in {path}")
    return dict(data)


def _parse_criterion(value: str) -> AcceptanceCriterion:
    criterion_id, separator, description = str(value or "").partition("=")
    if not separator or not criterion_id.strip() or not description.strip():
        raise ValueError("criterion must use ID=DESCRIPTION format")
    return AcceptanceCriterion(
        criterion_id=criterion_id.strip(),
        description=description.strip(),
    )


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))
