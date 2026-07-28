"""Slot execution harness for the Task-vs-Company outcome benchmark campaign.

Turns a campaign-plan slot into an observable governed run without manual
plumbing: goal registration, run start, execution through an injected
executor, durable artifact capture with SHA-256 digests, run finish, and a
scoring skeleton for the judgment step. Judgment itself stays human — the
harness never fabricates criterion scores, authority labels, or trusted
observations, so the ``actual_run`` + human-authority gate contract in
:mod:`opc.operations.benchmarks` is preserved end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opc.operations.benchmarks import _canonical_digest
from opc.operations.models import (
    GoalContract,
    RunManifest,
    RunStatus,
    utc_now,
)

ARTIFACT_INDEX_NAME = "artifact-index.json"
RESULT_SKELETON_NAME = "result-skeleton.json"
OUTPUT_ARTIFACT_NAME = "output.md"

SlotExecutor = Callable[[Mapping[str, Any], Path], Awaitable["SlotExecution"]]


@dataclass
class SlotExecution:
    """What an executor produced for one slot."""

    success: bool
    output_text: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    model_versions: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlotRunResult:
    slot_id: str
    run_id: str
    status: str  # completed | failed | skipped
    artifact_digest: str = ""
    artifact_directory: str = ""
    result_skeleton_path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "run_id": self.run_id,
            "status": self.status,
            "artifact_digest": self.artifact_digest,
            "artifact_directory": self.artifact_directory,
            "result_skeleton_path": self.result_skeleton_path,
            "error": self.error,
        }


def verify_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed if a campaign plan was edited after it was sealed."""

    payload = dict(plan)
    claimed = str(payload.pop("plan_digest", "") or "")
    recomputed = _canonical_digest(payload)
    if claimed != recomputed:
        raise ValueError(
            "campaign plan digest mismatch: the plan was modified after sealing"
        )
    return dict(plan)


def find_slot(plan: Mapping[str, Any], slot_id: str) -> dict[str, Any]:
    for slot in plan.get("slots", []) or []:
        if slot.get("slot_id") == slot_id:
            return dict(slot)
    raise KeyError(f"campaign plan has no slot: {slot_id}")


class CampaignSlotRunner:
    """Run one plan slot through the governed goal → run → artifact loop."""

    def __init__(
        self,
        service: Any,
        executor: SlotExecutor,
        *,
        project_id: str,
        artifacts_root: Path,
    ) -> None:
        self.service = service
        self.executor = executor
        self.project_id = str(project_id or "default")
        self.artifacts_root = Path(artifacts_root)

    async def run_slot(
        self,
        plan: Mapping[str, Any],
        slot_id: str,
        *,
        force: bool = False,
    ) -> SlotRunResult:
        plan = verify_plan(plan)
        slot = find_slot(plan, slot_id)
        run_id = str(slot["run_id"])
        artifact_dir = self.artifacts_root / str(slot["artifact_directory"])

        existing = await self.service.repository.get_manifest(run_id)
        if existing is not None and not force:
            return SlotRunResult(
                slot_id=slot_id,
                run_id=run_id,
                status="skipped",
                artifact_directory=str(artifact_dir),
                error=f"run manifest already exists with status {existing.status.value}",
            )

        await self._ensure_goal(slot)
        manifest = RunManifest(
            run_id=run_id,
            goal_id=str(slot["goal"]["goal_id"]),
            project_id=self.project_id,
            status=RunStatus.RUNNING,
            started_at=utc_now(),
            configuration_digest=str(plan.get("plan_digest", "") or ""),
            metadata={
                "benchmark_campaign_id": plan.get("campaign_id", ""),
                "benchmark_slot_id": slot_id,
                "benchmark_case_id": slot.get("case_id", ""),
                "benchmark_mode": slot.get("mode", ""),
                "benchmark_repetition": slot.get("repetition", 0),
                "benchmark_suite_digest": plan.get("suite_digest", ""),
            },
        )
        if existing is None:
            await self.service.start_run(manifest)

        artifact_dir.mkdir(parents=True, exist_ok=True)
        started = utc_now()
        try:
            execution = await self.executor(slot, artifact_dir)
        except Exception as exc:  # noqa: BLE001 - failures must settle the run
            await self.service.durable.finish_run(run_id, status=RunStatus.FAILED)
            return SlotRunResult(
                slot_id=slot_id,
                run_id=run_id,
                status="failed",
                artifact_directory=str(artifact_dir),
                error=f"{type(exc).__name__}: {exc}",
            )
        duration = (utc_now() - started).total_seconds()

        if execution.output_text:
            (artifact_dir / OUTPUT_ARTIFACT_NAME).write_text(
                execution.output_text, encoding="utf-8"
            )
        for name, content in sorted(execution.artifacts.items()):
            target = artifact_dir / name
            if not str(target.resolve()).startswith(str(artifact_dir.resolve())):
                raise ValueError(f"artifact escapes its slot directory: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        digest = write_artifact_index(artifact_dir)
        skeleton_path = write_result_skeleton(
            artifact_dir,
            slot=slot,
            run_id=run_id,
            artifact_digest=digest,
            duration_seconds=duration,
            execution=execution,
        )

        final = RunStatus.COMPLETED if execution.success else RunStatus.FAILED
        await self.service.durable.finish_run(run_id, status=final)
        return SlotRunResult(
            slot_id=slot_id,
            run_id=run_id,
            status=final.value,
            artifact_digest=digest,
            artifact_directory=str(artifact_dir),
            result_skeleton_path=str(skeleton_path),
            error="" if execution.success else "executor reported failure",
        )

    async def _ensure_goal(self, slot: Mapping[str, Any]) -> None:
        goal_payload = dict(slot["goal"])
        goal_payload["project_id"] = self.project_id
        goal = GoalContract.from_dict(goal_payload)
        existing = await self.service.repository.get_goal(goal.goal_id)
        if existing is None:
            await self.service.repository.save_goal(goal)


@dataclass
class CampaignBudget:
    """Campaign-level execution ceilings enforced by the batch runner.

    ``max_cost_usd`` only counts *measured* provider usage; subscription CLI
    calls report no cost and are governed by the rolling call-count quota
    instead — the ceiling is a hard stop on what can be measured, never a
    claim that unmeasured usage was free.
    """

    max_slots: int | None = None
    max_failures: int = 3
    max_cost_usd: float | None = None

    def validate(self) -> None:
        if self.max_slots is not None and self.max_slots < 1:
            raise ValueError("max_slots must be positive when set")
        if self.max_failures < 0:
            raise ValueError("max_failures must be non-negative")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive when set")


class CampaignRunner:
    """Resumable batch driver over a sealed campaign plan."""

    def __init__(self, slot_runner: CampaignSlotRunner) -> None:
        self.slot_runner = slot_runner

    async def run_campaign(
        self,
        plan: Mapping[str, Any],
        *,
        budget: CampaignBudget | None = None,
        workloads: Iterable[str] | None = None,
        modes: Iterable[str] | None = None,
        stop_on_failure: bool = False,
    ) -> dict[str, Any]:
        plan = verify_plan(plan)
        budget = budget or CampaignBudget()
        budget.validate()
        workload_filter = {str(item) for item in workloads} if workloads else None
        mode_filter = {str(item) for item in modes} if modes else None

        results: list[SlotRunResult] = []
        executed = failed = skipped = 0
        measured_cost = 0.0
        halted_reason = ""
        slots: Sequence[Mapping[str, Any]] = sorted(
            plan.get("slots", []) or [], key=lambda item: int(item.get("sequence", 0))
        )
        for slot in slots:
            if workload_filter and slot.get("workload") not in workload_filter:
                continue
            if mode_filter and slot.get("mode") not in mode_filter:
                continue
            if budget.max_slots is not None and executed >= budget.max_slots:
                halted_reason = "max_slots budget reached"
                break
            result = await self.slot_runner.run_slot(plan, str(slot["slot_id"]))
            results.append(result)
            if result.status == "skipped":
                skipped += 1
                continue
            executed += 1
            if budget.max_cost_usd is not None:
                measured_cost += await self._measured_run_cost(result.run_id)
                if measured_cost >= budget.max_cost_usd:
                    halted_reason = "max_cost_usd budget reached"
                    if result.status == "failed":
                        failed += 1
                    break
            if result.status == "failed":
                failed += 1
                if stop_on_failure:
                    halted_reason = "stopped on first failure"
                    break
                if failed > budget.max_failures:
                    halted_reason = "max_failures budget exceeded"
                    break
        return {
            "campaign_id": plan.get("campaign_id", ""),
            "plan_digest": plan.get("plan_digest", ""),
            "executed": executed,
            "failed": failed,
            "skipped": skipped,
            "measured_cost_usd": round(measured_cost, 6),
            "halted_reason": halted_reason,
            "results": [item.to_dict() for item in results],
        }

    async def _measured_run_cost(self, run_id: str) -> float:
        events = await self.slot_runner.service.repository.list_provider_usage_events(
            run_id=run_id, limit=1000
        )
        return sum(
            float(event.cost_usd)
            for event in events
            if event.measured and event.cost_usd is not None
        )


def write_artifact_index(artifact_dir: Path) -> str:
    """Index every artifact file and return the campaign-grade SHA-256 digest.

    The digest covers the sorted (path, sha256, bytes) triples of every file
    in the slot directory, so any tampering with any artifact changes the
    digest that judges later bind to the observation.
    """

    entries: list[dict[str, Any]] = []
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file() or path.name == ARTIFACT_INDEX_NAME:
            continue
        data = path.read_bytes()
        entries.append(
            {
                "path": str(path.relative_to(artifact_dir)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    index = {"schema_version": 1, "files": entries}
    digest = _canonical_digest(index)
    index["artifact_digest"] = digest
    (artifact_dir / ARTIFACT_INDEX_NAME).write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return digest


def write_result_skeleton(
    artifact_dir: Path,
    *,
    slot: Mapping[str, Any],
    run_id: str,
    artifact_digest: str,
    duration_seconds: float,
    execution: SlotExecution,
) -> Path:
    """Write the scoring skeleton a human judge completes before ``evaluate score``.

    Scores are intentionally ``null`` — filling them is the judgment step and
    must never be automated past a draft (see :mod:`opc.operations.judging`).
    """

    goal = dict(slot.get("goal", {}))
    criteria = [dict(item) for item in goal.get("acceptance_criteria", []) or []]
    artifact_paths = [
        entry["path"]
        for entry in json.loads(
            (artifact_dir / ARTIFACT_INDEX_NAME).read_text(encoding="utf-8")
        )["files"]
    ]
    evidence: dict[str, list[str]] = {
        str(criterion["criterion_id"]): list(artifact_paths) for criterion in criteria
    }
    for requirement in goal.get("evidence_requirements", []) or []:
        evidence.setdefault(str(requirement), list(artifact_paths))
    skeleton = {
        "run_id": run_id,
        "slot_id": slot.get("slot_id", ""),
        "case_id": slot.get("case_id", ""),
        "mode": slot.get("mode", ""),
        "artifact_digest": artifact_digest,
        "criterion_scores": {
            str(criterion["criterion_id"]): None for criterion in criteria
        },
        "criterion_notes": {
            str(criterion["criterion_id"]): "" for criterion in criteria
        },
        "evidence": evidence,
        "metrics": {
            "duration_seconds": duration_seconds,
            "total_attempts": 1,
            "failed_attempts": 0 if execution.success else 1,
            "interventions": 0,
            "rework_cycles": 0,
        },
        "metadata": {
            "model_versions": dict(execution.model_versions),
            "executor": dict(execution.metadata),
            "judgment": "criterion_scores must be completed by a human judge "
            "(optionally drafted via `opc ops judge draft`)",
        },
    }
    path = artifact_dir / RESULT_SKELETON_NAME
    path.write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


@dataclass
class SubprocessExecutorConfig:
    """Command template for driving slots through the real ``opc exec`` CLI."""

    base_command: tuple[str, ...] = ("opc", "exec")
    task_args: tuple[str, ...] = ("--mode", "task", "--agent", "native")
    company_args: tuple[str, ...] = ("--mode", "company", "--company-profile", "corporate")
    timeout_seconds: float = 3600.0


def build_slot_command(
    slot: Mapping[str, Any],
    *,
    project_id: str,
    config: SubprocessExecutorConfig | None = None,
) -> list[str]:
    config = config or SubprocessExecutorConfig()
    mode = str(slot.get("mode", ""))
    if mode == "task":
        mode_args = config.task_args
    elif mode == "company":
        mode_args = config.company_args
    else:
        raise ValueError(f"unsupported benchmark slot mode: {mode!r}")
    return [
        *config.base_command,
        "-p",
        project_id,
        *mode_args,
        "--json",
        str(slot.get("prompt", "")),
    ]


def evaluate_exec_output(returncode: int | None, stdout: str) -> dict[str, Any]:
    """Judge one ``opc exec --json`` invocation fail-closed.

    Success requires ALL of: exit code 0, parseable JSON, ``ok`` true, a
    task_status outside the failure set, and a non-empty response (a
    benchmark slot without a deliverable is a failure, not a success).
    Anything unparseable is a failure — never trust a bare exit code.
    """

    verdict: dict[str, Any] = {
        "success": False,
        "response": "",
        "task_id": "",
        "session_id": "",
        "task_status": "",
        "reason": "",
    }
    if returncode != 0:
        verdict["reason"] = f"exit code {returncode}"
        return verdict
    text = str(stdout or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        verdict["reason"] = "stdout contains no JSON payload"
        return verdict
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        verdict["reason"] = "stdout JSON payload is malformed"
        return verdict
    verdict["task_id"] = str(payload.get("task_id", "") or "")
    verdict["session_id"] = str(payload.get("session_id", "") or "")
    verdict["task_status"] = str(payload.get("task_status", "") or "").lower()
    verdict["response"] = str(payload.get("response", "") or "")
    if payload.get("ok") is not True:
        verdict["reason"] = "exec reported ok=false"
        return verdict
    if verdict["task_status"] in {"failed", "cancelled"}:
        verdict["reason"] = f"task ended {verdict['task_status']}"
        return verdict
    if not verdict["response"].strip():
        verdict["reason"] = "exec produced no deliverable response"
        return verdict
    verdict["success"] = True
    return verdict


class SubprocessSlotExecutor:
    """Default executor: run the slot prompt through the ``opc exec`` CLI.

    Success is decided by :func:`evaluate_exec_output` over the ``--json``
    payload, never by the bare exit code — harness success still only means
    "the run completed with a deliverable"; quality comes exclusively from
    the human-judged scorecard.
    """

    def __init__(
        self,
        *,
        project_id: str,
        config: SubprocessExecutorConfig | None = None,
    ) -> None:
        self.project_id = project_id
        self.config = config or SubprocessExecutorConfig()

    async def __call__(
        self, slot: Mapping[str, Any], artifact_dir: Path
    ) -> SlotExecution:
        command = build_slot_command(
            slot, project_id=self.project_id, config=self.config
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return SlotExecution(
                success=False,
                metadata={
                    "command": command,
                    "error": f"timed out after {self.config.timeout_seconds}s",
                },
            )
        verdict = evaluate_exec_output(
            process.returncode, stdout.decode("utf-8", errors="replace")
        )
        return SlotExecution(
            success=bool(verdict["success"]),
            output_text=str(verdict["response"]),
            metadata={
                "command": command,
                "exit_code": process.returncode,
                "task_id": verdict["task_id"],
                "session_id": verdict["session_id"],
                "task_status": verdict["task_status"],
                "failure_reason": verdict["reason"],
                "stderr_tail": stderr.decode("utf-8", errors="replace")[-2000:],
            },
        )
