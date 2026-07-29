"""Deterministic paired experiments for Self-Grown operating assets."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from opc.operations.models import LearningAsset


_EXPERIMENT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
RELEASE_EXPERIMENT_COHORT_KEY = "learning_release_experiment_cohort"


def build_release_playbook_experiment(
    asset: LearningAsset,
    *,
    experiment_id: str,
    pairs: int = 5,
) -> dict[str, Any]:
    """Seal a treated/control evaluation plan without activating or promoting it.

    The supplied asset must already have source-run provenance. The plan only
    describes future runs and their exact activation metadata; it never changes
    the asset lifecycle.
    """

    clean_experiment_id = str(experiment_id or "").strip()
    if not _EXPERIMENT_ID.fullmatch(clean_experiment_id):
        raise ValueError(
            "experiment_id must be 1-64 characters using letters, digits, ., _, or -"
        )
    if pairs < 3:
        raise ValueError("release playbook experiments require at least 3 pairs")
    asset.validate()
    if not asset.content:
        raise ValueError("release playbook asset content must not be empty")
    if not asset.source_run_ids:
        raise ValueError(
            "release playbook experiments require source run provenance"
        )

    content_digest = _canonical_digest(asset.content)
    slots: list[dict[str, Any]] = []
    for pair_index in range(1, pairs + 1):
        cohort = f"{clean_experiment_id}/pair-{pair_index:02d}"
        ordered_arms = (
            ("control", "treated")
            if pair_index % 2
            else ("treated", "control")
        )
        for order, arm in enumerate(ordered_arms, start=1):
            treated = arm == "treated"
            slots.append(
                {
                    "slot_id": f"{clean_experiment_id}-p{pair_index:02d}-{arm}",
                    "pair_index": pair_index,
                    "execution_order": order,
                    "arm": arm,
                    "metadata": {
                        "learning_release_experiment_id": clean_experiment_id,
                        RELEASE_EXPERIMENT_COHORT_KEY: cohort,
                        "learning_experiment_arm": arm,
                        "learning_activation": {
                            "assets": (
                                [
                                    {
                                        "asset_id": asset.asset_id,
                                        "version": asset.version,
                                        "content_digest": content_digest,
                                    }
                                ]
                                if treated
                                else []
                            )
                        },
                    },
                }
            )

    plan: dict[str, Any] = {
        "schema_version": 1,
        "experiment_id": clean_experiment_id,
        "experiment_type": "paired_release_playbook_effectiveness",
        "objective": (
            "Measure whether the pinned release-evidence playbook reduces "
            "missing evidence and rework without lowering accepted outcome quality."
        ),
        "automatic_promotion": False,
        "asset": {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "kind": asset.kind,
            "version": asset.version,
            "status_at_plan_time": asset.status.value,
            "content_digest": content_digest,
            "source_run_ids": sorted(set(asset.source_run_ids)),
        },
        "design": {
            "pair_count": pairs,
            "slot_count": len(slots),
            "cohort_metadata_key": RELEASE_EXPERIMENT_COHORT_KEY,
            "assignment": (
                "Each pair uses the same sealed release-package fixture. "
                "Treated/control execution order alternates by pair."
            ),
            "hold_constant": [
                "goal contract and acceptance criteria",
                "sealed release-package input fixture",
                "execution mode, provider, model, and budget",
                "independent judgment rubric and authority policy",
            ],
            "contamination_controls": [
                "Control runs must have an empty learning_activation.assets list.",
                "Treated runs must pin the exact asset id, version, and content digest.",
                "Use an isolated execution project or workspace for every slot.",
                "Do not reuse treated output, feedback, or context in a control slot.",
            ],
        },
        "outcomes": {
            "primary": [
                {
                    "metric": "missing_required_evidence_count",
                    "direction": "lower",
                    "source": "trusted scorecard criterion evidence and violations",
                },
                {
                    "metric": "mean_rework_cycles",
                    "direction": "lower",
                    "source": "run scorecard metrics",
                },
                {
                    "metric": "success_rate",
                    "direction": "non_regression",
                    "maximum_regression": 0.02,
                    "source": "persisted passing scorecards",
                },
            ],
            "secondary": [
                "quality_score",
                "evidence_score",
                "mean_interventions",
                "mean_duration_seconds",
                "mean_cost_usd",
            ],
        },
        "evidence_gates": {
            "minimum_samples_per_arm": pairs,
            "all_pairs_require_both_arms": True,
            "trusted_judgment_required": True,
            "maximum_quality_regression": 0.02,
            "minimum_quality_improvement": 0.0,
            "promotion_requires_separate_lifecycle_evaluations": [
                "offline",
                "shadow",
                "canary",
            ],
        },
        "next_command": (
            f"opc ops learning effectiveness {asset.asset_id} "
            f"--cohort-key {RELEASE_EXPERIMENT_COHORT_KEY} "
            f"--minimum-samples {pairs} --maximum-regression 0.02"
        ),
        "slots": slots,
    }
    plan["plan_digest"] = _canonical_digest(plan)
    return plan


def verify_release_playbook_experiment(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Reject experiment plans edited after their digest was sealed."""

    payload = dict(plan)
    claimed_digest = str(payload.pop("plan_digest", "") or "")
    if claimed_digest != _canonical_digest(payload):
        raise ValueError(
            "release playbook experiment digest mismatch: plan was modified after sealing"
        )
    if payload.get("automatic_promotion") is not False:
        raise ValueError("release playbook experiment must disable automatic promotion")
    return dict(plan)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
