"""Audited two-phase operator actions for Mission Control."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any

from opc.operations.durable import DurableRunKernel
from opc.operations.learning import LearningAssetManager
from opc.operations.models import (
    LearningAssetStatus,
    OperatorAction,
    RunStatus,
    utc_now,
)
from opc.operations.repository import OperationsRepository


ALLOWED_OPERATOR_ACTIONS = frozenset(
    {
        "recover_run",
        "replay_dead_letter",
        "rollback_learning_asset",
        "retire_learning_asset",
    }
)

_CONSEQUENCES = {
    "recover_run": (
        "Acquires a fenced run lease, recovers expired delivery claims, and records "
        "a deadlock event when the run is still stalled."
    ),
    "replay_dead_letter": (
        "Resets one exhausted outbox message to pending so its consumer may receive "
        "it again under at-least-once delivery."
    ),
    "rollback_learning_asset": (
        "Stops future runs from pinning this promoted asset and restores its prior "
        "version when one exists; already-running manifests remain unchanged."
    ),
    "retire_learning_asset": (
        "Retires one promoted asset only after its recorded expiry time has passed; "
        "already-running manifests remain unchanged."
    ),
}


def _plan_payload(action: OperatorAction) -> dict[str, Any]:
    return {
        "schema_version": action.schema_version,
        "action_id": action.action_id,
        "project_id": action.project_id,
        "kind": action.kind,
        "target_id": action.target_id,
        "parameters": dict(action.parameters),
        "created_at": action.created_at.isoformat(),
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
    }


def _plan_digest(action: OperatorAction) -> str:
    encoded = json.dumps(
        _plan_payload(action),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class OperatorActionService:
    """Plan and execute a narrow allowlist of project-scoped operator actions."""

    def __init__(
        self,
        repository: OperationsRepository,
        durable: DurableRunKernel,
        learning: LearningAssetManager,
    ) -> None:
        self.repository = repository
        self.durable = durable
        self.learning = learning

    async def plan(
        self,
        *,
        project_id: str,
        kind: str,
        target_id: str,
        reason: str,
        idempotency_key: str = "",
        expires_in_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_kind = str(kind or "").strip()
        normalized_target = str(target_id or "").strip()
        normalized_reason = str(reason or "").strip()
        normalized_project = str(project_id or "default").strip() or "default"
        if normalized_kind not in ALLOWED_OPERATOR_ACTIONS:
            raise ValueError(f"unsupported operator action: {normalized_kind!r}")
        if not normalized_target or not normalized_reason:
            raise ValueError("operator action target_id and reason are required")
        self.repository.assert_project(normalized_project)
        await self._validate_target(
            project_id=normalized_project,
            kind=normalized_kind,
            target_id=normalized_target,
            now=now or utc_now(),
        )
        normalized_idempotency = str(idempotency_key or "").strip()
        if normalized_idempotency:
            existing = await self.repository.get_operator_action_by_idempotency_key(
                normalized_idempotency
            )
            if existing is not None:
                if (
                    existing.project_id,
                    existing.kind,
                    existing.target_id,
                    existing.parameters.get("reason"),
                ) != (
                    normalized_project,
                    normalized_kind,
                    normalized_target,
                    normalized_reason,
                ):
                    raise ValueError(
                        "operator action idempotency key was already used for another plan"
                    )
                return self.describe(existing)

        timestamp = now or utc_now()
        action = OperatorAction(
            project_id=normalized_project,
            kind=normalized_kind,
            target_id=normalized_target,
            plan_digest="0" * 64,
            idempotency_key=normalized_idempotency,
            parameters={"reason": normalized_reason},
            created_at=timestamp,
            expires_at=timestamp
            + timedelta(seconds=max(30, min(int(expires_in_seconds), 3600))),
        )
        action.plan_digest = _plan_digest(action)
        await self.repository.save_operator_action(action)
        return self.describe(action)

    async def execute(
        self,
        *,
        project_id: str,
        action_id: str,
        plan_digest: str,
        operator_id: str,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise PermissionError("operator action requires explicit confirmation")
        normalized_operator = str(operator_id or "").strip()
        if not normalized_operator:
            raise ValueError("operator_id is required")
        action = await self.repository.get_operator_action(str(action_id or "").strip())
        if action is None:
            raise KeyError(f"operator action not found: {action_id}")
        normalized_project = str(project_id or "default").strip() or "default"
        self.repository.assert_project(normalized_project)
        if action.project_id != normalized_project:
            raise PermissionError("operator action belongs to another project")
        expected = _plan_digest(action)
        if not hmac.compare_digest(action.plan_digest, expected):
            raise ValueError("persisted operator action plan digest is invalid")
        if not hmac.compare_digest(action.plan_digest, str(plan_digest or "").strip()):
            raise PermissionError("operator action confirmation digest does not match")
        if action.status == "executed":
            return self.describe(action)
        if action.status != "planned":
            raise RuntimeError(
                f"operator action {action.action_id} cannot execute from {action.status}"
            )
        timestamp = now or utc_now()
        if action.expires_at is not None and action.expires_at <= timestamp:
            action.status = "expired"
            await self.repository.save_operator_action(action)
            raise PermissionError("operator action plan has expired")

        await self._validate_target(
            project_id=action.project_id,
            kind=action.kind,
            target_id=action.target_id,
            now=timestamp,
        )
        action.status = "executing"
        action.operator_id = normalized_operator
        await self.repository.save_operator_action(action)
        try:
            action.result = await self._dispatch(action, now=timestamp)
            action.status = "executed"
            action.executed_at = timestamp
            await self.repository.save_operator_action(action)
        except Exception as exc:
            action.status = "failed"
            action.result = {"error_type": type(exc).__name__, "error": str(exc)}
            action.executed_at = timestamp
            await self.repository.save_operator_action(action)
            raise
        return self.describe(action)

    async def list(
        self,
        *,
        project_id: str,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.repository.assert_project(project_id)
        actions = await self.repository.list_operator_actions(
            project_id=project_id,
            statuses=statuses,
            limit=limit,
        )
        return [self.describe(item) for item in actions]

    @staticmethod
    def describe(action: OperatorAction) -> dict[str, Any]:
        return {
            **action.to_dict(),
            "requires_confirmation": action.status == "planned",
            "consequence": _CONSEQUENCES[action.kind],
            "plan": _plan_payload(action),
        }

    async def _validate_target(
        self,
        *,
        project_id: str,
        kind: str,
        target_id: str,
        now: datetime,
    ) -> None:
        if kind == "recover_run":
            manifest = await self.repository.get_manifest(target_id)
            if manifest is None:
                raise KeyError(f"run manifest not found: {target_id}")
            if manifest.project_id != project_id:
                raise PermissionError("run belongs to another project")
            if manifest.status not in {
                RunStatus.PENDING,
                RunStatus.RUNNING,
                RunStatus.BLOCKED,
            }:
                raise ValueError("only active runs can be recovered")
            return
        if kind == "replay_dead_letter":
            message = await self.repository.get_outbox_message(target_id)
            if message is None:
                raise KeyError(f"outbox message not found: {target_id}")
            manifest = await self.repository.get_manifest(message.run_id)
            if manifest is None or manifest.project_id != project_id:
                raise PermissionError("outbox message belongs to another project")
            if message.status != "dead_letter":
                raise ValueError("only dead-letter outbox messages can be replayed")
            return
        asset = await self.repository.get_learning_asset(target_id)
        if asset is None:
            raise KeyError(f"learning asset not found: {target_id}")
        if asset.project_id != project_id:
            raise PermissionError("learning asset belongs to another project")
        if asset.status != LearningAssetStatus.PROMOTED:
            raise ValueError("operator learning action requires a promoted asset")
        if kind == "retire_learning_asset" and (
            asset.expires_at is None or asset.expires_at > now
        ):
            raise ValueError("learning asset cannot be retired before expiry")

    async def _dispatch(
        self,
        action: OperatorAction,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        reason = str(action.parameters["reason"])
        if action.kind == "recover_run":
            report = await self.durable.recover_run(
                action.target_id,
                owner=f"operator-action:{action.action_id}",
                now=now,
            )
            return report.to_dict()
        if action.kind == "replay_dead_letter":
            message, event = await self.durable.replay_dead_letter(
                action.target_id,
                reason=reason,
                now=now,
            )
            return {"message": message.to_dict(), "audit_event": event.to_dict()}
        if action.kind == "rollback_learning_asset":
            rolled_back, restored = await self.learning.rollback(
                action.target_id,
                reason=reason,
            )
            return {
                "rolled_back": rolled_back.to_dict(),
                "restored": restored.to_dict() if restored else None,
            }
        if action.kind == "retire_learning_asset":
            asset = await self.repository.get_learning_asset(action.target_id)
            assert asset is not None
            asset.status = LearningAssetStatus.RETIRED
            asset.metadata = {
                **dict(asset.metadata),
                "retired_reason": "expired",
                "retired_at": now.isoformat(),
                "operator_action_id": action.action_id,
            }
            await self.repository.save_learning_asset(asset)
            return {"retired": asset.to_dict()}
        raise AssertionError(f"unreachable operator action: {action.kind}")
