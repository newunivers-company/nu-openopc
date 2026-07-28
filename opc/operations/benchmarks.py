"""Versioned, paired outcome benchmarks for Task Mode and Company Mode."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Iterable, Mapping

from opc.operations.models import GateStatus, GoalContract, RunManifest, RunScorecard, RunStatus


DEFAULT_SUITE_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmark_assets"
    / "openopc_core_outcomes_v1.json"
)
_ALLOWED_MODES = {"task", "company"}
_TRUSTED_AUTHORITIES = {"human_confirmed", "independent_judge"}
_CAMPAIGN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


@dataclass(frozen=True)
class OutcomeBenchmarkCase:
    case_id: str
    workload: str
    title: str
    prompt: str
    goal: GoalContract
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutcomeBenchmarkCase":
        goal = GoalContract.from_dict(dict(data.get("goal", {}) or {}))
        case = cls(
            case_id=str(data.get("case_id", "") or "").strip(),
            workload=str(data.get("workload", "") or "").strip(),
            title=str(data.get("title", "") or "").strip(),
            prompt=str(data.get("prompt", "") or "").strip(),
            goal=goal,
            tags=tuple(str(item).strip() for item in data.get("tags", []) or [] if str(item).strip()),
        )
        case.validate()
        return case

    def validate(self) -> None:
        if not self.case_id or not self.workload or not self.title or not self.prompt:
            raise ValueError("benchmark case requires case_id, workload, title, and prompt")
        self.goal.validate()

    def to_dict(self) -> dict[str, Any]:
        goal = self.goal.to_dict()
        # GoalContract timestamps are runtime audit fields. They are populated
        # while parsing and must not make the immutable suite digest depend on
        # wall-clock load time.
        goal.pop("created_at", None)
        goal.pop("updated_at", None)
        return {
            "case_id": self.case_id,
            "workload": self.workload,
            "title": self.title,
            "prompt": self.prompt,
            "goal": goal,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class OutcomeBenchmarkSuite:
    suite_id: str
    version: int
    status: str
    description: str
    repetitions: int
    minimum_paired_samples_per_workload: int
    baseline_mode: str
    candidate_mode: str
    maximum_quality_regression: float
    maximum_success_regression: float
    minimum_quality_improvement: float
    cases: tuple[OutcomeBenchmarkCase, ...]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutcomeBenchmarkSuite":
        policy = dict(data.get("gate_policy", {}) or {})
        suite = cls(
            suite_id=str(data.get("suite_id", "") or "").strip(),
            version=int(data.get("version", 0) or 0),
            status=str(data.get("status", "") or "").strip(),
            description=str(data.get("description", "") or "").strip(),
            repetitions=int(data.get("repetitions", 0) or 0),
            minimum_paired_samples_per_workload=int(
                policy.get("minimum_paired_samples_per_workload", 0) or 0
            ),
            baseline_mode=str(policy.get("baseline_mode", "task") or "task"),
            candidate_mode=str(policy.get("candidate_mode", "company") or "company"),
            maximum_quality_regression=float(
                policy.get("maximum_quality_regression", 0.02) or 0.0
            ),
            maximum_success_regression=float(
                policy.get("maximum_success_regression", 0.02) or 0.0
            ),
            minimum_quality_improvement=float(
                policy.get("minimum_quality_improvement", 0.0) or 0.0
            ),
            cases=tuple(
                OutcomeBenchmarkCase.from_dict(item)
                for item in data.get("cases", []) or []
                if isinstance(item, Mapping)
            ),
            schema_version=int(data.get("schema_version", 1) or 1),
        )
        suite.validate()
        return suite

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("benchmark suite schema_version must be 1")
        if not self.suite_id or self.version < 1 or not self.status or not self.description:
            raise ValueError("benchmark suite identity, version, status, and description are required")
        if self.repetitions < 1:
            raise ValueError("benchmark suite repetitions must be positive")
        if self.minimum_paired_samples_per_workload < 2:
            raise ValueError("benchmark suite requires at least two paired samples per workload")
        if self.baseline_mode not in _ALLOWED_MODES or self.candidate_mode not in _ALLOWED_MODES:
            raise ValueError("benchmark modes must be task or company")
        if self.baseline_mode == self.candidate_mode:
            raise ValueError("benchmark baseline and candidate modes must differ")
        for name, value in (
            ("maximum_quality_regression", self.maximum_quality_regression),
            ("maximum_success_regression", self.maximum_success_regression),
            ("minimum_quality_improvement", self.minimum_quality_improvement),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if not self.cases:
            raise ValueError("benchmark suite requires cases")
        ids = [item.case_id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark case_id values must be unique")
        workload_counts: dict[str, int] = {}
        for case in self.cases:
            workload_counts[case.workload] = workload_counts.get(case.workload, 0) + 1
        for workload, count in workload_counts.items():
            available = count * self.repetitions
            if available < self.minimum_paired_samples_per_workload:
                raise ValueError(
                    f"workload {workload!r} has only {available} possible pairs, below the gate target"
                )

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def workloads(self) -> tuple[str, ...]:
        return tuple(sorted({item.workload for item in self.cases}))

    def case(self, case_id: str) -> OutcomeBenchmarkCase:
        match = next((item for item in self.cases if item.case_id == case_id), None)
        if match is None:
            raise KeyError(f"benchmark case not found: {case_id}")
        return match

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "version": self.version,
            "status": self.status,
            "description": self.description,
            "repetitions": self.repetitions,
            "gate_policy": {
                "baseline_mode": self.baseline_mode,
                "candidate_mode": self.candidate_mode,
                "minimum_paired_samples_per_workload": (
                    self.minimum_paired_samples_per_workload
                ),
                "maximum_quality_regression": self.maximum_quality_regression,
                "maximum_success_regression": self.maximum_success_regression,
                "minimum_quality_improvement": self.minimum_quality_improvement,
            },
            "cases": [item.to_dict() for item in self.cases],
        }


@dataclass(frozen=True)
class OutcomeObservation:
    suite_digest: str
    case_id: str
    mode: str
    repetition: int
    run_id: str
    source: str
    authority: str
    gate_status: str
    score: float
    quality_score: float
    duration_seconds: float
    cost_usd: float | None
    usage_measured: bool
    interventions: int
    rework_cycles: int
    evidence: tuple[str, ...]
    artifact_digest: str
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutcomeObservation":
        cost = data.get("cost_usd")
        observation = cls(
            suite_digest=str(data.get("suite_digest", "") or "").strip(),
            case_id=str(data.get("case_id", "") or "").strip(),
            mode=str(data.get("mode", "") or "").strip(),
            repetition=int(data.get("repetition", 0) or 0),
            run_id=str(data.get("run_id", "") or "").strip(),
            source=str(data.get("source", "") or "").strip(),
            authority=str(data.get("authority", "") or "").strip(),
            gate_status=str(data.get("gate_status", "") or "").strip(),
            score=float(data.get("score", 0.0) or 0.0),
            quality_score=float(data.get("quality_score", data.get("score", 0.0)) or 0.0),
            duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
            cost_usd=None if cost is None else float(cost),
            usage_measured=bool(data.get("usage_measured", False)),
            interventions=int(data.get("interventions", 0) or 0),
            rework_cycles=int(data.get("rework_cycles", 0) or 0),
            evidence=tuple(str(item).strip() for item in data.get("evidence", []) or [] if str(item).strip()),
            artifact_digest=str(data.get("artifact_digest", "") or "").strip(),
            metadata=dict(data.get("metadata", {}) or {}),
            schema_version=int(data.get("schema_version", 1) or 1),
        )
        observation.validate()
        return observation

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("outcome observation schema_version must be 1")
        if not self.suite_digest or not self.case_id or not self.run_id:
            raise ValueError("outcome observation requires suite_digest, case_id, and run_id")
        if self.mode not in _ALLOWED_MODES:
            raise ValueError("outcome observation mode must be task or company")
        if self.repetition < 1:
            raise ValueError("outcome observation repetition must be positive")
        if self.gate_status not in {item.value for item in GateStatus}:
            raise ValueError("outcome observation gate_status is invalid")
        if not 0 <= self.score <= 1 or not 0 <= self.quality_score <= 1:
            raise ValueError("outcome observation scores must be between 0 and 1")
        if self.duration_seconds < 0 or (self.cost_usd is not None and self.cost_usd < 0):
            raise ValueError("outcome observation duration and cost must be non-negative")
        if self.interventions < 0 or self.rework_cycles < 0:
            raise ValueError("outcome observation intervention counts must be non-negative")

    @property
    def accepted(self) -> bool:
        return self.gate_status == GateStatus.PASS.value

    @property
    def trusted(self) -> bool:
        return (
            self.source == "actual_run"
            and self.authority in _TRUSTED_AUTHORITIES
            and bool(self.evidence)
            and _is_sha256(self.artifact_digest)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"evidence": list(self.evidence)}


def load_suite(path: Path = DEFAULT_SUITE_PATH) -> OutcomeBenchmarkSuite:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("benchmark suite must be a JSON object")
    return OutcomeBenchmarkSuite.from_dict(raw)


def load_observations(path: Path) -> list[OutcomeObservation]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(rows, list):
        raise ValueError("benchmark observations must be a JSON array or JSONL")
    return [
        OutcomeObservation.from_dict(item)
        for item in rows
        if isinstance(item, Mapping)
    ]


def observation_from_run(
    suite: OutcomeBenchmarkSuite,
    *,
    case_id: str,
    mode: str,
    repetition: int,
    manifest: RunManifest,
    scorecard: RunScorecard,
    authority: str,
    artifact_digest: str,
    campaign_id: str = "",
) -> OutcomeObservation:
    suite.case(case_id)
    campaign = str(campaign_id or "").strip()
    if campaign and not _CAMPAIGN_ID.fullmatch(campaign):
        raise ValueError("benchmark campaign_id has invalid characters")
    if manifest.status != RunStatus.COMPLETED:
        raise ValueError("benchmark observations require a completed run manifest")
    if scorecard.run_id != manifest.run_id or scorecard.goal_id != manifest.goal_id:
        raise ValueError("benchmark scorecard identity does not match its run")
    evidence = tuple(
        dict.fromkeys(
            item
            for result in scorecard.criterion_results
            for item in result.evidence
            if str(item).strip()
        )
    )
    measured = bool(scorecard.metadata.get("usage_measured", True))
    return OutcomeObservation(
        suite_digest=suite.digest,
        case_id=case_id,
        mode=mode,
        repetition=repetition,
        run_id=manifest.run_id,
        source="actual_run",
        authority=authority,
        gate_status=scorecard.gate_status.value,
        score=scorecard.total_score,
        quality_score=scorecard.quality_score,
        duration_seconds=scorecard.metrics.duration_seconds,
        cost_usd=scorecard.metrics.cost_usd if measured else None,
        usage_measured=measured,
        interventions=scorecard.metrics.interventions,
        rework_cycles=scorecard.metrics.rework_cycles,
        evidence=evidence,
        artifact_digest=artifact_digest,
        metadata={
            "goal_id": manifest.goal_id,
            "goal_version": manifest.goal_version,
            "source_revision": manifest.source_revision,
            "scorecard_id": scorecard.scorecard_id,
            "campaign_id": campaign,
            # Execution-environment pins for reproducibility audits: which
            # exact configuration and models produced this observation.
            "configuration_digest": manifest.configuration_digest,
            "model_versions": dict(manifest.model_versions)
            or dict(scorecard.metadata.get("model_versions", {}) or {}),
        },
    )


def build_campaign_plan(
    suite: OutcomeBenchmarkSuite,
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Build a deterministic, counterbalanced execution matrix."""

    campaign = str(campaign_id or "").strip()
    if not _CAMPAIGN_ID.fullmatch(campaign):
        raise ValueError(
            "benchmark campaign_id must be 1-64 safe filename characters"
        )
    pair_rows: list[tuple[str, OutcomeBenchmarkCase, int]] = []
    for case in suite.cases:
        for repetition in range(1, suite.repetitions + 1):
            pair_id = f"{case.case_id}-r{repetition}"
            order_key = hashlib.sha256(
                f"{suite.digest}:{campaign}:{pair_id}".encode("utf-8")
            ).hexdigest()
            pair_rows.append((order_key, case, repetition))
    pair_rows.sort(key=lambda item: (item[0], item[1].case_id, item[2]))

    slots: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    sequence = 0
    for pair_index, (_, case, repetition) in enumerate(pair_rows):
        pair_id = f"{case.case_id}-r{repetition}"
        first_mode = (
            suite.baseline_mode
            if pair_index % 2 == 0
            else suite.candidate_mode
        )
        second_mode = (
            suite.candidate_mode
            if first_mode == suite.baseline_mode
            else suite.baseline_mode
        )
        pair_slot_ids: list[str] = []
        for mode in (first_mode, second_mode):
            sequence += 1
            slot_id = f"{case.case_id}/{mode}/{repetition}"
            pair_slot_ids.append(slot_id)
            run_id = f"benchmark-{campaign}-{case.case_id}-r{repetition}-{mode}"
            goal = dict(case.to_dict()["goal"])
            goal["goal_id"] = (
                f"{case.goal.goal_id}-{campaign}-r{repetition}-{mode}"
            )
            slots.append(
                {
                    "sequence": sequence,
                    "slot_id": slot_id,
                    "pair_id": pair_id,
                    "case_id": case.case_id,
                    "workload": case.workload,
                    "mode": mode,
                    "repetition": repetition,
                    "run_id": run_id,
                    "title": case.title,
                    "prompt": case.prompt,
                    "goal": goal,
                    "artifact_directory": (
                        f"artifacts/{campaign}/{pair_id}/{mode}"
                    ),
                    "required_authorities": sorted(_TRUSTED_AUTHORITIES),
                }
            )
        pairs.append(
            {
                "pair_id": pair_id,
                "case_id": case.case_id,
                "workload": case.workload,
                "repetition": repetition,
                "execution_order": [first_mode, second_mode],
                "slot_ids": pair_slot_ids,
            }
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign,
        "suite_id": suite.suite_id,
        "suite_version": suite.version,
        "suite_digest": suite.digest,
        "baseline_mode": suite.baseline_mode,
        "candidate_mode": suite.candidate_mode,
        "counterbalance": {
            "strategy": "digest_order_alternating_first_mode",
            "baseline_first_pairs": sum(
                item["execution_order"][0] == suite.baseline_mode
                for item in pairs
            ),
            "candidate_first_pairs": sum(
                item["execution_order"][0] == suite.candidate_mode
                for item in pairs
            ),
        },
        "pair_count": len(pairs),
        "slot_count": len(slots),
        "pairs": pairs,
        "slots": slots,
    }
    report["plan_digest"] = _canonical_digest(report)
    return report


def campaign_progress(
    suite: OutcomeBenchmarkSuite,
    observations: Iterable[OutcomeObservation],
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Reconcile one campaign plan with collected observations."""

    plan = build_campaign_plan(suite, campaign_id=campaign_id)
    expected = {
        (
            str(item["case_id"]),
            str(item["mode"]),
            int(item["repetition"]),
        ): item
        for item in plan["slots"]
    }
    accepted: dict[tuple[str, str, int], OutcomeObservation] = {}
    duplicate_slots: list[str] = []
    foreign_rows: list[str] = []
    untrusted_slots: list[str] = []
    all_rows = list(observations)
    for row in all_rows:
        row_campaign = str(row.metadata.get("campaign_id", "") or "").strip()
        key = (row.case_id, row.mode, row.repetition)
        if row_campaign != campaign_id or row.suite_digest != suite.digest:
            foreign_rows.append(row.run_id)
            continue
        if key not in expected:
            foreign_rows.append(row.run_id)
            continue
        slot_id = str(expected[key]["slot_id"])
        if key in accepted:
            duplicate_slots.append(slot_id)
            continue
        accepted[key] = row
        if not row.trusted:
            untrusted_slots.append(slot_id)

    missing = [
        item
        for key, item in expected.items()
        if key not in accepted
    ]
    workload_progress: dict[str, dict[str, Any]] = {}
    trusted_pairs = 0
    completed_pairs = 0
    for workload in suite.workloads:
        workload_pairs = [
            item for item in plan["pairs"] if item["workload"] == workload
        ]
        completed = 0
        trusted = 0
        for pair in workload_pairs:
            case_id = str(pair["case_id"])
            repetition = int(pair["repetition"])
            baseline = accepted.get(
                (case_id, suite.baseline_mode, repetition)
            )
            candidate = accepted.get(
                (case_id, suite.candidate_mode, repetition)
            )
            if baseline is not None and candidate is not None:
                completed += 1
                if baseline.trusted and candidate.trusted:
                    trusted += 1
        completed_pairs += completed
        trusted_pairs += trusted
        workload_progress[workload] = {
            "expected_pairs": len(workload_pairs),
            "completed_pairs": completed,
            "trusted_pairs": trusted,
            "minimum_trusted_pairs": (
                suite.minimum_paired_samples_per_workload
            ),
        }
    trusted_rows = [item for item in accepted.values() if item.trusted]
    gate = evaluate_outcomes(suite, accepted.values())
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "plan_digest": plan["plan_digest"],
        "suite_digest": suite.digest,
        "expected_slots": len(expected),
        "observed_slots": len(accepted),
        "trusted_slots": len(trusted_rows),
        "missing_slot_count": len(missing),
        "missing_slots": [
            {
                key: item[key]
                for key in (
                    "sequence",
                    "slot_id",
                    "pair_id",
                    "case_id",
                    "workload",
                    "mode",
                    "repetition",
                    "run_id",
                )
            }
            for item in missing
        ],
        "untrusted_slots": sorted(set(untrusted_slots)),
        "duplicate_slots": sorted(set(duplicate_slots)),
        "foreign_run_ids": sorted(set(foreign_rows)),
        "expected_pairs": int(plan["pair_count"]),
        "completed_pairs": completed_pairs,
        "trusted_pairs": trusted_pairs,
        "workloads": workload_progress,
        "collection_complete": not missing and not duplicate_slots,
        "promotion_eligible": bool(gate["promotion_eligible"]),
        "status": (
            "pass"
            if gate["promotion_eligible"]
            else "collecting"
            if missing
            else "blocked"
        ),
        "gate_blockers": gate["blockers"],
    }


def evaluate_outcomes(
    suite: OutcomeBenchmarkSuite,
    observations: Iterable[OutcomeObservation],
) -> dict[str, Any]:
    rows = list(observations)
    blockers: list[str] = []
    slots: dict[tuple[str, str, int], OutcomeObservation] = {}
    known_cases = {item.case_id for item in suite.cases}
    for row in rows:
        if row.suite_digest != suite.digest:
            blockers.append(f"run {row.run_id} references a different suite digest")
            continue
        if row.case_id not in known_cases:
            blockers.append(f"run {row.run_id} references unknown case {row.case_id}")
            continue
        if row.repetition > suite.repetitions:
            blockers.append(
                f"run {row.run_id} repetition {row.repetition} exceeds suite repetitions"
            )
            continue
        key = (row.case_id, row.mode, row.repetition)
        if key in slots:
            blockers.append(
                f"duplicate benchmark slot {row.case_id}/{row.mode}/{row.repetition}; retries are not independent samples"
            )
            continue
        slots[key] = row
        if not row.trusted:
            blockers.append(
                f"run {row.run_id} lacks actual-run, independent authority, evidence, or artifact digest"
            )

    workload_reports: dict[str, dict[str, Any]] = {}
    all_pairs: list[tuple[OutcomeObservation, OutcomeObservation]] = []
    for workload in suite.workloads:
        workload_cases = [item for item in suite.cases if item.workload == workload]
        pairs: list[tuple[OutcomeObservation, OutcomeObservation]] = []
        for case in workload_cases:
            for repetition in range(1, suite.repetitions + 1):
                baseline = slots.get((case.case_id, suite.baseline_mode, repetition))
                candidate = slots.get((case.case_id, suite.candidate_mode, repetition))
                if baseline is not None and candidate is not None and baseline.trusted and candidate.trusted:
                    pairs.append((baseline, candidate))
        report = _paired_report(suite, pairs)
        workload_reports[workload] = report
        all_pairs.extend(pairs)
        if report["paired_samples"] < suite.minimum_paired_samples_per_workload:
            blockers.append(
                f"workload {workload} has {report['paired_samples']} trusted pairs; "
                f"{suite.minimum_paired_samples_per_workload} required"
            )
        if not report["quality_gate_passed"]:
            blockers.append(f"workload {workload} failed the quality non-regression gate")
        if not report["success_gate_passed"]:
            blockers.append(f"workload {workload} failed the success non-regression gate")

    unique_blockers = list(dict.fromkeys(blockers))
    overall = _paired_report(suite, all_pairs)
    promotion_eligible = not unique_blockers and bool(workload_reports)
    return {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "suite_version": suite.version,
        "suite_digest": suite.digest,
        "suite_status": suite.status,
        "baseline_mode": suite.baseline_mode,
        "candidate_mode": suite.candidate_mode,
        "observation_count": len(rows),
        "trusted_pair_count": len(all_pairs),
        "promotion_eligible": promotion_eligible,
        "product_claim_ready": promotion_eligible,
        "status": "pass" if promotion_eligible else "blocked",
        "blockers": unique_blockers,
        "workloads": workload_reports,
        "overall": overall,
    }


def _paired_report(
    suite: OutcomeBenchmarkSuite,
    pairs: list[tuple[OutcomeObservation, OutcomeObservation]],
) -> dict[str, Any]:
    quality_deltas = [candidate.quality_score - baseline.quality_score for baseline, candidate in pairs]
    score_deltas = [candidate.score - baseline.score for baseline, candidate in pairs]
    baseline_success = fmean(float(item[0].accepted) for item in pairs) if pairs else 0.0
    candidate_success = fmean(float(item[1].accepted) for item in pairs) if pairs else 0.0
    duration_deltas = [
        candidate.duration_seconds - baseline.duration_seconds
        for baseline, candidate in pairs
    ]
    intervention_deltas = [
        candidate.interventions - baseline.interventions
        for baseline, candidate in pairs
    ]
    measured_cost_pairs = [
        (baseline, candidate)
        for baseline, candidate in pairs
        if baseline.usage_measured
        and candidate.usage_measured
        and baseline.cost_usd is not None
        and candidate.cost_usd is not None
    ]
    cost_deltas = [
        float(candidate.cost_usd) - float(baseline.cost_usd)
        for baseline, candidate in measured_cost_pairs
    ]
    quality_ci = _mean_confidence_interval(quality_deltas)
    quality_gate = (
        bool(pairs)
        and quality_ci["lower"] >= -suite.maximum_quality_regression
        and quality_ci["mean"] >= suite.minimum_quality_improvement
    )
    # Repetitions of the same case are correlated, so the per-pair CI above
    # can be over-confident. Cluster deltas by case (mean per case, then CI
    # over case means) and report it alongside; the gate stays on the
    # per-pair CI so existing gate semantics are unchanged.
    per_case: dict[str, list[float]] = defaultdict(list)
    for (baseline, candidate), delta in zip(pairs, quality_deltas):
        per_case[baseline.case_id].append(delta)
    case_means = [fmean(values) for _, values in sorted(per_case.items())]
    case_clustered = _mean_confidence_interval(case_means)
    case_clustered["cases"] = len(case_means)
    success_delta = candidate_success - baseline_success
    return {
        "paired_samples": len(pairs),
        "baseline_success_rate": round(baseline_success, 6),
        "candidate_success_rate": round(candidate_success, 6),
        "success_rate_delta": round(success_delta, 6),
        "success_gate_passed": (
            bool(pairs) and success_delta >= -suite.maximum_success_regression
        ),
        "mean_total_score_delta": round(fmean(score_deltas), 6) if score_deltas else 0.0,
        "quality_delta": quality_ci,
        "quality_delta_case_clustered": case_clustered,
        "quality_gate_passed": quality_gate,
        "mean_duration_seconds_delta": (
            round(fmean(duration_deltas), 6) if duration_deltas else 0.0
        ),
        "mean_intervention_delta": (
            round(fmean(intervention_deltas), 6) if intervention_deltas else 0.0
        ),
        "measured_cost_pairs": len(measured_cost_pairs),
        "mean_cost_usd_delta": round(fmean(cost_deltas), 6) if cost_deltas else None,
    }


def _mean_confidence_interval(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "lower": -1.0, "upper": 1.0}
    mean = fmean(values)
    if len(values) == 1:
        return {
            "mean": round(mean, 6),
            "lower": round(mean, 6),
            "upper": round(mean, 6),
        }
    margin = 1.96 * stdev(values) / math.sqrt(len(values))
    return {
        "mean": round(mean, 6),
        "lower": round(max(-1.0, mean - margin), 6),
        "upper": round(min(1.0, mean + margin), 6),
    }


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return (
        len(normalized) == 64
        and all(char in "0123456789abcdef" for char in normalized)
    )
