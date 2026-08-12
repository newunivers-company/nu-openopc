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
import contextlib
import hashlib
import json
import os
import re
import signal
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opc.core.config import get_project_workplace
from opc.operations.benchmarks import (
    OutcomeBenchmarkSuite,
    OutcomeObservation,
    _canonical_digest,
    campaign_progress,
)
from opc.operations.models import (
    GoalContract,
    RunManifest,
    RunStatus,
    TERMINAL_RUN_STATUSES,
    utc_now,
)

ARTIFACT_INDEX_NAME = "artifact-index.json"
RESULT_SKELETON_NAME = "result-skeleton.json"
OUTPUT_ARTIFACT_NAME = "output.md"
WORKSPACE_ARTIFACT_PREFIX = "workspace"

_WORKSPACE_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".opc",
    ".opc-comms",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_WORKSPACE_EXCLUDED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.json",
}
_WORKSPACE_SECRET_SUFFIXES = {
    ".key",
    ".p12",
    ".pfx",
    ".pem",
}

SlotExecutor = Callable[[Mapping[str, Any], Path], Awaitable["SlotExecution"]]

_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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


@dataclass(frozen=True)
class WorkspaceSnapshotPolicy:
    """Bounded, text-only evidence capture from an isolated slot workplace."""

    max_files: int = 200
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 10_000_000


def collect_workspace_artifacts(
    workspace_root: Path,
    *,
    policy: WorkspaceSnapshotPolicy | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Collect reviewable deliverables without copying runtime state or secrets."""

    policy = policy or WorkspaceSnapshotPolicy()
    root = Path(workspace_root).resolve()
    artifacts: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    total_bytes = 0
    if not root.is_dir():
        return {}, {
            "workspace_root": str(root),
            "captured_files": 0,
            "captured_bytes": 0,
            "skipped": [{"path": "", "reason": "workspace_missing"}],
            "complete": False,
        }

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _WORKSPACE_EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        normalized_name = path.name.lower()
        if (
            normalized_name in _WORKSPACE_EXCLUDED_NAMES
            or path.suffix.lower() in _WORKSPACE_SECRET_SUFFIXES
            or normalized_name.startswith(".env.")
        ):
            skipped.append({"path": str(relative), "reason": "secret_name"})
            continue
        if len(artifacts) >= policy.max_files:
            skipped.append({"path": str(relative), "reason": "file_limit"})
            break
        size = path.stat().st_size
        if size > policy.max_file_bytes:
            skipped.append({"path": str(relative), "reason": "file_too_large"})
            continue
        if total_bytes + size > policy.max_total_bytes:
            skipped.append({"path": str(relative), "reason": "total_size_limit"})
            break
        data = path.read_bytes()
        if b"\x00" in data:
            skipped.append({"path": str(relative), "reason": "binary"})
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": str(relative), "reason": "non_utf8"})
            continue
        artifacts[f"{WORKSPACE_ARTIFACT_PREFIX}/{relative.as_posix()}"] = content
        total_bytes += len(data)

    return artifacts, {
        "workspace_root": str(root),
        "captured_files": len(artifacts),
        "captured_bytes": total_bytes,
        "skipped": skipped[:50],
        "complete": not any(
            item["reason"] in {"file_limit", "total_size_limit"} for item in skipped
        ),
    }


def refresh_workspace_artifacts(
    artifact_dir: Path,
    workspace_root: Path,
    *,
    policy: WorkspaceSnapshotPolicy | None = None,
) -> dict[str, Any]:
    """Backfill a completed unscored slot with its isolated workspace evidence."""

    artifact_dir = Path(artifact_dir)
    skeleton_path = artifact_dir / RESULT_SKELETON_NAME
    if not skeleton_path.is_file():
        raise FileNotFoundError(f"result skeleton not found: {skeleton_path}")
    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    artifacts, snapshot = collect_workspace_artifacts(workspace_root, policy=policy)
    if not artifacts:
        raise ValueError("workspace snapshot produced no reviewable artifacts")
    for name, content in sorted(artifacts.items()):
        target = artifact_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    digest = write_artifact_index(artifact_dir)
    index = json.loads(
        (artifact_dir / ARTIFACT_INDEX_NAME).read_text(encoding="utf-8")
    )
    paths = [str(item["path"]) for item in index["files"]]
    skeleton["artifact_digest"] = digest
    skeleton["evidence"] = {
        str(key): list(paths) for key in dict(skeleton.get("evidence", {}))
    }
    metadata = dict(skeleton.get("metadata", {}) or {})
    executor = dict(metadata.get("executor", {}) or {})
    executor["workspace_snapshot"] = snapshot
    metadata["executor"] = executor
    skeleton["metadata"] = metadata
    skeleton_path.write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "artifact_directory": str(artifact_dir),
        "artifact_digest": digest,
        "result_skeleton_path": str(skeleton_path),
        "workspace_snapshot": snapshot,
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
        if existing is not None and force:
            # A forced rerun is an audited operator decision: reopen the
            # manifest so the durable kernel can settle the new attempt, and
            # never overwrite a run that already has a scorecard.
            if await self.service.repository.get_scorecard(run_id) is not None:
                raise ValueError(
                    f"cannot force-rerun {run_id}: a scorecard already exists"
                )
            existing.metadata = {
                **dict(existing.metadata),
                "forced_rerun_at": utc_now().isoformat(),
                "forced_rerun_previous_status": existing.status.value,
            }
            existing.status = RunStatus.RUNNING
            existing.completed_at = None
            await self.service.repository.save_manifest(existing)

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
                "benchmark_workload": slot.get("workload", ""),
                "benchmark_mode": slot.get("mode", ""),
                "benchmark_repetition": slot.get("repetition", 0),
                "benchmark_suite_digest": plan.get("suite_digest", ""),
                "benchmark_expected_slots": plan.get("slot_count", 0),
                "benchmark_expected_pairs": plan.get("pair_count", 0),
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
    max_pairs: int | None = None
    max_failures: int = 3
    max_cost_usd: float | None = None
    max_wall_clock_seconds: float | None = None

    def validate(self) -> None:
        if self.max_slots is not None and self.max_slots < 1:
            raise ValueError("max_slots must be positive when set")
        if self.max_pairs is not None and self.max_pairs < 1:
            raise ValueError("max_pairs must be positive when set")
        if self.max_failures < 0:
            raise ValueError("max_failures must be non-negative")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive when set")
        if (
            self.max_wall_clock_seconds is not None
            and self.max_wall_clock_seconds <= 0
        ):
            raise ValueError("max_wall_clock_seconds must be positive when set")


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
        clock: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        plan = verify_plan(plan)
        budget = budget or CampaignBudget()
        budget.validate()
        import time as _time

        tick = clock or _time.monotonic
        started_at = tick()
        workload_filter = {str(item) for item in workloads} if workloads else None
        mode_filter = {str(item) for item in modes} if modes else None

        results: list[SlotRunResult] = []
        executed = failed = skipped = 0
        measured_cost = 0.0
        halted_reason = ""
        selected_pair_ids: set[str] = set()
        visited_pair_ids: set[str] = set()
        slots: Sequence[Mapping[str, Any]] = sorted(
            plan.get("slots", []) or [], key=lambda item: int(item.get("sequence", 0))
        )
        for slot in slots:
            if workload_filter and slot.get("workload") not in workload_filter:
                continue
            if mode_filter and slot.get("mode") not in mode_filter:
                continue
            pair_id = str(slot.get("pair_id", "") or "")
            if pair_id and pair_id not in visited_pair_ids:
                pair_complete = await self._pair_is_complete(
                    slots,
                    pair_id,
                    workload_filter=workload_filter,
                )
                if (
                    not pair_complete
                    and budget.max_pairs is not None
                    and len(selected_pair_ids) >= budget.max_pairs
                ):
                    halted_reason = "max_pairs budget reached"
                    break
                visited_pair_ids.add(pair_id)
                if not pair_complete:
                    selected_pair_ids.add(pair_id)
            if budget.max_slots is not None and executed >= budget.max_slots:
                halted_reason = "max_slots budget reached"
                break
            if (
                budget.max_wall_clock_seconds is not None
                and tick() - started_at >= budget.max_wall_clock_seconds
            ):
                halted_reason = "max_wall_clock budget reached"
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
        completed_pair_ids, partial_pair_ids = await self._pair_progress(
            slots,
            visited_pair_ids,
            workload_filter=workload_filter,
        )
        return {
            "campaign_id": plan.get("campaign_id", ""),
            "plan_digest": plan.get("plan_digest", ""),
            "executed": executed,
            "failed": failed,
            "skipped": skipped,
            "pairs_selected": len(selected_pair_ids),
            "pairs_completed": len(completed_pair_ids),
            "partial_pair_ids": sorted(partial_pair_ids),
            "measured_cost_usd": round(measured_cost, 6),
            "halted_reason": halted_reason,
            "results": [item.to_dict() for item in results],
        }

    async def _pair_is_complete(
        self,
        slots: Sequence[Mapping[str, Any]],
        pair_id: str,
        *,
        workload_filter: set[str] | None,
    ) -> bool:
        pair_slots = [
            slot
            for slot in slots
            if str(slot.get("pair_id", "") or "") == pair_id
            and (
                not workload_filter
                or str(slot.get("workload", "") or "") in workload_filter
            )
        ]
        if len(pair_slots) < 2:
            return False
        for slot in pair_slots:
            manifest = await self.slot_runner.service.repository.get_manifest(
                str(slot.get("run_id", "") or "")
            )
            if manifest is None or manifest.status not in TERMINAL_RUN_STATUSES:
                return False
        return True

    async def _pair_progress(
        self,
        slots: Sequence[Mapping[str, Any]],
        pair_ids: set[str],
        *,
        workload_filter: set[str] | None,
    ) -> tuple[set[str], set[str]]:
        completed: set[str] = set()
        partial: set[str] = set()
        for pair_id in pair_ids:
            if await self._pair_is_complete(
                slots,
                pair_id,
                workload_filter=workload_filter,
            ):
                completed.add(pair_id)
            else:
                partial.add(pair_id)
        return completed, partial

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
        if not path.is_file() or path.name in {
            ARTIFACT_INDEX_NAME,
            RESULT_SKELETON_NAME,
        }:
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


def validate_artifact_index(
    artifact_dir: Path,
    *,
    expected_digest: str = "",
) -> dict[str, Any]:
    """Verify that judgment reads the exact sealed slot artifact set."""

    root = Path(artifact_dir).resolve()
    index_path = root / ARTIFACT_INDEX_NAME
    if not index_path.is_file():
        raise FileNotFoundError(f"artifact index not found: {index_path}")
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or int(raw.get("schema_version", 0) or 0) != 1:
        raise ValueError("artifact index must be a schema_version 1 object")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("artifact index must contain at least one artifact")

    normalized: list[dict[str, Any]] = []
    indexed_paths: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("artifact index entries must be objects")
        relative_text = str(item.get("path", "") or "").strip()
        relative = Path(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_text in {ARTIFACT_INDEX_NAME, RESULT_SKELETON_NAME}
        ):
            raise ValueError(f"artifact index contains unsafe path: {relative_text!r}")
        normalized_path = relative.as_posix()
        if normalized_path in indexed_paths:
            raise ValueError(f"artifact index contains duplicate path: {normalized_path}")
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError(
                f"indexed artifact is missing or escapes its directory: {normalized_path}"
            )
        data = target.read_bytes()
        actual_digest = hashlib.sha256(data).hexdigest()
        claimed_digest = str(item.get("sha256", "") or "").strip().lower()
        claimed_bytes = int(item.get("bytes", -1))
        if claimed_digest != actual_digest or claimed_bytes != len(data):
            raise ValueError(f"indexed artifact changed after sealing: {normalized_path}")
        indexed_paths.add(normalized_path)
        normalized.append(
            {
                "path": normalized_path,
                "sha256": actual_digest,
                "bytes": len(data),
            }
        )

    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {ARTIFACT_INDEX_NAME, RESULT_SKELETON_NAME}
    }
    unindexed = sorted(actual_paths - indexed_paths)
    if unindexed:
        raise ValueError(
            "artifact directory contains unsealed files: " + ", ".join(unindexed[:10])
        )
    normalized.sort(key=lambda item: str(item["path"]))
    digest = _canonical_digest({"schema_version": 1, "files": normalized})
    claimed = str(raw.get("artifact_digest", "") or "").strip().lower()
    if claimed != digest:
        raise ValueError("artifact index digest does not match its sealed entries")
    expected = str(expected_digest or "").strip().lower()
    if expected and expected != digest:
        raise ValueError("artifact digest does not match the expected judgment identity")
    return {
        "schema_version": 1,
        "artifact_digest": digest,
        "file_count": len(normalized),
        "total_bytes": sum(int(item["bytes"]) for item in normalized),
        "files": normalized,
    }


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


# Company-mode preflight is a chain of checkpoints, each answered with a
# fixed protocol reply (identical for every slot, so it cannot bias the
# comparison): staffing selection -> "auto recruit", recruitment proposal
# -> "approve". The final delivery-release gate (awaiting_human) is also
# answered with a scripted approve FOR BENCHMARK SLOTS ONLY: it releases
# the deliverable inside a synthetic run, it does not attest quality —
# quality authority stays exclusively with the human-confirmed scorecard.
CHECKPOINT_REPLIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "pending manual staffing selection",
            "Reply `approve` to use these defaults",
        ),
        "auto recruit",
    ),
    (
        (
            "pending staffing decision",
            "accept these hires and start execution",
        ),
        "approve",
    ),
    # Company mode can end a turn with a dispatch acknowledgement while its
    # work items keep running in the durable runtime. Nudging the session
    # resumes the runtime and eventually yields the integrated delivery.
    (
        (
            "runtime will reactivate",
            "Dispatched and confirmed",
            "currently running",
            "waiting on the",
            # Interim runtime snapshots: the turn ended while work items are
            # still suspended/blocked awaiting the next runtime step (e.g.
            # the final decider's arbitration turn).
            "checkpoint remains available to continue",
            "Routed the latest user follow-up",
            "Latest Runtime Snapshot",
        ),
        "continue",
    ),
    (
        (
            "Organization Runtime Parked",
            "waiting on human input",
        ),
        # Approval-type checkpoints reject plain chat by design; the
        # executor resolves them with explicit checkpoint-addressed replies.
        "__answer_checkpoints__",
    ),
)

ANSWER_CHECKPOINTS = "__answer_checkpoints__"

# Per-checkpoint-type scripted replies for parked benchmark runs. The
# delivery self-evolution feedback is IGNORED on purpose: synthetic
# benchmark runs must never feed employee evolution.
CHECKPOINT_TYPE_REPLIES: tuple[tuple[str, str], ...] = (
    ("company_delivery_feedback", "ignore"),
    ("company_work_item_gate", "approve"),
)
BLOCKED_RESPONSE_MARKERS = (
    "Awaiting user input",
    "Tool execution blocked by autonomy policy",
)


def checkpoint_reply_for(response: str) -> str | None:
    """Return the scripted reply for a recognized checkpoint response."""

    text = str(response or "")
    for markers, reply in CHECKPOINT_REPLIES:
        if any(marker in text for marker in markers):
            return reply
    return None


def classify_response(response: str) -> str:
    """Classify an exec response: deliverable, staffing_checkpoint, or blocked.

    Headless runs can end "successfully" while actually returning an approval
    prompt instead of work. Checkpoints get their scripted protocol reply;
    blocked responses are failures, never deliverables.
    """

    text = str(response or "")
    if checkpoint_reply_for(text) is not None:
        return "staffing_checkpoint"
    if any(marker in text for marker in BLOCKED_RESPONSE_MARKERS):
        return "blocked"
    return "deliverable"


@dataclass
class SubprocessExecutorConfig:
    """Command template for driving slots through the real ``opc`` CLI."""

    base_command: tuple[str, ...] = ("opc", "exec")
    task_args: tuple[str, ...] = ("--mode", "task", "--agent", "native")
    company_args: tuple[str, ...] = ("--mode", "company", "--company-profile", "corporate")
    # Checkpoints are answered with a regular session message (the engine
    # parses approve/auto replies); `session continue` is only for paused
    # company runtime checkpoints and rejects staffing replies.
    continue_command: tuple[str, ...] = ("opc", "session", "send")
    max_continuations: int = 12
    max_process_invocations: int = 48
    max_external_agent_calls: int = 24
    timeout_seconds: float = 3600.0
    termination_grace_seconds: float = 5.0
    isolate_slot_projects: bool = True


def subprocess_executor_preflight(
    config: SubprocessExecutorConfig,
    *,
    native_transport_ready: bool,
) -> dict[str, Any]:
    """Fail closed before a campaign compares an unavailable Task executor."""

    task_args = list(config.task_args)
    try:
        task_agent = task_args[task_args.index("--agent") + 1]
    except (ValueError, IndexError):
        task_agent = ""
    blockers: list[str] = []
    warnings: list[str] = []
    if config.timeout_seconds <= 0:
        blockers.append("Slot timeout must be positive.")
    if config.max_continuations < 0:
        blockers.append("Continuation budget must be non-negative.")
    if config.max_process_invocations < 1:
        blockers.append("Process invocation budget must be positive.")
    if config.max_external_agent_calls < 1:
        blockers.append("External-agent call budget must be positive.")
    if not task_agent:
        blockers.append("Task executor command does not pin an --agent.")
    elif task_agent == "native":
        if not native_transport_ready:
            blockers.append(
                "Task/native LLM route is not transport-ready for live execution."
            )
    else:
        executable = {
            "codex": "codex",
            "claude_code": "claude",
            "cursor": "cursor-agent",
            "opencode": "opencode",
        }.get(task_agent)
        if executable is None:
            blockers.append(f"Unsupported Task executor agent: {task_agent}.")
        elif shutil.which(executable) is None:
            blockers.append(
                f"Task executor {task_agent} is unavailable ({executable} not found)."
            )
        else:
            warnings.append(
                f"{task_agent} executable is present; authentication is rechecked "
                "by the isolated slot runtime."
            )
    return {
        "execution_ready": not blockers,
        "task_agent": task_agent,
        "native_transport_ready": bool(native_transport_ready),
        "blockers": blockers,
        "warnings": warnings,
    }


def slot_execution_project_id(
    project_id: str,
    slot: Mapping[str, Any],
) -> str:
    """Return a safe, deterministic project id that isolates one slot.

    Task and Company arms must never share a mutable workplace.  The campaign
    repository still owns the governed run manifest; this project id scopes the
    concrete executor task, workspace, checkpoints, and external-agent session.
    """

    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(project_id or "benchmark"))
    base = base.strip("-_") or "benchmark"
    identity = str(
        slot.get("run_id")
        or slot.get("slot_id")
        or _canonical_digest(dict(slot))
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{base[:40]}-slot-{suffix}"


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
    text = _ANSI_ESCAPES.sub("", str(stdout or "")).strip()
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
        self._slot_started_monotonic = 0.0
        self._slot_deadline_monotonic = 0.0
        self._process_invocations = 0

    async def __call__(
        self, slot: Mapping[str, Any], artifact_dir: Path
    ) -> SlotExecution:
        loop = asyncio.get_running_loop()
        self._slot_started_monotonic = loop.time()
        self._slot_deadline_monotonic = (
            self._slot_started_monotonic + self.config.timeout_seconds
        )
        self._process_invocations = 0
        execution_project_id = (
            slot_execution_project_id(self.project_id, slot)
            if self.config.isolate_slot_projects
            else self.project_id
        )
        command = build_slot_command(
            slot, project_id=execution_project_id, config=self.config
        )
        spawn = await self._invoke(command)
        if spawn.get("timed_out") or spawn.get("budget_exhausted"):
            return self._finalize_execution(
                execution_project_id=execution_project_id,
                success=False,
                metadata={**spawn, "command": command, "continuations": 0},
            )
        verdict = evaluate_exec_output(spawn["exit_code"], spawn["stdout"])
        continuations = 0
        task_id = str(verdict["task_id"])
        # A "successful" exec that returns a staffing checkpoint is not a
        # deliverable yet — answer it with the scripted protocol reply and
        # keep the run's real result instead.
        answered_checkpoints: list[dict[str, str]] = []
        while (
            verdict["success"]
            and (reply := checkpoint_reply_for(verdict["response"])) is not None
            and continuations < self.config.max_continuations
            and task_id
        ):
            continuations += 1
            if reply == ANSWER_CHECKPOINTS:
                answered = await self._answer_pending_checkpoints(
                    execution_project_id
                )
                answered_checkpoints.extend(answered)
                reply = "continue"
            continue_command = [
                *self.config.continue_command,
                task_id,
                reply,
                "-p",
                execution_project_id,
                "--json",
            ]
            spawn = await self._invoke(continue_command)
            if spawn.get("timed_out") or spawn.get("budget_exhausted"):
                return self._finalize_execution(
                    execution_project_id=execution_project_id,
                    success=False,
                    metadata={
                        **spawn,
                        "command": command,
                        "continuations": continuations,
                        "answered_checkpoints": answered_checkpoints,
                    },
                )
            verdict = evaluate_exec_output(spawn["exit_code"], spawn["stdout"])
            verdict["task_id"] = verdict["task_id"] or task_id
        success = bool(verdict["success"])
        reason = str(verdict["reason"])
        kind = classify_response(verdict["response"]) if success else "failure"
        if success and kind != "deliverable":
            success = False
            reason = f"run ended with a {kind} response, not a deliverable"
        return self._finalize_execution(
            execution_project_id=execution_project_id,
            success=success,
            output_text=str(verdict["response"]) if success else "",
            metadata={
                "command": command,
                "execution_project_id": execution_project_id,
                "exit_code": spawn["exit_code"],
                "task_id": verdict["task_id"] or task_id,
                "session_id": verdict["session_id"],
                "task_status": verdict["task_status"],
                "failure_reason": reason,
                "continuations": continuations,
                "answered_checkpoints": answered_checkpoints,
                "stderr_tail": spawn["stderr_tail"],
            },
        )

    async def _answer_pending_checkpoints(
        self, execution_project_id: str
    ) -> list[dict[str, str]]:
        """Resolve parked approval checkpoints with explicit-id replies.

        Approval-type checkpoints intentionally refuse plain chat; each is
        answered through ``session send --respond-checkpoint`` with the
        scripted per-type reply from ``CHECKPOINT_TYPE_REPLIES``.
        """

        listing = await self._invoke(
            [
                "opc",
                "runtime",
                "checkpoints",
                "-p",
                execution_project_id,
                "--json",
            ]
        )
        answered: list[dict[str, str]] = []
        if listing.get("timed_out") or listing.get("exit_code") != 0:
            return answered
        text = str(listing.get("stdout", ""))
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return answered
        try:
            payload = json.loads(_ANSI_ESCAPES.sub("", text[start : end + 1]))
        except json.JSONDecodeError:
            return answered
        replies = dict(CHECKPOINT_TYPE_REPLIES)
        for checkpoint in payload.get("checkpoints", []) or []:
            if str(checkpoint.get("status", "")).lower() != "pending":
                continue
            checkpoint_type = str(checkpoint.get("checkpoint_type", "") or "")
            reply_kind = replies.get(checkpoint_type)
            checkpoint_id = str(checkpoint.get("checkpoint_id", "") or "")
            reply_task = str(checkpoint.get("task_id", "") or "")
            if not reply_kind or not checkpoint_id or not reply_task:
                continue
            result = await self._invoke(
                [
                    "opc",
                    "session",
                    "send",
                    reply_task,
                    reply_kind,
                    "--respond-checkpoint",
                    checkpoint_id,
                    "--reply-kind",
                    reply_kind,
                    "-p",
                    execution_project_id,
                    "--json",
                ]
            )
            answered.append(
                {
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_type": checkpoint_type,
                    "reply_kind": reply_kind,
                    "ok": str(bool(not result.get("timed_out") and result.get("exit_code") == 0)),
                }
            )
        return answered

    async def _invoke(self, command: list[str]) -> dict[str, Any]:
        if self._process_invocations >= self.config.max_process_invocations:
            return {
                "command": command,
                "timed_out": False,
                "budget_exhausted": True,
                "exit_code": None,
                "stdout": "",
                "stderr_tail": "",
                "error": "process invocation budget exhausted",
            }
        if self._remaining_seconds() <= 0:
            return {
                "command": command,
                "timed_out": True,
                "deadline_exceeded": True,
                "exit_code": None,
                "stdout": "",
                "stderr_tail": "",
                "error": (
                    f"slot deadline exceeded after {self.config.timeout_seconds}s"
                ),
            }
        self._process_invocations += 1
        return await self._spawn(command)

    def _remaining_seconds(self) -> float:
        if self._slot_deadline_monotonic <= 0:
            return self.config.timeout_seconds
        return max(
            0.0,
            self._slot_deadline_monotonic - asyncio.get_running_loop().time(),
        )

    def _finalize_execution(
        self,
        *,
        execution_project_id: str,
        success: bool,
        metadata: Mapping[str, Any],
        output_text: str = "",
    ) -> SlotExecution:
        workspace = get_project_workplace(execution_project_id)
        artifacts, snapshot = collect_workspace_artifacts(workspace)
        permits_dir = workspace / ".opc" / "benchmark_external_call_budget"
        external_agent_calls = (
            len(list(permits_dir.glob("call-*.json"))) if permits_dir.is_dir() else 0
        )
        raw_log_dir = workspace / ".opc" / "external_logs"
        raw_external_logs = (
            len(list(raw_log_dir.glob("*.log"))) if raw_log_dir.is_dir() else 0
        )
        elapsed = max(
            0.0,
            asyncio.get_running_loop().time() - self._slot_started_monotonic,
        )
        return SlotExecution(
            success=success,
            output_text=output_text,
            artifacts=artifacts,
            metadata={
                **dict(metadata),
                "failure_reason": (
                    ""
                    if success
                    else str(
                        metadata.get("failure_reason")
                        or metadata.get("error")
                        or "executor reported failure"
                    )
                ),
                "process_invocations": self._process_invocations,
                "process_invocation_limit": self.config.max_process_invocations,
                "external_agent_calls": external_agent_calls,
                "external_agent_call_limit": self.config.max_external_agent_calls,
                "raw_external_log_count": raw_external_logs,
                "slot_timeout_seconds": self.config.timeout_seconds,
                "slot_elapsed_seconds": elapsed,
                "deadline_exceeded": bool(
                    metadata.get("deadline_exceeded")
                    or metadata.get("timed_out")
                ),
                "workspace_snapshot": snapshot,
            },
        )

    async def _spawn(self, command: list[str]) -> dict[str, Any]:
        environment = os.environ.copy()
        environment["OPC_BENCHMARK_EXTERNAL_CALL_LIMIT"] = str(
            self.config.max_external_agent_calls
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=os.name == "posix",
        )
        timeout = self._remaining_seconds()
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            await self._terminate_process_tree(process)
            return {
                "command": command,
                "timed_out": True,
                "deadline_exceeded": True,
                "exit_code": None,
                "stdout": "",
                "stderr_tail": "",
                "error": (
                    f"slot deadline exceeded after {self.config.timeout_seconds}s"
                ),
            }
        return {
            "command": command,
            "timed_out": False,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr_tail": stderr.decode("utf-8", errors="replace")[-2000:],
        }

    async def _terminate_process_tree(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
        else:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.config.termination_grace_seconds
            )
            return
        except asyncio.TimeoutError:
            pass
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                process.wait(), timeout=self.config.termination_grace_seconds
            )


async def pending_judgment_slots(
    service: Any, plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Slots whose run completed but still lacks a scorecard (judgment work)."""

    plan = verify_plan(plan)
    pending: list[dict[str, Any]] = []
    for slot in plan.get("slots", []) or []:
        run_id = str(slot["run_id"])
        manifest = await service.repository.get_manifest(run_id)
        if manifest is None or manifest.status != RunStatus.COMPLETED:
            continue
        if await service.repository.get_scorecard(run_id) is None:
            pending.append(dict(slot))
    return pending


async def campaign_status(
    service: Any,
    suite: "OutcomeBenchmarkSuite",
    plan: Mapping[str, Any],
    observations: Sequence["OutcomeObservation"],
) -> dict[str, Any]:
    """One view of a campaign: execution, judgment, and gate distance.

    Combines the durable execution side (manifests/scorecards in the
    repository) with the observation side (``campaign_progress``) so an
    operator sees, per workload, what is run, what awaits judgment, and how
    many trusted pairs remain before the gate — without chaining five
    commands and files by hand.
    """

    plan = verify_plan(plan)
    if str(plan.get("suite_digest", "")) != suite.digest:
        raise ValueError("campaign plan does not match the provided suite")
    progress = campaign_progress(
        suite, observations, campaign_id=str(plan["campaign_id"])
    )
    execution: dict[str, dict[str, int]] = {
        workload: {
            "slots": 0,
            "not_started": 0,
            "completed": 0,
            "failed": 0,
            "in_flight": 0,
            "awaiting_judgment": 0,
            "awaiting_observation": 0,
        }
        for workload in suite.workloads
    }
    awaiting: list[dict[str, Any]] = []
    awaiting_observation: list[dict[str, Any]] = []
    observed_keys = {
        (row.case_id, row.mode, row.repetition)
        for row in observations
        if row.suite_digest == suite.digest
        and str(row.metadata.get("campaign_id", "") or "")
        == str(plan["campaign_id"])
    }
    for slot in plan.get("slots", []) or []:
        workload = str(slot["workload"])
        stats = execution[workload]
        stats["slots"] += 1
        run_id = str(slot["run_id"])
        manifest = await service.repository.get_manifest(run_id)
        if manifest is None:
            stats["not_started"] += 1
            continue
        if manifest.status == RunStatus.FAILED:
            stats["failed"] += 1
            continue
        if manifest.status != RunStatus.COMPLETED:
            stats["in_flight"] += 1
            continue
        stats["completed"] += 1
        if await service.repository.get_scorecard(run_id) is None:
            stats["awaiting_judgment"] += 1
            awaiting.append(
                {
                    "slot_id": slot["slot_id"],
                    "run_id": run_id,
                    "workload": workload,
                    "artifact_directory": slot["artifact_directory"],
                }
            )
            continue
        slot_key = (
            str(slot["case_id"]),
            str(slot["mode"]),
            int(slot["repetition"]),
        )
        if slot_key not in observed_keys:
            stats["awaiting_observation"] += 1
            awaiting_observation.append(
                {
                    "slot_id": slot["slot_id"],
                    "run_id": run_id,
                    "case_id": slot["case_id"],
                    "mode": slot["mode"],
                    "repetition": slot["repetition"],
                    "workload": workload,
                }
            )
    workloads: dict[str, dict[str, Any]] = {}
    for workload in suite.workloads:
        observed = dict(progress["workloads"].get(workload, {}))
        trusted = int(observed.get("trusted_pairs", 0) or 0)
        minimum = int(
            observed.get(
                "minimum_trusted_pairs", suite.minimum_paired_samples_per_workload
            )
        )
        workloads[workload] = {
            **execution[workload],
            **observed,
            "trusted_pairs_remaining_to_gate": max(0, minimum - trusted),
        }
    totals = {
        key: sum(int(stats[key]) for stats in execution.values())
        for key in (
            "not_started",
            "failed",
            "in_flight",
            "awaiting_judgment",
            "awaiting_observation",
        )
    }
    totals.update(
        {
            "untrusted_observations": len(progress["untrusted_slots"]),
            "duplicate_observations": len(progress["duplicate_slots"]),
            "foreign_observations": len(progress["foreign_run_ids"]),
        }
    )
    preflight = dict(plan.get("execution_preflight", {}) or {})
    expansion_gate = _campaign_expansion_gate(
        totals=totals,
        preflight=preflight,
        workloads=workloads,
    )
    next_actions = _campaign_next_actions(
        totals=totals,
        preflight=preflight,
        trusted_pairs_remaining=sum(
            int(stats["trusted_pairs_remaining_to_gate"])
            for stats in workloads.values()
        ),
        expansion_gate=expansion_gate,
    )
    return {
        "schema_version": 1,
        "campaign_id": plan.get("campaign_id", ""),
        "plan_digest": plan.get("plan_digest", ""),
        "suite_digest": suite.digest,
        "execution_preflight": preflight,
        "workloads": workloads,
        "awaiting_judgment": awaiting,
        "awaiting_observation": awaiting_observation,
        "attention_summary": totals,
        "batch_expansion": expansion_gate,
        "next_actions": next_actions,
        "observed_slots": progress["observed_slots"],
        "trusted_pairs": progress["trusted_pairs"],
        "promotion_eligible": progress["promotion_eligible"],
    }


def _campaign_expansion_gate(
    *,
    totals: Mapping[str, int],
    preflight: Mapping[str, Any],
    workloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Permit only canaries or one bounded expansion pair at a time.

    A full campaign must not keep consuming slots while failed execution,
    unreviewed scorecards, or ledger defects are outstanding. Before the first
    expansion, every workload also needs at least one trusted paired canary.
    This gate is intentionally separate from product promotion: even a ready
    expansion gate authorizes only the next bounded pair.
    """

    blockers: list[str] = []
    if preflight and not bool(preflight.get("execution_ready", False)):
        blockers.append("execution_preflight")
    attention_fields = (
        "failed",
        "in_flight",
        "awaiting_judgment",
        "awaiting_observation",
        "untrusted_observations",
        "duplicate_observations",
        "foreign_observations",
    )
    blockers.extend(
        field for field in attention_fields if int(totals.get(field, 0) or 0) > 0
    )
    missing_canary_workloads = sorted(
        workload
        for workload, stats in workloads.items()
        if int(stats.get("trusted_pairs", 0) or 0) < 1
    )
    canary_complete = not missing_canary_workloads
    expansion_ready = not blockers and canary_complete
    if blockers:
        phase = "blocked"
        next_pair_budget = 0
    elif not canary_complete:
        phase = "canary_collection"
        next_pair_budget = 1
    else:
        phase = "bounded_expansion"
        next_pair_budget = 1
    if int(totals.get("not_started", 0) or 0) <= 0:
        next_pair_budget = 0
    return {
        "phase": phase,
        "expansion_ready": expansion_ready,
        "next_pair_budget": next_pair_budget,
        "maximum_pairs_per_batch": 1,
        "trusted_canary_pairs_required_per_workload": 1,
        "missing_canary_workloads": missing_canary_workloads,
        "blockers": blockers,
        "promotion_authority": False,
    }


def _campaign_next_actions(
    *,
    totals: Mapping[str, int],
    preflight: Mapping[str, Any],
    trusted_pairs_remaining: int,
    expansion_gate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build a deterministic, operator-oriented campaign action queue."""

    actions: list[dict[str, Any]] = []
    if preflight and not bool(preflight.get("execution_ready", False)):
        actions.append(
            {
                "priority": "critical",
                "action": "bind_benchmark_inputs",
                "count": int(preflight.get("blocked_case_count", 0) or 0),
                "reason": "Benchmark input contracts are incomplete.",
            }
        )
    if totals["failed"]:
        actions.append(
            {
                "priority": "high",
                "action": "diagnose_failed_slots",
                "count": totals["failed"],
                "reason": "Failed runs cannot become trusted paired evidence.",
            }
        )
    if totals["duplicate_observations"]:
        actions.append(
            {
                "priority": "high",
                "action": "resolve_duplicate_observations",
                "count": totals["duplicate_observations"],
                "reason": "Retries cannot be counted as independent samples.",
            }
        )
    if totals["untrusted_observations"]:
        actions.append(
            {
                "priority": "high",
                "action": "repair_untrusted_observations",
                "count": totals["untrusted_observations"],
                "reason": "Actual-run evidence, authority, or artifact identity is incomplete.",
            }
        )
    if totals["awaiting_judgment"]:
        actions.append(
            {
                "priority": "high",
                "action": "confirm_independent_judgments",
                "count": totals["awaiting_judgment"],
                "reason": "Completed artifacts need criterion-level authority.",
            }
        )
    if totals["awaiting_observation"]:
        actions.append(
            {
                "priority": "high",
                "action": "record_trusted_observations",
                "count": totals["awaiting_observation"],
                "reason": "Scored runs are not yet present in the campaign ledger.",
            }
        )
    if totals["in_flight"]:
        actions.append(
            {
                "priority": "medium",
                "action": "monitor_in_flight_slots",
                "count": totals["in_flight"],
                "reason": "Runs are active and should settle before replacement work.",
            }
        )
    if totals["foreign_observations"]:
        actions.append(
            {
                "priority": "medium",
                "action": "quarantine_foreign_observations",
                "count": totals["foreign_observations"],
                "reason": "Rows from another suite or campaign must stay out of this ledger.",
            }
        )
    if totals["not_started"] and int(expansion_gate.get("next_pair_budget", 0) or 0) < 1:
        actions.append(
            {
                "priority": "medium",
                "action": "hold_batch_expansion",
                "count": totals["not_started"],
                "reason": (
                    "Resolve execution, judgment, and observation blockers "
                    "before consuming another pair."
                ),
            }
        )
    elif totals["not_started"] and expansion_gate.get("missing_canary_workloads"):
        actions.append(
            {
                "priority": "medium",
                "action": "run_workload_canaries",
                "count": len(expansion_gate["missing_canary_workloads"]),
                "reason": (
                    "Collect one trusted paired canary for every workload, "
                    "one pair per batch."
                ),
            }
        )
    elif totals["not_started"]:
        actions.append(
            {
                "priority": "medium",
                "action": "run_next_bounded_pair",
                "count": totals["not_started"],
                "reason": (
                    "Expansion is ready for one complete pair; reassess the "
                    "gate after it settles."
                ),
            }
        )
    if trusted_pairs_remaining:
        actions.append(
            {
                "priority": "medium",
                "action": "close_trusted_pair_gap",
                "count": trusted_pairs_remaining,
                "reason": "Promotion stays blocked until every workload reaches its pair floor.",
            }
        )
    if not actions:
        actions.append(
            {
                "priority": "low",
                "action": "review_promotion_dossier",
                "count": 1,
                "reason": "Execution, judgment, and paired evidence queues are clear.",
            }
        )
    return actions
