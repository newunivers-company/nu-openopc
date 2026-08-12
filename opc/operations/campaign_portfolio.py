"""Repository-derived benchmark campaign cockpit for Mission Control."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from opc.operations.models import GateStatus, RunManifest, RunScorecard, RunStatus


_TRUSTED_AUTHORITIES = {"human_confirmed", "independent_judge"}


def build_campaign_portfolio(
    manifests: Sequence[RunManifest],
    scorecards: Sequence[RunScorecard],
) -> dict[str, Any]:
    """Summarize campaign execution and judgment without promotion authority.

    Observation ledgers remain the authority for promotion. This view is
    deliberately repository-derived so Mission Control can still tell an
    operator exactly which bounded action is safe next.
    """

    scorecards_by_run = {item.run_id: item for item in scorecards}
    grouped: dict[str, list[RunManifest]] = defaultdict(list)
    for manifest in manifests:
        campaign_id = str(
            manifest.metadata.get("benchmark_campaign_id", "") or ""
        ).strip()
        if campaign_id:
            grouped[campaign_id].append(manifest)

    campaigns = [
        _campaign_summary(campaign_id, rows, scorecards_by_run)
        for campaign_id, rows in grouped.items()
    ]
    campaigns.sort(
        key=lambda item: (
            str(item.get("latest_activity_at", "")),
            str(item["campaign_id"]),
        ),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "campaign_count": len(campaigns),
        "active_campaign": campaigns[0] if campaigns else None,
        "campaigns": campaigns,
        "promotion_authority": False,
    }


def _campaign_summary(
    campaign_id: str,
    manifests: Sequence[RunManifest],
    scorecards_by_run: Mapping[str, RunScorecard],
) -> dict[str, Any]:
    expected_slots = max(
        [
            int(
                item.metadata.get("benchmark_expected_slots", 0)
                or 0
            )
            for item in manifests
        ]
        + [len(manifests)]
    )
    workloads: dict[str, dict[str, Any]] = {}
    pair_arms: dict[tuple[str, str, int], dict[str, bool]] = defaultdict(dict)
    completed = failed = in_flight = judged = accepted = trusted = 0
    latest_activity = ""
    for manifest in manifests:
        scorecard = scorecards_by_run.get(manifest.run_id)
        workload = str(
            manifest.metadata.get("benchmark_workload", "")
            or manifest.metadata.get("benchmark_case_id", "")
            or "unclassified"
        ).strip()
        case_id = str(
            manifest.metadata.get("benchmark_case_id", "") or "unknown"
        ).strip()
        mode = str(manifest.metadata.get("benchmark_mode", "") or "").strip()
        repetition = int(
            manifest.metadata.get("benchmark_repetition", 0) or 0
        )
        stats = workloads.setdefault(
            workload,
            {
                "started": 0,
                "completed": 0,
                "judged": 0,
                "trusted_judgments": 0,
                "trusted_pairs": 0,
            },
        )
        stats["started"] += 1
        if manifest.status == RunStatus.COMPLETED:
            completed += 1
            stats["completed"] += 1
        elif manifest.status == RunStatus.FAILED:
            failed += 1
        elif manifest.status in {
            RunStatus.PENDING,
            RunStatus.RUNNING,
            RunStatus.BLOCKED,
        }:
            in_flight += 1
        if scorecard is not None:
            judged += 1
            stats["judged"] += 1
            accepted += int(scorecard.gate_status == GateStatus.PASS)
            is_trusted = _scorecard_is_trusted(scorecard)
            trusted += int(is_trusted)
            stats["trusted_judgments"] += int(is_trusted)
            if case_id and mode in {"task", "company"} and repetition > 0:
                pair_arms[(workload, case_id, repetition)][mode] = is_trusted
        timestamps = [
            value.isoformat()
            for value in (
                manifest.completed_at,
                manifest.updated_at,
                manifest.started_at,
            )
            if value is not None
        ]
        if timestamps:
            latest_activity = max(latest_activity, *timestamps)

    for (workload, _case_id, _repetition), arms in pair_arms.items():
        if arms.get("task") and arms.get("company"):
            workloads[workload]["trusted_pairs"] += 1
    trusted_pairs = sum(
        int(item["trusted_pairs"]) for item in workloads.values()
    )
    awaiting_judgment = max(0, completed - judged)
    untrusted_judgments = max(0, judged - trusted)
    not_started = max(0, expected_slots - len(manifests))
    missing_canary_workloads = sorted(
        workload
        for workload, stats in workloads.items()
        if int(stats["trusted_pairs"]) < 1
    )
    blockers: list[str] = []
    if failed:
        blockers.append("failed")
    if in_flight:
        blockers.append("in_flight")
    if awaiting_judgment:
        blockers.append("awaiting_judgment")
    if untrusted_judgments:
        blockers.append("untrusted_judgment")
    if blockers:
        phase = "blocked"
        next_pair_budget = 0
        next_action = "Resolve run and judgment blockers before starting another pair."
    elif missing_canary_workloads:
        phase = "canary_collection"
        next_pair_budget = int(not_started > 0)
        next_action = (
            "Run one complete Task/Company canary pair for: "
            + ", ".join(missing_canary_workloads)
        )
    elif not_started:
        phase = "bounded_expansion"
        next_pair_budget = 1
        next_action = "Run exactly one complete pair, then reassess the gate."
    else:
        phase = "evidence_review"
        next_pair_budget = 0
        next_action = "Reconcile the observation ledger and review the promotion dossier."
    return {
        "campaign_id": campaign_id,
        "suite_digest": str(
            manifests[0].metadata.get("benchmark_suite_digest", "") or ""
        ),
        "latest_activity_at": latest_activity,
        "expected_slots": expected_slots,
        "started": len(manifests),
        "not_started": not_started,
        "completed": completed,
        "failed": failed,
        "in_flight": in_flight,
        "judged": judged,
        "accepted": accepted,
        "trusted_judgments": trusted,
        "awaiting_judgment": awaiting_judgment,
        "untrusted_judgments": untrusted_judgments,
        "trusted_pairs": trusted_pairs,
        "workloads": dict(sorted(workloads.items())),
        "batch_expansion": {
            "phase": phase,
            "expansion_ready": (
                not blockers and not missing_canary_workloads and not_started > 0
            ),
            "next_pair_budget": next_pair_budget,
            "maximum_pairs_per_batch": 1,
            "missing_canary_workloads": missing_canary_workloads,
            "blockers": blockers,
            "next_action": next_action,
            "promotion_authority": False,
            "source": "repository_operational_estimate",
        },
    }


def _scorecard_is_trusted(scorecard: RunScorecard) -> bool:
    authority = str(
        scorecard.metadata.get("judgment_authority", "") or ""
    ).strip()
    artifact_digest = str(
        scorecard.metadata.get("benchmark_artifact_digest", "") or ""
    ).strip()
    evidence_complete = bool(scorecard.criterion_results) and all(
        bool(item.evidence) for item in scorecard.criterion_results
    )
    return bool(
        authority in _TRUSTED_AUTHORITIES
        and len(artifact_digest) == 64
        and evidence_complete
    )
