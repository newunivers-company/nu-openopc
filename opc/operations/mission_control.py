"""Secretary-facing operational portfolio, alerts, and daily brief."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

from opc.operations.canary import summarize_provider_readiness
from opc.operations.durable import DurableRunKernel
from opc.operations.models import (
    GateStatus,
    GoalContractStatus,
    LearningAssetStatus,
    MissionAlert,
    MissionControlSnapshot,
    RunStatus,
    utc_now,
)
from opc.operations.repository import OperationsRepository


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class MissionControlService:
    """Build an evidence-based operating view without invoking an LLM."""

    def __init__(
        self,
        repository: OperationsRepository,
        durable_kernel: DurableRunKernel,
        provider_config: Any | None = None,
    ) -> None:
        self.repository = repository
        self.durable_kernel = durable_kernel
        self.provider_config = provider_config

    async def snapshot(
        self,
        *,
        project_id: str = "default",
        now: datetime | None = None,
    ) -> MissionControlSnapshot:
        timestamp = now or utc_now()
        goals = await self.repository.list_goals(project_id=project_id, limit=1000)
        manifests = await self.repository.list_manifests(project_id=project_id, limit=1000)
        scorecards = await self.repository.list_scorecards(project_id=project_id, limit=1000)
        outbox = await self.repository.list_outbox(limit=5000)
        assets = await self.repository.list_learning_assets(project_id=project_id, limit=1000)
        usage_events = await self.repository.list_provider_usage_events(
            project_id=project_id,
            limit=5000,
        )
        canary_results = await self.repository.list_provider_canary_results(
            project_id=project_id,
            limit=5000,
        )
        active_goals = [item for item in goals if item.status == GoalContractStatus.ACTIVE]
        active_runs = [
            item
            for item in manifests
            if item.status in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.BLOCKED}
        ]
        blocked_runs = [item for item in active_runs if item.status == RunStatus.BLOCKED]
        failed_scorecards = [item for item in scorecards if item.gate_status == GateStatus.FAIL]
        pending_outbox = [item for item in outbox if item.status in {"pending", "processing"}]
        dead_letters = [item for item in outbox if item.status == "dead_letter"]
        learning_candidates = [
            item
            for item in assets
            if item.status
            in {
                LearningAssetStatus.CANDIDATE,
                LearningAssetStatus.EVALUATED,
                LearningAssetStatus.SHADOW,
                LearningAssetStatus.CANARY,
            }
        ]
        promoted_assets = [
            item
            for item in assets
            if item.status == LearningAssetStatus.PROMOTED
            and (item.expires_at is None or item.expires_at > timestamp)
        ]
        pending_approvals = await self._pending_approval_count(project_id)
        alerts: list[MissionAlert] = []
        unmeasured_usage_events = sum(not item.measured for item in usage_events)
        availability_target = float(
            getattr(self.provider_config, "slo_availability_target", 0.95)
        )
        latency_target = float(
            getattr(self.provider_config, "slo_p95_latency_target_ms", 30_000.0)
        )
        minimum_slo_samples = int(
            getattr(self.provider_config, "slo_min_samples", 3)
        )
        trend_window_samples = int(
            getattr(self.provider_config, "slo_trend_window_samples", 3)
        )
        provider_slo = summarize_provider_readiness(
            canary_results,
            availability_target=availability_target,
            p95_latency_target_ms=latency_target,
            minimum_samples=minimum_slo_samples,
            trend_window_samples=trend_window_samples,
            minimum_observation_seconds=int(
                getattr(
                    self.provider_config,
                    "readiness_min_observation_seconds",
                    86_400,
                )
            ),
            time_bucket_seconds=int(
                getattr(
                    self.provider_config,
                    "readiness_time_bucket_seconds",
                    21_600,
                )
            ),
            minimum_time_buckets=int(
                getattr(
                    self.provider_config,
                    "readiness_min_time_buckets",
                    4,
                )
            ),
            minimum_samples_per_bucket=int(
                getattr(
                    self.provider_config,
                    "readiness_min_samples_per_bucket",
                    1,
                )
            ),
            maximum_sample_age_seconds=int(
                getattr(
                    self.provider_config,
                    "readiness_max_sample_age_seconds",
                    1_800,
                )
            ),
            maximum_gap_seconds=int(
                getattr(
                    self.provider_config,
                    "readiness_max_gap_seconds",
                    28_800,
                )
            ),
            failure_drill_max_age_seconds=int(
                getattr(
                    self.provider_config,
                    "readiness_failure_drill_max_age_seconds",
                    2_592_000,
                )
            ),
            required_failure_scenarios=list(
                getattr(
                    self.provider_config,
                    "readiness_required_failure_scenarios",
                    [
                        "credential_expiry",
                        "transport_timeout",
                        "quota_exhaustion",
                        "model_drift",
                    ],
                )
                or []
            ),
            now=timestamp,
        )
        quota_limit = int(
            getattr(self.provider_config, "subscription_call_limit", 0)
        )
        quota_window = int(
            getattr(self.provider_config, "subscription_window_seconds", 86_400)
        )
        quota_providers = list(
            getattr(self.provider_config, "subscription_providers", []) or []
        )
        observed_quota_providers = (
            await self.repository.list_provider_call_quota_providers(
                project_id=project_id
            )
        )
        quota_provider_names = list(observed_quota_providers)
        quota_provider_names.extend(
            str(provider)
            for provider in quota_providers
            if str(provider).strip()
            and not any(
                _provider_family_match(observed, str(provider))
                for observed in observed_quota_providers
            )
        )
        provider_call_quotas = {
            str(provider): await self.repository.provider_call_quota_status(
                project_id=project_id,
                provider=str(provider),
                limit=quota_limit,
                window_seconds=quota_window,
                now=timestamp,
            )
            for provider in quota_provider_names
            if str(provider).strip()
        }

        for run in active_runs:
            deadlock = await self.durable_kernel.detect_deadlock(run.run_id, now=timestamp)
            if deadlock.deadlocked:
                alerts.append(
                    MissionAlert(
                        severity="critical",
                        kind="deadlock",
                        title=f"Run {run.run_id} has stopped making progress",
                        detail=(
                            f"No durable activity for {deadlock.inactivity_seconds:.0f}s "
                            f"(threshold {deadlock.threshold_seconds:.0f}s)."
                        ),
                        run_id=run.run_id,
                        goal_id=run.goal_id,
                        action=f"Run recovery for {run.run_id} and inspect its last event.",
                        action_kind="recover_run",
                        action_target_id=run.run_id,
                    )
                )
            elif run.status == RunStatus.BLOCKED:
                alerts.append(
                    MissionAlert(
                        severity="high",
                        kind="blocked_run",
                        title=f"Run {run.run_id} is blocked",
                        detail=str(run.metadata.get("blocked_reason", "") or "No blocker detail was recorded."),
                        run_id=run.run_id,
                        goal_id=run.goal_id,
                        action="Resolve the blocker or cancel the run explicitly.",
                    )
                )

        for scorecard in failed_scorecards:
            alerts.append(
                MissionAlert(
                    severity="high",
                    kind="failed_gate",
                    title=f"Outcome gate failed for run {scorecard.run_id}",
                    detail="; ".join(scorecard.violations[:3]) or "Scorecard did not pass.",
                    run_id=scorecard.run_id,
                    goal_id=scorecard.goal_id,
                    action="Inspect evidence and violations before retrying or promoting outputs.",
                )
            )

        scorecard_run_ids = {item.run_id for item in scorecards}
        judgment_candidates = sorted(
            (
                run
                for run in manifests
                if run.status == RunStatus.COMPLETED and run.run_id not in scorecard_run_ids
            ),
            key=lambda run: run.completed_at or run.updated_at,
            reverse=True,
        )
        judgment_queue = [
            {
                "run_id": run.run_id,
                "goal_id": run.goal_id,
                "completed_at": run.completed_at.isoformat() if run.completed_at else "",
                "benchmark_slot_id": str(run.metadata.get("benchmark_slot_id", "") or ""),
            }
            for run in judgment_candidates[:20]
        ]
        for run in manifests:
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED} and run.run_id not in scorecard_run_ids:
                alerts.append(
                    MissionAlert(
                        severity="high",
                        kind="missing_scorecard",
                        title=f"Terminal run {run.run_id} has no scorecard",
                        detail="The run ended without a persisted outcome evaluation.",
                        run_id=run.run_id,
                        goal_id=run.goal_id,
                        action=f"Evaluate run {run.run_id} before accepting its deliverables.",
                    )
                )

        for goal in active_goals:
            goal_runs = [item for item in manifests if item.goal_id == goal.goal_id]
            if not goal_runs:
                alerts.append(
                    MissionAlert(
                        severity="medium",
                        kind="unstarted_goal",
                        title=f"Goal {goal.goal_id} has no run",
                        detail=goal.title,
                        goal_id=goal.goal_id,
                        action="Create a run manifest and assign an execution owner.",
                    )
                )
            if goal.deadline:
                remaining = goal.deadline - timestamp
                if remaining.total_seconds() < 0:
                    alerts.append(
                        MissionAlert(
                            severity="critical",
                            kind="overdue_goal",
                            title=f"Goal {goal.goal_id} is overdue",
                            detail=f"Deadline passed {-remaining.total_seconds() / 3600:.1f}h ago.",
                            goal_id=goal.goal_id,
                            action="Re-plan, escalate, or close the goal with an explicit decision.",
                        )
                    )
                elif remaining <= timedelta(hours=24):
                    alerts.append(
                        MissionAlert(
                            severity="high",
                            kind="deadline_risk",
                            title=f"Goal {goal.goal_id} is due soon",
                            detail=f"{remaining.total_seconds() / 3600:.1f}h remain.",
                            goal_id=goal.goal_id,
                            action="Confirm the critical path and remove non-goal work.",
                        )
                    )

        if dead_letters:
            alerts.append(
                MissionAlert(
                    severity="critical",
                    kind="dead_letter",
                    title=f"{len(dead_letters)} outbox message(s) exhausted retries",
                    detail="At-least-once delivery stopped after the configured attempt limit.",
                    action=(
                        "Inspect errors, repair the consumer, then replay the oldest "
                        "dead letter intentionally."
                    ),
                    action_kind="replay_dead_letter",
                    action_target_id=dead_letters[0].message_id,
                )
            )
        if pending_approvals:
            alerts.append(
                MissionAlert(
                    severity="high",
                    kind="pending_approval",
                    title=f"{pending_approvals} approval/review checkpoint(s) need attention",
                    detail="Execution is waiting for a human decision.",
                    action="Open the pending approval cards and decide or reject them.",
                )
            )
        if pending_outbox and not dead_letters:
            oldest = min(item.created_at for item in pending_outbox)
            age = max(0.0, (timestamp - oldest).total_seconds())
            if age >= self.durable_kernel.config.deadlock_after_seconds:
                alerts.append(
                    MissionAlert(
                        severity="medium",
                        kind="outbox_backlog",
                        title=f"{len(pending_outbox)} outbox message(s) are pending",
                        detail=f"The oldest has waited {age:.0f}s.",
                        action="Check dispatcher health and expired delivery leases.",
                    )
                )

        if unmeasured_usage_events:
            alerts.append(
                MissionAlert(
                    severity="medium",
                    kind="unmeasured_provider_usage",
                    title=f"{unmeasured_usage_events} provider call(s) have unmeasured usage",
                    detail="Missing token or cost telemetry remains unknown rather than being counted as zero.",
                    action="Enable provider usage reporting or keep explicit call-count quota guards.",
                )
            )
        for provider, quota in provider_call_quotas.items():
            if not quota["enabled"] or quota["limit"] <= 0:
                continue
            utilization = quota["used"] / quota["limit"]
            if utilization >= 0.8:
                exhausted = not quota["allowed"]
                alerts.append(
                    MissionAlert(
                        severity="critical" if exhausted else "high",
                        kind="subscription_call_quota",
                        title=(
                            f"Provider {provider} subscription call quota "
                            f"{'is exhausted' if exhausted else 'is nearing its limit'}"
                        ),
                        detail=(
                            f"{quota['used']}/{quota['limit']} calls used in the rolling "
                            f"{quota['window_seconds']}s window."
                        ),
                        action=(
                            f"Route new calls away from {provider} or wait for quota recovery."
                        ),
                    )
                )
        for provider, slo in provider_slo.items():
            if slo["attainment_state"] == "missed":
                alerts.append(
                    MissionAlert(
                        severity="high",
                        kind="provider_slo",
                        title=f"Provider {provider} is outside its SLO",
                        detail=(
                            f"Availability {slo['availability']:.1%} "
                            f"(target {availability_target:.1%}) over {slo['samples']} canaries; "
                            f"p95 {slo['p95_latency_ms']:.1f}ms "
                            f"(target {latency_target:.1f}ms)."
                        ),
                        action=f"Demote {provider} from primary routing until its canary recovers.",
                    )
                )
            elif not slo["production_ready"]:
                blockers = list(slo.get("blockers", []) or [])
                alerts.append(
                    MissionAlert(
                        severity="medium",
                        kind="provider_readiness",
                        title=f"Provider {provider} still needs promotion evidence",
                        detail=(
                            "; ".join(str(item) for item in blockers[:3])
                            or "Production-readiness evidence is incomplete."
                        ),
                        action=(
                            f"Continue the governed readiness campaign for {provider}; "
                            "collect fresh status canaries and verified failure drills."
                        ),
                    )
                )

        for asset in assets:
            if asset.status == LearningAssetStatus.PROMOTED and asset.expires_at and asset.expires_at <= timestamp:
                alerts.append(
                    MissionAlert(
                        severity="medium",
                        kind="expired_learning_asset",
                        title=f"Promoted learning asset {asset.name} has expired",
                        detail=f"Asset {asset.asset_id} v{asset.version} should no longer be injected.",
                        action="Retire it or create and evaluate a replacement candidate.",
                        action_kind="retire_learning_asset",
                        action_target_id=asset.asset_id,
                    )
                )

        alerts.sort(
            key=lambda item: (
                _SEVERITY_ORDER.get(item.severity, 99),
                item.kind,
                item.run_id,
                item.goal_id,
            )
        )
        recommendations = _recommendations(
            alerts,
            learning_candidate_count=len(learning_candidates),
            pending_outbox_count=len(pending_outbox),
        )
        return MissionControlSnapshot(
            project_id=project_id,
            active_goals=len(active_goals),
            active_runs=len(active_runs),
            blocked_runs=len(blocked_runs),
            failed_gates=len(failed_scorecards),
            pending_outbox=len(pending_outbox),
            dead_letters=len(dead_letters),
            pending_approvals=pending_approvals,
            learning_candidates=len(learning_candidates),
            promoted_assets=len(promoted_assets),
            average_score=(fmean(item.total_score for item in scorecards) if scorecards else 0.0),
            total_cost_usd=sum(item.metrics.cost_usd for item in scorecards),
            unmeasured_usage_events=unmeasured_usage_events,
            provider_slo=provider_slo,
            provider_call_quotas=provider_call_quotas,
            judgment_queue=judgment_queue,
            alerts=alerts,
            recommendations=recommendations,
            generated_at=timestamp,
        )

    async def summary(
        self,
        *,
        project_id: str = "default",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return (await self.snapshot(project_id=project_id, now=now)).to_dict()

    async def daily_brief(
        self,
        *,
        project_id: str = "default",
        now: datetime | None = None,
    ) -> str:
        snapshot = await self.snapshot(project_id=project_id, now=now)
        lines = [
            f"Mission Control — {snapshot.project_id}",
            (
                f"Goals {snapshot.active_goals} active · Runs {snapshot.active_runs} active / "
                f"{snapshot.blocked_runs} blocked · Gates {snapshot.failed_gates} failed"
            ),
            (
                f"Outbox {snapshot.pending_outbox} pending / {snapshot.dead_letters} dead-letter · "
                f"Approvals {snapshot.pending_approvals} pending"
            ),
            (
                f"Learning {snapshot.learning_candidates} candidates / "
                f"{snapshot.promoted_assets} promoted · Average score {snapshot.average_score:.3f} · "
                f"Tracked cost ${snapshot.total_cost_usd:.4f}"
            ),
            (
                f"Provider telemetry {len(snapshot.provider_slo)} tracked / "
                f"{snapshot.unmeasured_usage_events} unmeasured usage event(s) / "
                f"{len(snapshot.provider_call_quotas)} call quota(s)"
            ),
        ]
        if snapshot.alerts:
            lines.append("Alerts:")
            lines.extend(
                f"- [{item.severity.upper()}] {item.title}: {item.detail}"
                for item in snapshot.alerts[:10]
            )
        else:
            lines.append("Alerts: none")
        if snapshot.recommendations:
            lines.append("Recommended next actions:")
            lines.extend(f"- {item}" for item in snapshot.recommendations)
        return "\n".join(lines)

    async def _pending_approval_count(self, project_id: str) -> int:
        async with self.repository.db.execute(
            """SELECT COUNT(*) FROM execution_checkpoints
               WHERE project_id = ? AND status = 'pending'""",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] if row else 0)


def _recommendations(
    alerts: list[MissionAlert],
    *,
    learning_candidate_count: int,
    pending_outbox_count: int,
) -> list[str]:
    recommendations = [item.action for item in alerts if item.action]
    if learning_candidate_count:
        recommendations.append(
            f"Advance or reject {learning_candidate_count} learning candidate(s) using offline, shadow, and canary evidence."
        )
    if pending_outbox_count and not any(item.kind in {"dead_letter", "outbox_backlog"} for item in alerts):
        recommendations.append("Keep the outbox dispatcher running until the pending delivery queue drains.")
    return list(dict.fromkeys(recommendations))[:12]


def _provider_family_match(provider: str, family: str) -> bool:
    normalized = str(provider or "").strip().lower()
    prefix = str(family or "").strip().lower()
    return bool(
        normalized == prefix
        or normalized.startswith(f"{prefix}-")
        or normalized.startswith(f"{prefix}_")
    )
