"""Quality and transport gates for bounded LLM shadow experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping

from opc.operations.benchmarks import _mean_confidence_interval


_TRUSTED_AUTHORITIES = {"human_confirmed", "independent_judge"}


@dataclass(frozen=True)
class ShadowQualityObservation:
    experiment_id: str
    decision_id: str
    request_fingerprint: str
    workload: str
    served_provider: str
    served_model: str
    challenger_provider: str
    challenger_model: str
    served_ok: bool
    challenger_ok: bool
    served_quality: float
    challenger_quality: float
    served_latency_ms: float
    challenger_latency_ms: float
    actual_run: bool
    authority: str
    judge_revision: str
    evidence: tuple[str, ...]
    served_artifact_digest: str = ""
    challenger_artifact_digest: str = ""
    violations: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShadowQualityObservation":
        row = cls(
            experiment_id=str(data.get("experiment_id", "") or "").strip(),
            decision_id=str(data.get("decision_id", "") or "").strip(),
            request_fingerprint=str(data.get("request_fingerprint", "") or "").strip().lower(),
            workload=str(data.get("workload", "") or "").strip(),
            served_provider=str(data.get("served_provider", "") or "").strip(),
            served_model=str(data.get("served_model", "") or "").strip(),
            challenger_provider=str(data.get("challenger_provider", "") or "").strip(),
            challenger_model=str(data.get("challenger_model", "") or "").strip(),
            served_ok=bool(data.get("served_ok", False)),
            challenger_ok=bool(data.get("challenger_ok", False)),
            served_quality=float(data.get("served_quality", 0.0) or 0.0),
            challenger_quality=float(data.get("challenger_quality", 0.0) or 0.0),
            served_latency_ms=float(data.get("served_latency_ms", 0.0) or 0.0),
            challenger_latency_ms=float(data.get("challenger_latency_ms", 0.0) or 0.0),
            actual_run=bool(data.get("actual_run", False)),
            authority=str(data.get("authority", "") or "").strip(),
            judge_revision=str(data.get("judge_revision", "") or "").strip(),
            evidence=tuple(str(item).strip() for item in data.get("evidence", []) or [] if str(item).strip()),
            served_artifact_digest=str(
                data.get("served_artifact_digest", "") or ""
            ).strip().lower(),
            challenger_artifact_digest=str(
                data.get("challenger_artifact_digest", "") or ""
            ).strip().lower(),
            violations=tuple(str(item).strip() for item in data.get("violations", []) or [] if str(item).strip()),
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )
        row.validate()
        return row

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("shadow observation schema_version must be 1")
        required = {
            "experiment_id": self.experiment_id,
            "decision_id": self.decision_id,
            "workload": self.workload,
            "served_provider": self.served_provider,
            "served_model": self.served_model,
            "challenger_provider": self.challenger_provider,
            "challenger_model": self.challenger_model,
            "judge_revision": self.judge_revision,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"shadow observation missing required fields: {missing}")
        if (
            len(self.request_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in self.request_fingerprint)
        ):
            raise ValueError("request_fingerprint must be a SHA-256 hex digest")
        if self.served_provider == self.challenger_provider and self.served_model == self.challenger_model:
            raise ValueError("served and challenger route identities must differ")
        for name, value in (
            ("served_quality", self.served_quality),
            ("challenger_quality", self.challenger_quality),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.served_latency_ms < 0 or self.challenger_latency_ms < 0:
            raise ValueError("shadow latencies must be non-negative")

    @property
    def trusted(self) -> bool:
        return (
            self.actual_run
            and self.authority in _TRUSTED_AUTHORITIES
            and bool(self.evidence)
            and _is_sha256(self.served_artifact_digest)
            and _is_sha256(self.challenger_artifact_digest)
            and not self.violations
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "evidence": list(self.evidence),
            "violations": list(self.violations),
        }


def load_shadow_observations(path: Path) -> list[ShadowQualityObservation]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    rows = json.loads(text) if text.startswith("[") else [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]
    if not isinstance(rows, list):
        raise ValueError("shadow observations must be a JSON array or JSONL")
    return [
        ShadowQualityObservation.from_dict(item)
        for item in rows
        if isinstance(item, Mapping)
    ]


def evaluate_shadow_promotion(
    observations: Iterable[ShadowQualityObservation],
    *,
    experiment_id: str,
    minimum_samples_per_workload: int = 30,
    maximum_quality_regression: float = 0.02,
    maximum_success_regression: float = 0.02,
    minimum_quality_improvement: float = 0.0,
    transport_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if minimum_samples_per_workload < 2:
        raise ValueError("minimum_samples_per_workload must be at least 2")
    for name, value in (
        ("maximum_quality_regression", maximum_quality_regression),
        ("maximum_success_regression", maximum_success_regression),
        ("minimum_quality_improvement", minimum_quality_improvement),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    experiment = str(experiment_id or "").strip()
    if not experiment:
        raise ValueError("experiment_id is required")

    rows = list(observations)
    blockers: list[str] = []
    trusted: list[ShadowQualityObservation] = []
    seen: set[str] = set()
    for row in rows:
        if row.experiment_id != experiment:
            blockers.append(
                f"decision {row.decision_id} belongs to experiment {row.experiment_id!r}"
            )
            continue
        if row.decision_id in seen:
            blockers.append(f"duplicate decision_id: {row.decision_id}")
            continue
        seen.add(row.decision_id)
        if not row.trusted:
            blockers.append(
                f"decision {row.decision_id} lacks actual independent evidence or has violations"
            )
            continue
        trusted.append(row)

    by_workload: dict[str, dict[str, Any]] = {}
    for workload in sorted({item.workload for item in rows}):
        workload_rows = [item for item in trusted if item.workload == workload]
        report = _shadow_workload_report(
            workload_rows,
            maximum_quality_regression=maximum_quality_regression,
            maximum_success_regression=maximum_success_regression,
            minimum_quality_improvement=minimum_quality_improvement,
        )
        by_workload[workload] = report
        if len(workload_rows) < minimum_samples_per_workload:
            blockers.append(
                f"workload {workload} has {len(workload_rows)} trusted samples; "
                f"{minimum_samples_per_workload} required"
            )
        if not report["quality_gate_passed"]:
            blockers.append(f"workload {workload} failed quality gate")
        if not report["success_gate_passed"]:
            blockers.append(f"workload {workload} failed success gate")
    if not by_workload:
        blockers.append("no workload has trusted shadow quality observations")

    transport = dict(transport_report or {})
    if transport:
        for item in transport.get("blockers", []) or []:
            blockers.append(f"transport:{item}")
        transport_decision_ids = {
            str(item).strip()
            for item in transport.get("decision_ids", []) or []
            if str(item).strip()
        }
        if not transport_decision_ids:
            blockers.append("transport report lacks immutable decision identities")
        missing_transport = sorted(
            item.decision_id
            for item in trusted
            if item.decision_id not in transport_decision_ids
        )
        if missing_transport:
            blockers.append(
                "transport report is missing trusted decisions: "
                + ", ".join(missing_transport)
            )
        if int(transport.get("decisions_with_shadow", 0) or 0) < len(trusted):
            blockers.append(
                "transport evidence covers fewer shadow decisions than the trusted quality set"
            )
    else:
        blockers.append("content-free transport report is required")

    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": 1,
        "experiment_id": experiment,
        "observation_count": len(rows),
        "trusted_observation_count": len(trusted),
        "minimum_samples_per_workload": minimum_samples_per_workload,
        "promotion_ready": not unique_blockers,
        "status": "pass" if not unique_blockers else "blocked",
        "blockers": unique_blockers,
        "workloads": by_workload,
        "transport": transport,
    }


def shadow_transport_report(
    database_path: Path,
    *,
    minimum_decisions: int = 20,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise FileNotFoundError(f"shadow event database not found: {database_path}")
    from nu_llm_routing_lib.shadow_serving_report import (
        build_shadow_serving_report,
    )
    from nu_llm_routing_lib.sqlite_event_store import SQLiteEventStore

    store = SQLiteEventStore(database_path)
    snapshot = store.snapshot()
    report = build_shadow_serving_report(
        snapshot.all_events(),
        minimum_decisions=minimum_decisions,
    ).to_dict()
    inventory = _shadow_transport_inventory(snapshot)
    return {
        **report,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "decision_ids": [
            item["decision_id"] for item in inventory
        ],
        "decisions": inventory,
    }


def shadow_review_queue(
    database_path: Path,
    *,
    experiment_id: str,
    observations: Iterable[ShadowQualityObservation] = (),
) -> dict[str, Any]:
    """Build a privacy-preserving queue of transport-bound decisions to judge."""

    experiment = str(experiment_id or "").strip()
    if not experiment:
        raise ValueError("experiment_id is required")
    from nu_llm_routing_lib.sqlite_event_store import SQLiteEventStore

    snapshot = SQLiteEventStore(database_path).snapshot()
    inventory = _shadow_transport_inventory(snapshot)
    observed = {
        row.decision_id
        for row in observations
        if row.experiment_id == experiment
    }
    items = [
        {
            **item,
            "experiment_id": experiment,
            "review_state": (
                "observed"
                if item["decision_id"] in observed
                else "ready"
                if item["review_ready"]
                else "blocked_transport"
            ),
            "required_artifacts": [
                "served response artifact and SHA-256",
                "challenger response artifact and SHA-256",
                "independent or human judge evidence",
                "judge revision",
            ],
        }
        for item in inventory
    ]
    return {
        "schema_version": 1,
        "experiment_id": experiment,
        "transport_snapshot_sha256": snapshot.snapshot_sha256,
        "decision_count": len(items),
        "ready_count": sum(
            item["review_state"] == "ready" for item in items
        ),
        "observed_count": sum(
            item["review_state"] == "observed" for item in items
        ),
        "blocked_transport_count": sum(
            item["review_state"] == "blocked_transport" for item in items
        ),
        "items": items,
    }


def bind_shadow_observation_to_transport(
    observation: ShadowQualityObservation,
    database_path: Path,
    *,
    latency_tolerance_ms: float = 1.0,
) -> ShadowQualityObservation:
    """Validate claimed quality evidence against immutable routing events."""

    if not _is_sha256(observation.served_artifact_digest):
        raise ValueError("served_artifact_digest must be a SHA-256 digest")
    if not _is_sha256(observation.challenger_artifact_digest):
        raise ValueError("challenger_artifact_digest must be a SHA-256 digest")
    from nu_llm_routing_lib.sqlite_event_store import SQLiteEventStore

    snapshot = SQLiteEventStore(database_path).snapshot()
    match = next(
        (
            item
            for item in _shadow_transport_inventory(snapshot)
            if item["decision_id"] == observation.decision_id
        ),
        None,
    )
    if match is None:
        raise ValueError(
            f"shadow decision is absent from transport ledger: {observation.decision_id}"
        )
    if not match["review_ready"]:
        raise ValueError("shadow decision has incomplete transport evidence")
    claimed = {
        "request_fingerprint": observation.request_fingerprint,
        "workload": observation.workload,
        "served_provider": observation.served_provider,
        "served_model": observation.served_model,
        "challenger_provider": observation.challenger_provider,
        "challenger_model": observation.challenger_model,
    }
    expected = {
        "request_fingerprint": match["request_fingerprint"],
        "workload": match["workload"],
        "served_provider": match["served"]["provider"],
        "served_model": match["served"]["model"],
        "challenger_provider": match["challenger"]["provider"],
        "challenger_model": match["challenger"]["model"],
    }
    mismatches = [
        name
        for name, value in expected.items()
        if value and claimed[name] != value
    ]
    if mismatches:
        raise ValueError(
            "shadow observation does not match transport fields: "
            + ", ".join(mismatches)
        )
    tolerance = max(0.0, float(latency_tolerance_ms))
    for name, claimed_latency, expected_latency in (
        (
            "served_latency_ms",
            observation.served_latency_ms,
            float(match["served"]["latency_ms"]),
        ),
        (
            "challenger_latency_ms",
            observation.challenger_latency_ms,
            float(match["challenger"]["latency_ms"]),
        ),
    ):
        if abs(claimed_latency - expected_latency) > tolerance:
            raise ValueError(
                f"{name} differs from transport ledger by more than {tolerance} ms"
            )
    return ShadowQualityObservation.from_dict(
        observation.to_dict()
        | {
            "metadata": {
                **observation.metadata,
                "transport_snapshot_sha256": snapshot.snapshot_sha256,
                "transport_bound": True,
            }
        }
    )


def _shadow_workload_report(
    rows: list[ShadowQualityObservation],
    *,
    maximum_quality_regression: float,
    maximum_success_regression: float,
    minimum_quality_improvement: float,
) -> dict[str, Any]:
    quality_deltas = [
        item.challenger_quality - item.served_quality for item in rows
    ]
    quality = _mean_confidence_interval(quality_deltas)
    served_success = fmean(float(item.served_ok) for item in rows) if rows else 0.0
    challenger_success = (
        fmean(float(item.challenger_ok) for item in rows) if rows else 0.0
    )
    success_delta = challenger_success - served_success
    return {
        "samples": len(rows),
        "served_success_rate": round(served_success, 6),
        "challenger_success_rate": round(challenger_success, 6),
        "success_rate_delta": round(success_delta, 6),
        "success_gate_passed": (
            bool(rows) and success_delta >= -maximum_success_regression
        ),
        "quality_delta": quality,
        "quality_gate_passed": (
            bool(rows)
            and quality["lower"] >= -maximum_quality_regression
            and quality["mean"] >= minimum_quality_improvement
        ),
        "served_mean_latency_ms": (
            round(fmean(item.served_latency_ms for item in rows), 3)
            if rows
            else 0.0
        ),
        "challenger_mean_latency_ms": (
            round(fmean(item.challenger_latency_ms for item in rows), 3)
            if rows
            else 0.0
        ),
    }


def _shadow_transport_inventory(snapshot: Any) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for event in snapshot.all_events():
        payload = event.to_dict()
        decision_id = str(payload.get("decision_id", "") or "")
        if decision_id not in grouped:
            grouped[decision_id] = []
            order.append(decision_id)
        grouped[decision_id].append(payload)
    rows: list[dict[str, Any]] = []
    for decision_id in order:
        events = grouped[decision_id]
        decision = next(
            (
                item
                for item in events
                if item.get("event_type") == "routing_decision"
            ),
            {},
        )
        outcomes = [
            item
            for item in events
            if item.get("event_type") == "routing_outcome"
        ]
        served_candidates = [
            item for item in outcomes if bool(item.get("terminal", False))
        ]
        shadow_candidates = [
            item
            for item in outcomes
            if str(item.get("remediation_action", "") or "") == "shadow"
        ]
        served = served_candidates[-1] if served_candidates else {}
        challenger = shadow_candidates[-1] if shadow_candidates else {}
        context = dict(decision.get("context", {}) or {})
        workload = str(
            context.get("workload")
            or context.get("task_type")
            or context.get("matched_profile_id")
            or "unclassified"
        )
        rows.append(
            {
                "decision_id": decision_id,
                "request_fingerprint": str(
                    decision.get("request_fingerprint", "") or ""
                ),
                "workload": workload,
                "occurred_at": str(decision.get("occurred_at", "") or ""),
                "served": _transport_route(served),
                "challenger": _transport_route(challenger),
                "review_ready": bool(decision and served and challenger),
                "transport_blockers": [
                    message
                    for condition, message in (
                        (not decision, "missing routing decision"),
                        (not served, "missing terminal served outcome"),
                        (not challenger, "missing shadow outcome"),
                    )
                    if condition
                ],
            }
        )
    return rows


def _transport_route(value: Mapping[str, Any]) -> dict[str, Any]:
    if not value:
        return {}
    return {
        "provider": str(value.get("provider", "") or ""),
        "model": str(value.get("model_revision", "") or ""),
        "transport_ok": bool(value.get("transport_ok", False)),
        "contract_ok": value.get("contract_ok"),
        "latency_ms": round(
            float(value.get("latency_s", 0.0) or 0.0) * 1000,
            6,
        ),
        "event_id": str(value.get("event_id", "") or ""),
    }


def _is_sha256(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return (
        len(normalized) == 64
        and all(char in "0123456789abcdef" for char in normalized)
    )
