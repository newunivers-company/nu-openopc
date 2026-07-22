"""Versioned contracts used by the OpenOPC operating kernel."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for persisted operations data."""
    return datetime.now(timezone.utc)


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return default
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clamp_score(value: Any) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class ContractMixin:
    schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class GoalContractStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


class LearningAssetStatus(str, Enum):
    CANDIDATE = "candidate"
    EVALUATED = "evaluated"
    SHADOW = "shadow"
    CANARY = "canary"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    RETIRED = "retired"


class CapabilityKind(str, Enum):
    LLM = "llm"
    EXTERNAL_AGENT = "external_agent"
    RESOURCE = "resource"


@dataclass
class AcceptanceCriterion(ContractMixin):
    criterion_id: str
    description: str
    weight: float = 1.0
    minimum_score: float = 1.0
    required: bool = True
    evidence_required: bool = True
    evaluator: str = "deterministic"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.criterion_id.strip():
            raise ValueError("criterion_id is required")
        if not self.description.strip():
            raise ValueError(f"criterion {self.criterion_id!r} requires a description")
        if self.weight <= 0:
            raise ValueError(f"criterion {self.criterion_id!r} weight must be positive")
        if not 0 <= self.minimum_score <= 1:
            raise ValueError(f"criterion {self.criterion_id!r} minimum_score must be between 0 and 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceCriterion":
        return cls(
            criterion_id=str(data.get("criterion_id", "")),
            description=str(data.get("description", "")),
            weight=float(data.get("weight", 1.0) or 1.0),
            minimum_score=float(data.get("minimum_score", 1.0)),
            required=bool(data.get("required", True)),
            evidence_required=bool(data.get("evidence_required", True)),
            evaluator=str(data.get("evaluator", "deterministic") or "deterministic"),
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class ResourceBudget(ContractMixin):
    max_cost_usd: float | None = None
    max_duration_seconds: float | None = None
    max_tokens: int | None = None
    max_interventions: int | None = None
    max_rework_cycles: int | None = None
    max_failed_attempts: int | None = None
    schema_version: int = 1

    def validate(self) -> None:
        values = {
            "max_cost_usd": self.max_cost_usd,
            "max_duration_seconds": self.max_duration_seconds,
            "max_tokens": self.max_tokens,
            "max_interventions": self.max_interventions,
            "max_rework_cycles": self.max_rework_cycles,
            "max_failed_attempts": self.max_failed_attempts,
        }
        for name, value in values.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative or null")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ResourceBudget":
        payload = dict(data or {})
        return cls(
            max_cost_usd=_optional_float(payload.get("max_cost_usd")),
            max_duration_seconds=_optional_float(payload.get("max_duration_seconds")),
            max_tokens=_optional_int(payload.get("max_tokens")),
            max_interventions=_optional_int(payload.get("max_interventions")),
            max_rework_cycles=_optional_int(payload.get("max_rework_cycles")),
            max_failed_attempts=_optional_int(payload.get("max_failed_attempts")),
            schema_version=int(payload.get("schema_version", 1) or 1),
        )


@dataclass
class GoalContract(ContractMixin):
    title: str
    objective: str
    project_id: str = "default"
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: GoalContractStatus = GoalContractStatus.ACTIVE
    non_goals: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    deadline: datetime | None = None
    risk_level: str = "medium"
    evidence_requirements: list[str] = field(default_factory=list)
    human_gates: list[str] = field(default_factory=list)
    organization_id: str = ""
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.goal_id.strip():
            raise ValueError("goal_id is required")
        if not self.project_id.strip():
            raise ValueError("project_id is required")
        if not self.title.strip():
            raise ValueError("goal title is required")
        if not self.objective.strip():
            raise ValueError("goal objective is required")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        seen: set[str] = set()
        for criterion in self.acceptance_criteria:
            criterion.validate()
            if criterion.criterion_id in seen:
                raise ValueError(f"duplicate criterion_id: {criterion.criterion_id}")
            seen.add(criterion.criterion_id)
        self.budget.validate()
        if self.version < 1:
            raise ValueError("goal contract version must be at least 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoalContract":
        status = GoalContractStatus(str(data.get("status", GoalContractStatus.ACTIVE.value)))
        return cls(
            goal_id=str(data.get("goal_id", "") or str(uuid.uuid4())),
            project_id=str(data.get("project_id", "default") or "default"),
            organization_id=str(data.get("organization_id", "") or ""),
            title=str(data.get("title", "")),
            objective=str(data.get("objective", "")),
            status=status,
            non_goals=[str(item) for item in data.get("non_goals", []) or []],
            deliverables=[str(item) for item in data.get("deliverables", []) or []],
            acceptance_criteria=[
                AcceptanceCriterion.from_dict(item)
                for item in data.get("acceptance_criteria", []) or []
                if isinstance(item, Mapping)
            ],
            budget=ResourceBudget.from_dict(data.get("budget")),
            deadline=parse_datetime(data.get("deadline")),
            risk_level=str(data.get("risk_level", "medium") or "medium"),
            evidence_requirements=[str(item) for item in data.get("evidence_requirements", []) or []],
            human_gates=[str(item) for item in data.get("human_gates", []) or []],
            version=int(data.get("version", 1) or 1),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(data.get("updated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class RunManifest(ContractMixin):
    goal_id: str
    project_id: str = "default"
    goal_version: int = 0
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: RunStatus = RunStatus.PENDING
    organization_id: str = ""
    organization_version: str = ""
    configuration_digest: str = ""
    model_versions: dict[str, str] = field(default_factory=dict)
    provider_versions: dict[str, str] = field(default_factory=dict)
    skill_versions: dict[str, str] = field(default_factory=dict)
    route_decisions: list[dict[str, Any]] = field(default_factory=list)
    source_revision: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.run_id.strip() or not self.goal_id.strip() or not self.project_id.strip():
            raise ValueError("run_id, goal_id, and project_id are required")
        if self.goal_version < 0:
            raise ValueError("goal_version must be non-negative")
        if self.completed_at and self.status not in TERMINAL_RUN_STATUSES:
            raise ValueError("completed_at is only valid for a terminal run")
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunManifest":
        return cls(
            run_id=str(data.get("run_id", "") or str(uuid.uuid4())),
            goal_id=str(data.get("goal_id", "")),
            project_id=str(data.get("project_id", "default") or "default"),
            goal_version=int(data.get("goal_version", 0) or 0),
            status=RunStatus(str(data.get("status", RunStatus.PENDING.value))),
            organization_id=str(data.get("organization_id", "") or ""),
            organization_version=str(data.get("organization_version", "") or ""),
            configuration_digest=str(data.get("configuration_digest", "") or ""),
            model_versions={str(k): str(v) for k, v in dict(data.get("model_versions", {}) or {}).items()},
            provider_versions={str(k): str(v) for k, v in dict(data.get("provider_versions", {}) or {}).items()},
            skill_versions={str(k): str(v) for k, v in dict(data.get("skill_versions", {}) or {}).items()},
            route_decisions=[dict(item) for item in data.get("route_decisions", []) or [] if isinstance(item, Mapping)],
            source_revision=str(data.get("source_revision", "") or ""),
            started_at=parse_datetime(data.get("started_at")),
            completed_at=parse_datetime(data.get("completed_at")),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(data.get("updated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class RunMetrics(ContractMixin):
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    tokens: int = 0
    interventions: int = 0
    rework_cycles: int = 0
    failed_attempts: int = 0
    total_attempts: int = 0
    resume_attempts: int = 0
    resume_successes: int = 0
    schema_version: int = 1

    def validate(self) -> None:
        for name, value in self.to_dict().items():
            if name == "schema_version":
                continue
            if float(value) < 0:
                raise ValueError(f"run metric {name} must be non-negative")
        if self.failed_attempts > self.total_attempts and self.total_attempts > 0:
            raise ValueError("failed_attempts cannot exceed total_attempts")
        if self.resume_successes > self.resume_attempts:
            raise ValueError("resume_successes cannot exceed resume_attempts")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RunMetrics":
        payload = dict(data or {})
        return cls(
            cost_usd=float(payload.get("cost_usd", 0.0) or 0.0),
            duration_seconds=float(payload.get("duration_seconds", 0.0) or 0.0),
            tokens=int(payload.get("tokens", 0) or 0),
            interventions=int(payload.get("interventions", 0) or 0),
            rework_cycles=int(payload.get("rework_cycles", 0) or 0),
            failed_attempts=int(payload.get("failed_attempts", 0) or 0),
            total_attempts=int(payload.get("total_attempts", 0) or 0),
            resume_attempts=int(payload.get("resume_attempts", 0) or 0),
            resume_successes=int(payload.get("resume_successes", 0) or 0),
            schema_version=int(payload.get("schema_version", 1) or 1),
        )


@dataclass
class CriterionResult(ContractMixin):
    criterion_id: str
    score: float
    passed: bool
    evidence: list[str] = field(default_factory=list)
    notes: str = ""
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CriterionResult":
        return cls(
            criterion_id=str(data.get("criterion_id", "")),
            score=clamp_score(data.get("score", 0.0)),
            passed=bool(data.get("passed", False)),
            evidence=[str(item) for item in data.get("evidence", []) or []],
            notes=str(data.get("notes", "") or ""),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class RoleOutcome(ContractMixin):
    role_id: str
    employee_id: str = ""
    quality_score: float = 0.0
    reliability_score: float = 0.0
    domain_scores: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    accepted_work_items: int = 0
    failed_work_items: int = 0
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoleOutcome":
        return cls(
            role_id=str(data.get("role_id", "")),
            employee_id=str(data.get("employee_id", "") or ""),
            quality_score=clamp_score(data.get("quality_score", 0.0)),
            reliability_score=clamp_score(data.get("reliability_score", 0.0)),
            domain_scores={str(k): clamp_score(v) for k, v in dict(data.get("domain_scores", {}) or {}).items()},
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
            accepted_work_items=int(data.get("accepted_work_items", 0) or 0),
            failed_work_items=int(data.get("failed_work_items", 0) or 0),
            evidence=[str(item) for item in data.get("evidence", []) or []],
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class RunScorecard(ContractMixin):
    run_id: str
    goal_id: str
    project_id: str = "default"
    scorecard_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    gate_status: GateStatus = GateStatus.REVIEW
    total_score: float = 0.0
    quality_score: float = 0.0
    evidence_score: float = 0.0
    budget_score: float = 0.0
    reliability_score: float = 0.0
    autonomy_score: float = 0.0
    criterion_results: list[CriterionResult] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    role_outcomes: list[RoleOutcome] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    baseline_label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @property
    def accepted(self) -> bool:
        return self.gate_status == GateStatus.PASS

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunScorecard":
        return cls(
            scorecard_id=str(data.get("scorecard_id", "") or str(uuid.uuid4())),
            run_id=str(data.get("run_id", "")),
            goal_id=str(data.get("goal_id", "")),
            project_id=str(data.get("project_id", "default") or "default"),
            gate_status=GateStatus(str(data.get("gate_status", GateStatus.REVIEW.value))),
            total_score=clamp_score(data.get("total_score", 0.0)),
            quality_score=clamp_score(data.get("quality_score", 0.0)),
            evidence_score=clamp_score(data.get("evidence_score", 0.0)),
            budget_score=clamp_score(data.get("budget_score", 0.0)),
            reliability_score=clamp_score(data.get("reliability_score", 0.0)),
            autonomy_score=clamp_score(data.get("autonomy_score", 0.0)),
            criterion_results=[
                CriterionResult.from_dict(item)
                for item in data.get("criterion_results", []) or []
                if isinstance(item, Mapping)
            ],
            metrics=RunMetrics.from_dict(data.get("metrics")),
            role_outcomes=[
                RoleOutcome.from_dict(item)
                for item in data.get("role_outcomes", []) or []
                if isinstance(item, Mapping)
            ],
            violations=[str(item) for item in data.get("violations", []) or []],
            warnings=[str(item) for item in data.get("warnings", []) or []],
            baseline_label=str(data.get("baseline_label", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
            evaluated_at=parse_datetime(data.get("evaluated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class OperatingEvent(ContractMixin):
    run_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    aggregate_type: str = "run"
    aggregate_id: str = ""
    aggregate_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = ""
    occurred_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperatingEvent":
        return cls(
            event_id=str(data.get("event_id", "") or str(uuid.uuid4())),
            run_id=str(data.get("run_id", "")),
            event_type=str(data.get("event_type", "")),
            payload=dict(data.get("payload", {}) or {}),
            aggregate_type=str(data.get("aggregate_type", "run") or "run"),
            aggregate_id=str(data.get("aggregate_id", "") or ""),
            aggregate_version=int(data.get("aggregate_version", 1) or 1),
            idempotency_key=str(data.get("idempotency_key", "") or ""),
            occurred_at=parse_datetime(data.get("occurred_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class OutboxMessage(ContractMixin):
    event_id: str
    run_id: str
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: datetime = field(default_factory=utc_now)
    lease_owner: str = ""
    lease_token: int = 0
    lease_expires_at: datetime | None = None
    last_error: str = ""
    created_at: datetime = field(default_factory=utc_now)
    delivered_at: datetime | None = None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutboxMessage":
        return cls(
            message_id=str(data.get("message_id", "") or str(uuid.uuid4())),
            event_id=str(data.get("event_id", "")),
            run_id=str(data.get("run_id", "")),
            topic=str(data.get("topic", "")),
            payload=dict(data.get("payload", {}) or {}),
            status=str(data.get("status", "pending") or "pending"),
            attempts=int(data.get("attempts", 0) or 0),
            max_attempts=int(data.get("max_attempts", 5) or 5),
            next_attempt_at=parse_datetime(data.get("next_attempt_at"), default=utc_now()) or utc_now(),
            lease_owner=str(data.get("lease_owner", "") or ""),
            lease_token=int(data.get("lease_token", 0) or 0),
            lease_expires_at=parse_datetime(data.get("lease_expires_at")),
            last_error=str(data.get("last_error", "") or ""),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            delivered_at=parse_datetime(data.get("delivered_at")),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class RunLease(ContractMixin):
    run_id: str
    owner: str
    fencing_token: int
    expires_at: datetime
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunLease":
        return cls(
            run_id=str(data.get("run_id", "")),
            owner=str(data.get("owner", "")),
            fencing_token=int(data.get("fencing_token", 0) or 0),
            expires_at=parse_datetime(data.get("expires_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(data.get("updated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class LearningAsset(ContractMixin):
    name: str
    kind: str
    content: dict[str, Any]
    organization_id: str = ""
    project_id: str = "default"
    employee_id: str = ""
    role_id: str = ""
    asset_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    status: LearningAssetStatus = LearningAssetStatus.CANDIDATE
    previous_asset_id: str = ""
    source_run_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.asset_id.strip() or not self.name.strip() or not self.kind.strip():
            raise ValueError("asset_id, name, and kind are required")
        if self.version < 1:
            raise ValueError("learning asset version must be at least 1")
        if not 0 <= self.confidence <= 1:
            raise ValueError("learning asset confidence must be between 0 and 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LearningAsset":
        return cls(
            asset_id=str(data.get("asset_id", "") or str(uuid.uuid4())),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            content=dict(data.get("content", {}) or {}),
            organization_id=str(data.get("organization_id", "") or ""),
            project_id=str(data.get("project_id", "default") or "default"),
            employee_id=str(data.get("employee_id", "") or ""),
            role_id=str(data.get("role_id", "") or ""),
            version=int(data.get("version", 1) or 1),
            status=LearningAssetStatus(str(data.get("status", LearningAssetStatus.CANDIDATE.value))),
            previous_asset_id=str(data.get("previous_asset_id", "") or ""),
            source_run_ids=[str(item) for item in data.get("source_run_ids", []) or []],
            confidence=clamp_score(data.get("confidence", 0.0)),
            expires_at=parse_datetime(data.get("expires_at")),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(data.get("updated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class LearningAssetEvaluation(ContractMixin):
    asset_id: str
    phase: str
    score: float
    passed: bool
    evaluation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    baseline_score: float | None = None
    sample_size: int = 0
    evidence: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @property
    def delta(self) -> float | None:
        if self.baseline_score is None:
            return None
        return self.score - self.baseline_score

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LearningAssetEvaluation":
        return cls(
            evaluation_id=str(data.get("evaluation_id", "") or str(uuid.uuid4())),
            asset_id=str(data.get("asset_id", "")),
            phase=str(data.get("phase", "offline") or "offline"),
            score=clamp_score(data.get("score", 0.0)),
            passed=bool(data.get("passed", False)),
            baseline_score=(
                None if data.get("baseline_score") is None else clamp_score(data.get("baseline_score"))
            ),
            sample_size=int(data.get("sample_size", 0) or 0),
            evidence=[str(item) for item in data.get("evidence", []) or []],
            violations=[str(item) for item in data.get("violations", []) or []],
            metadata=dict(data.get("metadata", {}) or {}),
            evaluated_at=parse_datetime(data.get("evaluated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class CapabilityRequest(ContractMixin):
    capability_kind: CapabilityKind
    task_type: str
    project_id: str = "default"
    run_id: str = ""
    prompt: str = ""
    modality: str = "text"
    required_capabilities: list[str] = field(default_factory=list)
    preferred_providers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    max_cost_usd: float | None = None
    allow_live: bool = False
    local_first: bool = True
    require_free: bool = False
    sandboxed_tools: bool = False
    gpu_free_vram_mib: int = 0
    hardware_profile: str = ""
    candidate_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = 1

    def validate(self) -> None:
        if not self.task_type.strip():
            raise ValueError("capability request task_type is required")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative or null")
        if self.gpu_free_vram_mib < 0:
            raise ValueError("gpu_free_vram_mib must be non-negative")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityRequest":
        return cls(
            request_id=str(data.get("request_id", "") or str(uuid.uuid4())),
            capability_kind=CapabilityKind(str(data.get("capability_kind", CapabilityKind.LLM.value))),
            task_type=str(data.get("task_type", "")),
            project_id=str(data.get("project_id", "default") or "default"),
            run_id=str(data.get("run_id", "") or ""),
            prompt=str(data.get("prompt", "") or ""),
            modality=str(data.get("modality", "text") or "text"),
            required_capabilities=[str(item) for item in data.get("required_capabilities", []) or []],
            preferred_providers=[str(item) for item in data.get("preferred_providers", []) or []],
            tags=[str(item) for item in data.get("tags", []) or []],
            max_cost_usd=_optional_float(data.get("max_cost_usd")),
            allow_live=bool(data.get("allow_live", False)),
            local_first=bool(data.get("local_first", True)),
            require_free=bool(data.get("require_free", False)),
            sandboxed_tools=bool(data.get("sandboxed_tools", False)),
            gpu_free_vram_mib=int(data.get("gpu_free_vram_mib", 0) or 0),
            hardware_profile=str(data.get("hardware_profile", "") or ""),
            candidate_id=str(data.get("candidate_id", "") or ""),
            parameters=dict(data.get("parameters", {}) or {}),
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class CapabilityRoute(ContractMixin):
    request_id: str
    capability_kind: CapabilityKind
    provider: str
    candidate_id: str
    mode: str
    allowed: bool
    route_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model: str = ""
    reason: str = ""
    blockers: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float | None = None
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityRoute":
        return cls(
            route_id=str(data.get("route_id", "") or str(uuid.uuid4())),
            request_id=str(data.get("request_id", "")),
            capability_kind=CapabilityKind(str(data.get("capability_kind", CapabilityKind.LLM.value))),
            provider=str(data.get("provider", "")),
            candidate_id=str(data.get("candidate_id", "")),
            mode=str(data.get("mode", "dry_run") or "dry_run"),
            allowed=bool(data.get("allowed", False)),
            model=str(data.get("model", "") or ""),
            reason=str(data.get("reason", "") or ""),
            blockers=[str(item) for item in data.get("blockers", []) or []],
            alternatives=[dict(item) for item in data.get("alternatives", []) or [] if isinstance(item, Mapping)],
            diagnostics=dict(data.get("diagnostics", {}) or {}),
            estimated_cost_usd=_optional_float(data.get("estimated_cost_usd")),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class CapabilityAttempt(ContractMixin):
    request_id: str
    route_id: str
    capability_kind: CapabilityKind
    project_id: str = "default"
    run_id: str = ""
    provider: str = ""
    candidate_id: str = ""
    status: str = "planned"
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    latency_ms: float = 0.0
    cost_usd: float | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityAttempt":
        return cls(
            attempt_id=str(data.get("attempt_id", "") or str(uuid.uuid4())),
            request_id=str(data.get("request_id", "")),
            route_id=str(data.get("route_id", "")),
            capability_kind=CapabilityKind(str(data.get("capability_kind", CapabilityKind.LLM.value))),
            project_id=str(data.get("project_id", "default") or "default"),
            run_id=str(data.get("run_id", "") or ""),
            provider=str(data.get("provider", "") or ""),
            candidate_id=str(data.get("candidate_id", "") or ""),
            status=str(data.get("status", "planned") or "planned"),
            latency_ms=float(data.get("latency_ms", 0.0) or 0.0),
            cost_usd=_optional_float(data.get("cost_usd")),
            error=str(data.get("error", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class StaffingCandidate(ContractMixin):
    employee_id: str
    role_ids: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    availability: float = 1.0
    expected_cost_usd: float = 0.0
    experience_score: float = 0.0
    quality_score: float = 0.5
    reliability_score: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StaffingCandidate":
        return cls(
            employee_id=str(data.get("employee_id", "")),
            role_ids=[str(item) for item in data.get("role_ids", []) or []],
            domains=[str(item) for item in data.get("domains", []) or []],
            availability=clamp_score(data.get("availability", 1.0)),
            expected_cost_usd=float(data.get("expected_cost_usd", 0.0) or 0.0),
            experience_score=max(0.0, float(data.get("experience_score", 0.0) or 0.0)),
            quality_score=clamp_score(data.get("quality_score", 0.5)),
            reliability_score=clamp_score(data.get("reliability_score", 0.5)),
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class StaffingDecision(ContractMixin):
    role_id: str
    selected_employee_id: str
    predicted_score: float
    project_id: str = "default"
    run_id: str = ""
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    required_domains: list[str] = field(default_factory=list)
    component_scores: dict[str, float] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    observed_score: float | None = None
    regret: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StaffingDecision":
        return cls(
            decision_id=str(data.get("decision_id", "") or str(uuid.uuid4())),
            role_id=str(data.get("role_id", "")),
            selected_employee_id=str(data.get("selected_employee_id", "")),
            predicted_score=clamp_score(data.get("predicted_score", 0.0)),
            project_id=str(data.get("project_id", "default") or "default"),
            run_id=str(data.get("run_id", "") or ""),
            required_domains=[str(item) for item in data.get("required_domains", []) or []],
            component_scores={str(k): clamp_score(v) for k, v in dict(data.get("component_scores", {}) or {}).items()},
            alternatives=[dict(item) for item in data.get("alternatives", []) or [] if isinstance(item, Mapping)],
            rationale=[str(item) for item in data.get("rationale", []) or []],
            observed_score=_optional_score(data.get("observed_score")),
            regret=_optional_float(data.get("regret")),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=parse_datetime(data.get("created_at"), default=utc_now()) or utc_now(),
            updated_at=parse_datetime(data.get("updated_at"), default=utc_now()) or utc_now(),
            schema_version=int(data.get("schema_version", 1) or 1),
        )


@dataclass
class MissionAlert(ContractMixin):
    severity: str
    kind: str
    title: str
    detail: str
    run_id: str = ""
    goal_id: str = ""
    action: str = ""
    schema_version: int = 1


@dataclass
class MissionControlSnapshot(ContractMixin):
    project_id: str
    active_goals: int
    active_runs: int
    blocked_runs: int
    failed_gates: int
    pending_outbox: int
    dead_letters: int
    pending_approvals: int
    learning_candidates: int
    promoted_assets: int
    average_score: float
    total_cost_usd: float
    alerts: list[MissionAlert] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _optional_score(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return clamp_score(value)
