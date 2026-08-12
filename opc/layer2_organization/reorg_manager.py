"""Runtime company reorganization orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Coroutine

from opc.core.models import (
    OrgSnapshot,
    ReorgChangeSet,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgMigrationPlan,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgRoleChange,
    ReorgScope,
    ReorgTaskAdjustment,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.work_item_identity import work_item_identity_payload_for_task


class ReorgManager:
    """Coordinates proposal, approval, application, and migration of runtime org changes."""

    ACTIVE_TASK_STATUSES = {
        TaskStatus.PENDING,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_PEER,
        TaskStatus.AWAITING_MANAGER_REVIEW,
        TaskStatus.AWAITING_HUMAN,
        TaskStatus.AWAITING_REVIEW,
    }

    def __init__(
        self,
        store: OPCStore,
        org_engine: OrgEngine,
        approval_engine: ApprovalEngine | None,
        communication: CommunicationManager | None,
        progress_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.store = store
        self.org_engine = org_engine
        self.approval_engine = approval_engine
        self.communication = communication
        self.progress_callback = progress_callback

    async def _emit_progress(self, message: str) -> None:
        if self.progress_callback:
            await self.progress_callback(message)

    async def build_org_snapshot(self, project_id: str) -> OrgSnapshot:
        tasks = await self.store.get_tasks(project_id=project_id)
        active_tasks = [
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "assigned_to": task.assigned_to,
                **work_item_identity_payload_for_task(task),
                "org_version": task.metadata.get("org_version", self.org_engine.current_org_version()),
                "runtime_topology_version": task.metadata.get("runtime_topology_version", self.org_engine.current_runtime_topology_version()),
            }
            for task in tasks
            if task.status in self.ACTIVE_TASK_STATUSES | {TaskStatus.RUNNING}
        ]
        return self.org_engine.snapshot_org(project_id=project_id, active_tasks=active_tasks)

    async def propose_reorg(
        self,
        *,
        project_id: str,
        summary: str,
        rationale: str = "",
        title: str = "",
        initiated_by: str = "owner",
        source_role_id: str = "",
        changeset: ReorgChangeSet | dict[str, Any] | None = None,
        scope: ReorgScope | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReorgProposal:
        if isinstance(changeset, dict):
            changeset = ReorgChangeSet(**changeset)
        changeset = self._normalize_changeset(changeset or ReorgChangeSet())
        scope = scope or self._infer_scope(changeset)
        risk_level = self._classify_risk(scope, changeset)
        snapshot = await self.build_org_snapshot(project_id)
        await self.store.save_org_snapshot(snapshot)

        migration_plan = await self._build_migration_plan(
            project_id=project_id,
            changeset=changeset,
            snapshot=snapshot,
            target_org_version=snapshot.org_version + (1 if scope == ReorgScope.ORG_MUTATION else 0),
        )

        proposal = ReorgProposal(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            scope=scope,
            risk_level=risk_level,
            status=ReorgProposalStatus.PROPOSED,
            title=title or summary[:120],
            summary=summary,
            rationale=rationale or summary,
            user_confirmation_required=(scope != ReorgScope.TASK_ADJUSTMENT or risk_level != ReorgRiskLevel.LOW),
            old_org_version=snapshot.org_version,
            new_org_version=migration_plan.metadata.get("target_org_version", snapshot.org_version),
            old_runtime_topology_version=snapshot.runtime_topology_version,
            new_runtime_topology_version=snapshot.runtime_topology_version,
            changeset=changeset,
            migration_plan=migration_plan,
            impact_summary={
                "affected_tasks": len(migration_plan.affected_task_ids),
                "affected_checkpoints": len(migration_plan.affected_checkpoint_ids),
                "role_mapping": migration_plan.role_mapping,
            },
            metadata=dict(metadata or {}),
        )
        await self.store.save_reorg_proposal(proposal)
        await self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=project_id,
                event_kind=ReorgEventKind.PROPOSED,
                summary=proposal.summary,
                details={
                    "scope": proposal.scope.value,
                    "risk_level": proposal.risk_level.value,
                    "changeset": proposal.changeset.__dict__,
                },
            )
        )
        return proposal

    async def request_reorg_approval(self, proposal_id: str) -> tuple[bool, ReorgProposal]:
        proposal = await self._require_proposal(proposal_id)
        if not proposal.user_confirmation_required:
            proposal.status = ReorgProposalStatus.APPROVED
            proposal.updated_at = datetime.now()
            await self.store.save_reorg_proposal(proposal)
            return True, proposal
        if not self.approval_engine:
            raise RuntimeError("Approval engine is unavailable")

        approval_task = Task(
            id=proposal.task_id or proposal.proposal_id,
            session_id=proposal.session_id,
            project_id=proposal.project_id,
            title=proposal.title,
            description=proposal.summary,
            assigned_to=proposal.source_role_id or "coordinator",
            metadata={
                "reorg_proposal_id": proposal.proposal_id,
                "org_version": proposal.old_org_version,
                "runtime_topology_version": proposal.old_runtime_topology_version,
            },
        )
        approved, decision = await self.approval_engine.authorize_work_item_action(
            task=approval_task,
            work_item_title=f"reorg:{proposal.title or proposal.proposal_id}",
            metadata={
                "role_id": proposal.source_role_id or "owner",
                "company_profile": self.org_engine.get_company_profile(),
                "gate_type": "company_reorg",
                "proposal_id": proposal.proposal_id,
                "scope": proposal.scope.value,
                "risk_level": proposal.risk_level.value,
            },
            on_progress=self.progress_callback,
            force_human=True,
        )
        proposal.approval_notes = decision.rationale
        proposal.status = ReorgProposalStatus.APPROVED if approved else ReorgProposalStatus.DENIED
        proposal.updated_at = datetime.now()
        await self.store.save_reorg_proposal(proposal)
        await self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPROVED if approved else ReorgEventKind.DENIED,
                summary=decision.rationale,
                details={
                    "approval_action": decision.action.value,
                    "risk_level": decision.risk_level.value,
                    "metadata": decision.metadata,
                },
            )
        )
        return approved, proposal

    async def set_reorg_approval(self, proposal_id: str, approved: bool, notes: str = "") -> ReorgProposal:
        proposal = await self._require_proposal(proposal_id)
        proposal.status = ReorgProposalStatus.APPROVED if approved else ReorgProposalStatus.DENIED
        proposal.approval_notes = notes or proposal.approval_notes
        proposal.updated_at = datetime.now()
        await self.store.save_reorg_proposal(proposal)
        await self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPROVED if approved else ReorgEventKind.DENIED,
                summary=notes or proposal.summary,
                details={"status": proposal.status.value},
            )
        )
        return proposal

    async def apply_reorg(self, proposal_id: str) -> dict[str, Any]:
        proposal = await self._require_proposal(proposal_id)
        if proposal.user_confirmation_required and proposal.status != ReorgProposalStatus.APPROVED:
            raise ValueError("Proposal must be approved before apply.")

        before_snapshot = await self.build_org_snapshot(proposal.project_id)
        await self.store.save_org_snapshot(before_snapshot)
        proposal.migration_plan.rollback_snapshot_id = before_snapshot.snapshot_id

        change_result = self.org_engine.apply_changeset(
            proposal.changeset,
            persist=True,
        ) if proposal.scope == ReorgScope.ORG_MUTATION or proposal.changeset.role_changes else {
            "old_org_version": self.org_engine.current_org_version(),
            "new_org_version": self.org_engine.current_org_version(),
            "role_mapping": {},
        }

        migration_summary = await self._migrate_active_state(proposal, change_result)
        proposal.status = ReorgProposalStatus.APPLIED
        proposal.old_org_version = change_result["old_org_version"]
        proposal.new_org_version = change_result["new_org_version"]
        proposal.migration_plan.role_mapping = dict(change_result.get("role_mapping", {}))
        proposal.migration_plan.metadata["migration_summary"] = migration_summary
        proposal.updated_at = datetime.now()
        await self.store.save_reorg_proposal(proposal)

        after_snapshot = await self.build_org_snapshot(proposal.project_id)
        await self.store.save_org_snapshot(after_snapshot)
        await self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPLIED,
                summary=proposal.summary,
                details={
                    "change_result": change_result,
                    "migration_summary": migration_summary,
                    "snapshot_id": after_snapshot.snapshot_id,
                },
            )
        )
        return {
            "proposal_id": proposal.proposal_id,
            "status": proposal.status.value,
            "migration_summary": migration_summary,
            "change_result": change_result,
            "snapshot_id": after_snapshot.snapshot_id,
        }

    async def suggest_task_adjustment(
        self,
        *,
        project_id: str,
        source_role_id: str,
        summary: str,
        changeset: ReorgChangeSet | dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        proposal = await self.propose_reorg(
            project_id=project_id,
            summary=summary,
            rationale=summary,
            initiated_by=source_role_id,
            source_role_id=source_role_id,
            changeset=changeset,
            scope=ReorgScope.TASK_ADJUSTMENT,
            session_id=session_id,
            task_id=task_id,
            metadata={"auto_apply_candidate": True},
        )
        if proposal.risk_level == ReorgRiskLevel.LOW and self._is_top_level_role(source_role_id):
            await self.set_reorg_approval(proposal.proposal_id, approved=True, notes="Auto-approved low-risk task adjustment.")
            result = await self.apply_reorg(proposal.proposal_id)
            await self.store.record_reorg_event(
                ReorgEventRecord(
                    proposal_id=proposal.proposal_id,
                    project_id=proposal.project_id,
                    event_kind=ReorgEventKind.AUTO_TASK_ADJUSTED,
                    summary=summary,
                    details=result,
                )
            )
            return {"proposal": proposal, "auto_applied": True, "result": result}
        return {"proposal": proposal, "auto_applied": False}

    async def _build_migration_plan(
        self,
        *,
        project_id: str,
        changeset: ReorgChangeSet,
        snapshot: OrgSnapshot,
        target_org_version: int,
    ) -> ReorgMigrationPlan:
        tasks = await self.store.get_tasks(project_id=project_id)
        checkpoints = await self.store.get_pending_checkpoints(project_id=project_id)
        affected_tasks = [task.id for task in tasks if task.status in self.ACTIVE_TASK_STATUSES]
        role_mapping: dict[str, str] = {}
        for change in changeset.role_changes:
            if change.action == "replace" and change.replacement_role_id:
                role_mapping[change.role_id] = change.replacement_role_id
            elif change.action == "remove":
                role_mapping[change.role_id] = ""
        warnings: list[str] = []
        if any(task.status == TaskStatus.RUNNING for task in tasks):
            warnings.append("Running tasks are not force-migrated and will continue until their current iteration completes.")
        return ReorgMigrationPlan(
            affected_task_ids=affected_tasks,
            affected_checkpoint_ids=[checkpoint.checkpoint_id for checkpoint in checkpoints],
            affected_handoff_ids=[],
            role_mapping=role_mapping,
            invalidated_waits=[],
            migration_notes=[
                f"Snapshot org_version={snapshot.org_version}.",
                f"Target org_version={target_org_version}.",
            ],
            compatibility_warnings=warnings,
            metadata={
                "target_org_version": target_org_version,
            },
        )

    async def _migrate_active_state(self, proposal: ReorgProposal, change_result: dict[str, Any]) -> dict[str, Any]:
        tasks = await self.store.get_tasks(project_id=proposal.project_id)
        checkpoints = await self.store.get_pending_checkpoints(project_id=proposal.project_id)
        migrated_task_ids: list[str] = []
        migrated_checkpoint_ids: list[str] = []
        role_mapping = dict(change_result.get("role_mapping", {}))
        target_org_version = change_result.get("new_org_version", self.org_engine.current_org_version())

        for task in tasks:
            if task.status == TaskStatus.RUNNING:
                task.metadata = dict(task.metadata)
                task.metadata["migration_status"] = "pending_running_completion"
                task.metadata["reorg_proposal_id"] = proposal.proposal_id
                await self.store.save_task(task)
                continue
            if task.status not in self.ACTIVE_TASK_STATUSES:
                continue
            task.metadata = dict(task.metadata)
            task.context_snapshot = dict(task.context_snapshot)
            current_role = task.assigned_to or str(task.metadata.get("work_item_role_id", ""))
            new_role = role_mapping.get(current_role, current_role)
            if current_role and new_role and new_role != current_role:
                task.assigned_to = new_role
                task.metadata["work_item_role_id"] = new_role
            elif current_role and new_role == "" and task.status in self.ACTIVE_TASK_STATUSES:
                task.status = TaskStatus.CANCELLED
            self._apply_task_adjustments(task, proposal.changeset)
            peer_wait = dict(task.metadata.get("peer_wait", {}))
            if peer_wait:
                waiting_on = list(peer_wait.get("waiting_on_agents", []))
                peer_wait["waiting_on_agents"] = [
                    role_mapping.get(agent_id, agent_id)
                    for agent_id in waiting_on
                    if role_mapping.get(agent_id, agent_id)
                ]
                task.metadata["peer_wait"] = peer_wait
            active_meeting = dict(task.context_snapshot.get("active_meeting", {}))
            if active_meeting:
                participants = list(active_meeting.get("participants", []))
                if participants:
                    active_meeting["participants"] = [
                        role_mapping.get(agent_id, agent_id)
                        for agent_id in participants
                        if role_mapping.get(agent_id, agent_id)
                    ]
                    task.context_snapshot["active_meeting"] = active_meeting
            task.metadata["org_version"] = target_org_version
            task.metadata["reorg_proposal_id"] = proposal.proposal_id
            task.metadata["migration_status"] = "migrated"
            task.metadata["superseded_by_reorg"] = proposal.proposal_id
            task.context_snapshot["migration_reason"] = proposal.summary
            task.context_snapshot["migration_role_mapping"] = role_mapping
            task.context_snapshot["migration_handoff"] = {
                "proposal_id": proposal.proposal_id,
                "reason": proposal.summary,
                "previous_role": current_role,
                "current_role": task.assigned_to,
            }
            await self.store.save_task(task)
            migrated_task_ids.append(task.id)

        for checkpoint in checkpoints:
            checkpoint.payload = dict(checkpoint.payload)
            checkpoint.payload["org_version"] = target_org_version
            checkpoint.payload["reorg_proposal_id"] = proposal.proposal_id
            await self.store.save_execution_checkpoint(checkpoint)
            migrated_checkpoint_ids.append(checkpoint.checkpoint_id)

        proposal.migration_plan.affected_task_ids = migrated_task_ids
        proposal.migration_plan.affected_checkpoint_ids = migrated_checkpoint_ids
        proposal.migration_plan.metadata["target_org_version"] = target_org_version
        await self._emit_progress(
            f"[Reorg] Applied proposal {proposal.proposal_id}: migrated {len(migrated_task_ids)} tasks and {len(migrated_checkpoint_ids)} checkpoints."
        )
        return {
            "migrated_task_ids": migrated_task_ids,
            "migrated_checkpoint_ids": migrated_checkpoint_ids,
            "target_org_version": target_org_version,
        }

    def _apply_task_adjustments(self, task: Task, changeset: ReorgChangeSet) -> None:
        if not changeset.task_adjustments:
            return
        for adjustment in changeset.task_adjustments:
            if adjustment.task_id and adjustment.task_id != task.id:
                continue
            if adjustment.action == "reassign" and adjustment.new_role_id:
                task.assigned_to = adjustment.new_role_id
                task.metadata["work_item_role_id"] = adjustment.new_role_id
            elif adjustment.action == "reprioritize" and adjustment.priority is not None:
                task.priority = adjustment.priority
            elif adjustment.action == "update_description" and adjustment.description_append.strip():
                addition = adjustment.description_append.strip()
                if addition not in task.description:
                    task.description = f"{task.description}\n\nAdjustment note:\n{addition}".strip()
            elif adjustment.action == "append_acceptance_criteria" and adjustment.acceptance_criteria:
                criteria = list(task.metadata.get("acceptance_criteria", []))
                for item in adjustment.acceptance_criteria:
                    if item not in criteria:
                        criteria.append(item)
                task.metadata["acceptance_criteria"] = criteria
            elif adjustment.action == "request_review":
                task.metadata["force_additional_review"] = True

    def _infer_scope(self, changeset: ReorgChangeSet) -> ReorgScope:
        if changeset.role_changes:
            return ReorgScope.ORG_MUTATION
        return ReorgScope.TASK_ADJUSTMENT

    def _classify_risk(self, scope: ReorgScope, changeset: ReorgChangeSet) -> ReorgRiskLevel:
        if scope == ReorgScope.ORG_MUTATION:
            return ReorgRiskLevel.HIGH
        for adjustment in changeset.task_adjustments:
            if adjustment.action not in {"reassign", "reprioritize", "update_description", "append_acceptance_criteria", "request_review"}:
                return ReorgRiskLevel.MEDIUM
        return ReorgRiskLevel.LOW

    def _is_top_level_role(self, role_id: str) -> bool:
        agent = self.org_engine.get_agent(role_id)
        if not agent:
            return role_id in {"owner", "coordinator"}
        return agent.reports_to == "owner"

    def _normalize_changeset(self, changeset: ReorgChangeSet) -> ReorgChangeSet:
        role_changes = [
            item if isinstance(item, ReorgRoleChange) else ReorgRoleChange(**item)
            for item in changeset.role_changes
        ]
        task_adjustments = [
            item if isinstance(item, ReorgTaskAdjustment) else ReorgTaskAdjustment(**item)
            for item in changeset.task_adjustments
        ]
        return ReorgChangeSet(
            role_changes=role_changes,
            task_adjustments=task_adjustments,
            metadata=dict(changeset.metadata),
        )

    async def _require_proposal(self, proposal_id: str) -> ReorgProposal:
        proposal = await self.store.get_reorg_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Unknown reorg proposal `{proposal_id}`.")
        return proposal
