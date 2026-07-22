#!/usr/bin/env python3
"""Run the complete goal → route → usage → gate → outbox → learning loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from opc.core.config import OperationsConfig
from opc.database.store import OPCStore
from opc.operations.models import (
    AcceptanceCriterion,
    CapabilityKind,
    CapabilityRequest,
    GoalContract,
    GoalContractStatus,
    RunManifest,
    RunMetrics,
    RunStatus,
)
from opc.operations.outbox import IndependentOutboxWorker, audit_outbox_handler
from opc.operations.service import OperationsService


class _GoldenAdapterRegistry:
    def list_available(self) -> list[str]:
        return ["golden-local-worker"]

    def describe_available(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_type": "golden-local-worker",
                "capabilities": ["analysis", "testing"],
                "local": True,
                "free": True,
                "health": {"credential_ready": True, "transport_ready": True},
            }
        ]


async def run_golden(root: Path, *, iterations: int = 20) -> dict[str, Any]:
    if iterations < 3:
        raise ValueError("golden E2E requires at least three iterations for learning evidence")
    root.mkdir(parents=True, exist_ok=True)
    store = OPCStore(root / "tasks.db")
    await store.initialize()
    service = OperationsService(
        store,
        OperationsConfig(),
        adapter_registry=_GoldenAdapterRegistry(),
    )
    try:
        for index in range(iterations):
            goal_id = f"golden-goal-{index:03d}"
            run_id = f"golden-run-{index:03d}"
            await service.repository.save_goal(
                GoalContract(
                    goal_id=goal_id,
                    title=f"Golden outcome {index}",
                    objective="Complete one deterministic, measured operating cycle.",
                    acceptance_criteria=[
                        AcceptanceCriterion(
                            criterion_id="verified",
                            description="The deterministic executor and evidence pass.",
                        )
                    ],
                )
            )
            await service.durable.start_run(
                RunManifest(
                    run_id=run_id,
                    goal_id=goal_id,
                    status=RunStatus.PENDING,
                    metadata={"complete_goal_on_pass": True},
                )
            )
            await service.capabilities.execute(
                CapabilityRequest(
                    request_id=f"golden-request-{index:03d}",
                    capability_kind=CapabilityKind.EXTERNAL_AGENT,
                    task_type="analysis",
                    project_id="default",
                    run_id=run_id,
                    required_capabilities=["testing"],
                    preferred_providers=["golden-local-worker"],
                    allow_live=True,
                    require_free=True,
                    max_cost_usd=0.0,
                ),
                lambda route, _request: {
                    "provider": route.provider,
                    "candidate_id": route.candidate_id,
                    "status": "completed",
                    "cost": 0.0,
                    "cost_unit": "usd",
                    "usage_accounting": {
                        "measured": True,
                        "source": "deterministic_golden",
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                        "cost_usd": 0.0,
                    },
                },
            )
            await service.durable.finish_run(run_id, status=RunStatus.COMPLETED)
            await service.evaluator.evaluate_run(
                run_id,
                criterion_scores={"verified": 1.0},
                evidence={"verified": [f"artifact://golden/{run_id}.json"]},
                metrics=RunMetrics(cost_usd=0.0, tokens=3, total_attempts=1),
                metadata={"suite": "operations_golden_e2e"},
            )
            await service.canaries.status_canary(
                CapabilityRequest(
                    request_id=f"golden-canary-{index:03d}",
                    capability_kind=CapabilityKind.EXTERNAL_AGENT,
                    task_type="analysis",
                    project_id="default",
                    preferred_providers=["golden-local-worker"],
                    required_capabilities=["testing"],
                )
            )

        worker = IndependentOutboxWorker(
            service.durable,
            audit_outbox_handler,
            consumer_id="golden-audit-v1",
            worker_id="golden-worker",
            batch_size=500,
        )
        outbox_report = await worker.dispatch_once()
        candidate, routing_summary = await service.routing_outcomes.propose_learning_candidate(
            project_id="default",
            name="golden-outcome-routing-policy",
            min_samples=iterations,
        )
        goals = await service.repository.list_goals(project_id="default", limit=5000)
        contracts = await service.repository.list_route_execution_contracts(
            project_id="default",
            limit=5000,
        )
        usage = await service.repository.list_provider_usage_events(
            project_id="default",
            limit=5000,
        )
        canaries = await service.repository.list_provider_canary_results(
            project_id="default",
            limit=5000,
        )
        scorecards = await service.repository.list_scorecards(project_id="default", limit=5000)
        mission = await service.mission_control.summary(project_id="default")
        invariants = {
            "all_goals_completed": len(goals) == iterations
            and all(item.status == GoalContractStatus.COMPLETED for item in goals),
            "all_scorecards_passed": len(scorecards) == iterations
            and all(item.accepted for item in scorecards),
            "all_contracts_completed": len(contracts) == iterations
            and all(item.status == "completed" for item in contracts),
            "all_usage_measured": len(usage) == iterations and all(item.measured for item in usage),
            "outbox_drained": outbox_report.delivered == iterations
            and outbox_report.failed == 0,
            "all_canaries_healthy": len(canaries) == iterations
            and all(item.success for item in canaries),
            "learning_candidate_is_shadow_only": candidate is not None
            and candidate.content.get("application_mode") == "shadow_only"
            and candidate.content.get("automatic_promotion") is False,
            "no_critical_alerts": not any(
                item.get("severity") == "critical" for item in mission.get("alerts", [])
            ),
        }
        report = {
            "suite": "operations_golden_e2e",
            "iterations": iterations,
            "passed": all(invariants.values()),
            "invariants": invariants,
            "counts": {
                "goals": len(goals),
                "scorecards": len(scorecards),
                "route_contracts": len(contracts),
                "usage_events": len(usage),
                "canaries": len(canaries),
                "outbox_delivered": outbox_report.delivered,
            },
            "outbox": outbox_report.to_dict(),
            "routing_summary": routing_summary,
            "learning_asset": candidate.to_dict() if candidate else None,
            "mission_control": mission,
        }
        destination = root / "operations-golden-report.json"
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        report["report_path"] = str(destination)
        return report
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.output_dir:
        report = asyncio.run(run_golden(args.output_dir, iterations=args.iterations))
    else:
        with tempfile.TemporaryDirectory(prefix="openopc-golden-") as raw_root:
            report = asyncio.run(run_golden(Path(raw_root), iterations=args.iterations))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
