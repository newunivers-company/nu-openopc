from __future__ import annotations

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.models import CompanyMemberSession, Task, TaskResult, TaskStatus
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.company_runtime_identity import is_company_runtime_task
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)


def _async_test(func):
    @wraps(func)
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


def test_overlapping_attempts_remain_active_until_last_attempt_exits() -> None:
    registry = ActiveTaskRunRegistry()
    first = registry.register("project-a", "task-1")
    second = registry.register("project-a", "task-1")

    assert first != second
    assert registry.attempt_count("project-a", "task-1") == 2
    assert registry.is_active("project-a", "task-1")
    assert registry.unregister("project-a", "task-1", first)
    assert registry.is_active("project-a", "task-1")
    assert registry.unregister("project-a", "task-1", second)
    assert not registry.is_active("project-a", "task-1")


def test_registry_isolates_projects_with_equal_task_ids() -> None:
    registry = ActiveTaskRunRegistry()
    token = registry.register("project-a", "task-1")

    assert registry.active_task_ids("project-a") == {"task-1"}
    assert registry.active_task_ids("project-b") == set()
    assert not registry.is_active("project-b", "task-1")
    assert registry.unregister("project-a", "task-1", token)


def test_plain_child_task_is_not_classified_as_company_runtime_scope() -> None:
    task = Task(
        id="plain-task",
        title="Plain task",
        project_id="project-a",
        parent_session_id="parent-session",
        metadata={"mode": "task", "parent_session_id": "parent-session"},
    )

    assert not is_company_runtime_task(task)


def test_closing_admission_preserves_existing_attempts_and_rejects_new_ones() -> None:
    registry = ActiveTaskRunRegistry()
    token = registry.register("project-a", "task-1")

    registry.close_admission()

    assert registry.admission_closed
    assert registry.active_task_ids("project-a") == {"task-1"}
    assert registry.is_active("project-a", "task-1")
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "task-2")
    assert registry.unregister("project-a", "task-1", token)


def test_closed_admission_allows_only_nested_live_driver_attempts() -> None:
    registry = ActiveTaskRunRegistry()
    driver_token = registry.register("project-a", "driver-task")

    with registry.bind_driver_attempt(driver_token):
        registry.close_admission()
        nested_token = registry.register("project-a", "claimed-child")
        assert registry.is_active("project-a", "claimed-child")
        registry.unregister("project-a", "claimed-child", nested_token)

    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "new-ingress")
    registry.unregister("project-a", "driver-task", driver_token)


@_async_test
async def test_shutdown_barrier_allows_only_reserved_handoff_to_register() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()

    with registry.bind_handoff(handoff_token):
        barrier = asyncio.create_task(
            registry.close_admission_and_wait_for_handoffs()
        )
        await asyncio.sleep(0)

        assert not barrier.done()
        attempt_token = registry.register("project-a", "task-1")

    registry.release_handoff(handoff_token)
    await asyncio.wait_for(barrier, timeout=0.1)

    # The handoff wait ends at real coroutine registration, not at the end of
    # that execution attempt.
    assert registry.is_active("project-a", "task-1")
    assert registry.pending_handoff_count == 0
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "late-task")
    assert registry.unregister("project-a", "task-1", attempt_token)


@_async_test
async def test_shutdown_barrier_drains_request_that_exits_before_registration() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()
    barrier = asyncio.create_task(registry.close_admission_and_wait_for_handoffs())
    await asyncio.sleep(0)

    assert not barrier.done()
    assert registry.release_handoff(handoff_token)
    await asyncio.wait_for(barrier, timeout=0.1)

    assert registry.pending_handoff_count == 0
    assert registry.active_task_ids("project-a") == set()


@_async_test
async def test_revoked_handoff_cannot_block_shutdown_or_register_late() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()

    with registry.bind_handoff(handoff_token):
        assert registry.retain_current_handoff() == handoff_token
        registry.close_admission()
        assert registry.revoke_handoff(handoff_token)
        await asyncio.wait_for(
            registry.close_admission_and_wait_for_handoffs(),
            timeout=0.1,
        )
        with pytest.raises(ActiveTaskRunAdmissionClosed):
            registry.register("project-a", "late-task")

    assert registry.pending_handoff_count == 0
    assert not registry.release_handoff(handoff_token)


@_async_test
async def test_engine_turns_closed_admission_into_infrastructure_cancellation() -> None:
    registry = ActiveTaskRunRegistry()
    registry.close_admission()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine._run_task_once = AsyncMock()
    task = Task(
        id="late-task",
        title="Late task",
        project_id="project-a",
        status=TaskStatus.PENDING,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine._run_task_once.assert_not_awaited()
    assert registry.active_task_ids("project-a") == set()


@_async_test
async def test_task_liveness_uses_registry_only() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine.store = SimpleNamespace(get_latest_external_session_for_task=AsyncMock())
    task = Task(
        id="task-1",
        title="Live task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    assert not await engine._task_runtime_is_live(task)
    engine.store.get_latest_external_session_for_task.assert_not_awaited()

    token = registry.register("project-a", task.id)
    assert await engine._task_runtime_is_live(task)
    registry.unregister("project-a", task.id, token)


@_async_test
async def test_project_delegate_receives_controller_registry() -> None:
    registry = ActiveTaskRunRegistry()
    root = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    root._initialized = True
    captured: dict[str, object] = {}

    class FakeDelegate:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.store = None

        async def initialize(self) -> None:
            return None

    with patch("opc.engine.OPCEngine", FakeDelegate):
        delegate = await root._get_project_delegate("project-b")

    assert delegate is root._project_engine_delegates["project-b"]
    assert captured["active_task_run_registry"] is registry
    assert captured["owns_active_task_run_registry"] is False


@_async_test
async def test_shutdown_cancellation_does_not_write_business_cancelled() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine._shutting_down = True
    engine.store = SimpleNamespace(
        is_ready=True,
        get_task=AsyncMock(),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(side_effect=asyncio.CancelledError)
    task = Task(
        id="task-1",
        title="Interrupted task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.get_task.assert_not_awaited()
    engine.store.save_task.assert_not_awaited()
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_company_cancellation_never_synthesizes_hold_without_checkpoint() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.store = SimpleNamespace(
        is_ready=True,
        get_task=AsyncMock(),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(side_effect=asyncio.CancelledError)
    task = Task(
        id="company-task",
        title="Company task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.get_task.assert_not_awaited()
    engine.store.save_task.assert_not_awaited()
    assert "company_runtime_suspended_at" not in task.metadata
    assert "last_stop_reason" not in task.metadata


@_async_test
async def test_suspended_checkpoint_discards_racing_task_completion() -> None:
    engine = OPCEngine(project_id="project-a")
    engine._shutting_down = False
    task = Task(
        id="company-task",
        title="Company task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )
    suspended = Task(
        id=task.id,
        title=task.title,
        project_id=task.project_id,
        status=TaskStatus.BLOCKED,
        metadata={
            "work_item_runtime": True,
            "dispatch_hold": "company_runtime_suspended",
            "company_runtime_stop_state": "suspended",
        },
    )
    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=suspended),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.DONE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.save_task.assert_not_awaited()
    engine._apply_runtime_state_to_task.assert_not_called()


@_async_test
async def test_attempt_stays_active_until_result_persistence_finishes() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    save_started = asyncio.Event()
    allow_save = asyncio.Event()

    async def blocked_save(_task: Task) -> None:
        save_started.set()
        await allow_save.wait()

    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=None),
        save_task=blocked_save,
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.IDLE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()
    task = Task(
        id="persisting-task",
        title="Persisting task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    execution = asyncio.create_task(engine._execute_task(task))
    await save_started.wait()
    assert registry.is_active("project-a", task.id)
    allow_save.set()
    await execution
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_claimed_work_item_ownership_covers_post_execution_finalize_gap() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=None),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.IDLE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()
    task = Task(
        id="finalizing-work-item",
        title="Finalizing work item",
        project_id="project-a",
        parent_session_id="runtime-session",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )
    inner_finished = asyncio.Event()
    allow_finalize = asyncio.Event()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry

    async def run_claimed(*_args: object, **_kwargs: object) -> TaskResult:
        result = await engine._execute_task(task)
        inner_finished.set()
        await allow_finalize.wait()
        return result

    executor._run_claimed_work_item = run_claimed
    owned = executor._create_claimed_work_item_task(
        CompanyMemberSession(
            role_id="executor",
            seat_id="seat::executor",
            member_session_id="role-session",
        ),
        task,
        {},
    )
    await inner_finished.wait()

    assert registry.attempt_count("project-a", task.id) == 1
    allow_finalize.set()
    await owned
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_work_item_claim_and_spawn_share_stop_scope_lock() -> None:
    registry = ActiveTaskRunRegistry()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry
    claim_entered = asyncio.Event()
    allow_claim = asyncio.Event()
    allow_child_exit = asyncio.Event()
    stop_acquired = asyncio.Event()
    task = Task(
        id="claimed-task",
        title="Claimed task",
        project_id="project-a",
        session_id="role-session",
        parent_session_id="runtime-session",
        metadata={"work_item_runtime": True},
    )
    member_session = CompanyMemberSession(
        role_id="executor",
        seat_id="seat::executor",
        member_session_id="role-session",
    )

    async def claim_runnable_tasks(
        _tasks: list[Task],
        *,
        work_items: list[object],
    ) -> list[tuple[CompanyMemberSession, Task]]:
        del work_items
        claim_entered.set()
        await allow_claim.wait()
        return [(member_session, task)]

    async def run_claimed(*_args: object, **_kwargs: object) -> None:
        await allow_child_exit.wait()

    executor.runtime = SimpleNamespace(
        claim_runnable_tasks=claim_runnable_tasks,
    )
    executor._run_claimed_work_item = run_claimed
    active: dict[asyncio.Task, tuple[CompanyMemberSession, Task]] = {}
    scheduled = asyncio.create_task(
        executor._claim_and_create_work_item_tasks([task], [], active)
    )
    await claim_entered.wait()

    async def stop_scope() -> None:
        async with registry.scope_lock("project-a", "runtime-session"):
            assert registry.is_active("project-a", task.id)
            stop_acquired.set()

    stopping = asyncio.create_task(stop_scope())
    await asyncio.sleep(0)
    assert not stop_acquired.is_set()

    allow_claim.set()
    await scheduled
    await stopping
    assert len(active) == 1

    allow_child_exit.set()
    await asyncio.gather(*active)
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_company_executor_driver_ownership_covers_idle_scheduler_window() -> None:
    registry = ActiveTaskRunRegistry()
    entered = asyncio.Event()
    allow_exit = asyncio.Event()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry

    async def idle_scheduler(
        _plan: CompanyWorkItemRuntimePlan,
        _tasks: list[Task],
    ) -> str:
        entered.set()
        await allow_exit.wait()
        return "done"

    executor._execute_multi_team_org = idle_scheduler
    task = Task(
        id="driver-task",
        title="Driver task",
        project_id="project-a",
        parent_session_id="runtime-session",
        metadata={"work_item_runtime": True},
    )
    execution = asyncio.create_task(
        executor.execute(
            CompanyWorkItemRuntimePlan(
                projections=[
                    WorkItemProjectionSpec(
                        projection_id="driver-work-item",
                        turn_type="execute",
                        role_id="engineer",
                        title="Driver work item",
                    )
                ]
            ),
            [task],
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    assert registry.is_active("project-a", task.id)
    allow_exit.set()
    assert await execution == "done"
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_borrowed_engine_shutdown_keeps_controller_registry_open() -> None:
    registry = ActiveTaskRunRegistry()
    root_token = registry.register("project-a", "root-attempt")
    borrowed = OPCEngine(
        project_id="project-a",
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )

    await borrowed.shutdown()

    assert not registry.admission_closed
    assert registry.is_active("project-a", "root-attempt")
    next_token = registry.register("project-a", "next-attempt")
    registry.unregister("project-a", "next-attempt", next_token)
    registry.unregister("project-a", "root-attempt", root_token)


@_async_test
async def test_shutdown_preparation_includes_project_delegates() -> None:
    engine = OPCEngine(project_id="project-a")
    delegate_prepare = AsyncMock(
        return_value=[{"session_id": "delegate-session", "checkpoint_id": "checkpoint-1"}]
    )
    engine._project_engine_delegates["project-b"] = SimpleNamespace(
        prepare_active_company_runtimes_for_shutdown=delegate_prepare,
    )

    prepared = await engine.prepare_active_company_runtimes_for_shutdown()

    assert prepared == [{"session_id": "delegate-session", "checkpoint_id": "checkpoint-1"}]
    delegate_prepare.assert_awaited_once()


@_async_test
async def test_engine_shutdown_prepares_before_closing_subsystems() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(return_value=[])

    await engine.shutdown()

    engine.prepare_active_company_runtimes_for_shutdown.assert_awaited_once()


@_async_test
async def test_engine_shutdown_does_not_close_store_when_durable_prepare_fails() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(
        side_effect=RuntimeError("checkpoint failed")
    )
    engine.store = SimpleNamespace(close=AsyncMock())
    engine.message_bus.stop = MagicMock()

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await engine.shutdown()

    engine.message_bus.stop.assert_not_called()
    engine.store.close.assert_not_awaited()
