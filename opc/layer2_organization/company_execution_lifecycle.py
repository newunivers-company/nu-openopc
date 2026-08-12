"""Lifecycle boundary around one governed Company scheduler execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from time import monotonic
from typing import Any, Protocol

from loguru import logger

from opc.core.models import Task
from opc.layer2_organization.company_policy import (
    apply_lean_company_policy,
    company_execution_telemetry,
)
from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan


class DriverOwnership(Protocol):
    """Minimum ownership contract needed by the execution lifecycle."""

    def bind(self) -> AbstractContextManager[Any]: ...

    def release(self) -> bool: ...


async def run_governed_company_execution(
    plan: CompanyWorkItemRuntimePlan,
    tasks: list[Task],
    *,
    acquire_ownership: Callable[[list[Task]], DriverOwnership | None],
    execute_scheduler: Callable[[CompanyWorkItemRuntimePlan, list[Task]], Awaitable[str]],
    store: Any | None = None,
    save_task: Callable[[Task], Awaitable[None]] | None = None,
) -> str:
    """Apply policy, hold driver ownership, and record terminal telemetry."""

    started_at = monotonic()
    status = "failed"
    ownership = acquire_ownership(tasks)
    try:
        plan.metadata = {
            **dict(plan.metadata or {}),
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
        }
        apply_lean_company_policy(plan, tasks)
        if ownership is None:
            result = await execute_scheduler(plan, tasks)
        else:
            with ownership.bind():
                result = await execute_scheduler(plan, tasks)
        status = "completed"
        return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if ownership is not None:
            ownership.release()
        await _persist_execution_telemetry(
            plan,
            tasks,
            duration_seconds=monotonic() - started_at,
            status=status,
            store=store,
            save_task=save_task,
        )


async def _persist_execution_telemetry(
    plan: CompanyWorkItemRuntimePlan,
    tasks: Sequence[Task],
    *,
    duration_seconds: float,
    status: str,
    store: Any | None,
    save_task: Callable[[Task], Awaitable[None]] | None,
) -> None:
    if not tasks or save_task is None:
        return
    telemetry = company_execution_telemetry(
        plan,
        duration_seconds=duration_seconds,
        status=status,
    )
    task = tasks[0]
    try:
        latest = (
            await store.get_task(task.id)
            if store is not None and hasattr(store, "get_task")
            else task
        )
        if latest is None:
            return
        latest.metadata = {
            **dict(latest.metadata or {}),
            "company_execution_telemetry": telemetry,
        }
        await save_task(latest)
    except Exception:
        logger.opt(exception=True).debug(
            "Best-effort Company execution telemetry persistence failed"
        )
