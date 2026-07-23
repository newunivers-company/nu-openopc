"""CLI surface for the goal-to-learning operating kernel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar

import typer

from opc.core.config import OPCConfig, get_opc_home
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
from opc.operations.backup import OperationsBackupManager
from opc.operations.service import OperationsService
from opc.operations.outbox import IndependentOutboxWorker, audit_outbox_handler
from opc.operations.resource_pipeline import ResourcePipelineRequest


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
            saved, _ = await service.durable.start_run(manifest)
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
        project: str = typer.Option("default", "--project", "-p"),
    ) -> None:
        """Run a no-generation readiness canary and persist its SLO evidence."""

        async def action(service: OperationsService) -> dict[str, Any]:
            request = CapabilityRequest.from_dict(_load_mapping(request_json))
            request.project_id = project
            request.allow_live = False
            return (
                await service.canaries.status_canary(request, expected_model=expected_model)
            ).to_dict()

        _emit(_run(project, action, integrations=True))

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
) -> T:
    return asyncio.run(_run_async(project_id, action, integrations=integrations))


async def _run_async(
    project_id: str,
    action: Callable[[OperationsService], Awaitable[T]],
    *,
    integrations: bool,
) -> T:
    project = _clean_project_id(project_id)
    opc_home = get_opc_home()
    config_dir = opc_home / "config"
    config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
    db_path = opc_home / "projects" / project / "tasks.db"
    store = OPCStore(db_path)
    await store.initialize()
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
