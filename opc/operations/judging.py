"""LLM pre-screening with mandatory human confirmation for judgment work.

The evidence gates in :mod:`opc.operations.benchmarks` and
:mod:`opc.operations.experiments` only trust ``human_confirmed`` /
``independent_judge`` authority. This module speeds that judgment up
without weakening it: an LLM drafts rubric-based criterion scores with
rationales, and a human reviews, adjusts, and *confirms* the draft. Only
the confirmation step may mint a human authority label — drafts are
explicitly marked ``authority: llm_draft`` and are rejected by every
trusted-evidence gate until confirmed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from opc.operations.models import utc_now

DRAFT_AUTHORITY = "llm_draft"
_CONFIRMABLE_AUTHORITIES = {"human_confirmed", "independent_judge"}


@dataclass
class JudgmentRubric:
    """Sealed rubric an LLM draft is produced against."""

    rubric_id: str
    criteria: list[dict[str, Any]]
    instructions: str = (
        "Score each criterion between 0.0 and 1.0 against the artifacts. "
        "Justify every score with concrete references to the artifact text. "
        "Do not reward confident language without evidence."
    )
    schema_version: int = 1

    @classmethod
    def from_goal(cls, goal: Mapping[str, Any]) -> "JudgmentRubric":
        criteria = [
            {
                "criterion_id": str(item["criterion_id"]),
                "description": str(item.get("description", "")),
                "minimum_score": float(item.get("minimum_score", 0.0) or 0.0),
            }
            for item in goal.get("acceptance_criteria", []) or []
        ]
        if not criteria:
            raise ValueError("judgment rubric requires at least one criterion")
        return cls(
            rubric_id=str(goal.get("goal_id", "") or "rubric"),
            criteria=criteria,
        )

    @property
    def digest(self) -> str:
        payload = {
            "rubric_id": self.rubric_id,
            "criteria": self.criteria,
            "instructions": self.instructions,
            "schema_version": self.schema_version,
        }
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()


@dataclass
class DraftJudgment:
    """An LLM-produced draft. Never trusted evidence on its own."""

    run_id: str
    rubric_digest: str
    judge_model: str
    criterion_scores: dict[str, float]
    criterion_notes: dict[str, str]
    authority: str = DRAFT_AUTHORITY
    created_at: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "rubric_digest": self.rubric_digest,
            "judge_model": self.judge_model,
            "criterion_scores": dict(self.criterion_scores),
            "criterion_notes": dict(self.criterion_notes),
            "authority": self.authority,
            "created_at": self.created_at,
        }


def build_draft_prompt(
    rubric: JudgmentRubric,
    *,
    artifacts: Mapping[str, str],
    max_artifact_chars: int = 24_000,
) -> list[dict[str, str]]:
    """Build the deterministic chat messages for a draft-scoring call."""

    sections: list[str] = []
    used = 0
    for name in sorted(artifacts):
        body = str(artifacts[name])
        remaining = max_artifact_chars - used
        if remaining <= 0:
            sections.append(f"### {name}\n[truncated: artifact budget exhausted]")
            continue
        clipped = body[:remaining]
        used += len(clipped)
        suffix = "" if clipped == body else "\n[truncated]"
        sections.append(f"### {name}\n{clipped}{suffix}")
    criteria_lines = "\n".join(
        f"- {item['criterion_id']} (minimum {item['minimum_score']}): "
        f"{item['description']}"
        for item in rubric.criteria
    )
    user = (
        f"{rubric.instructions}\n\n"
        f"## Criteria\n{criteria_lines}\n\n"
        f"## Artifacts\n" + "\n\n".join(sections) + "\n\n"
        "Respond with ONLY a JSON object of the form "
        '{"criterion_scores": {"<criterion_id>": <0.0-1.0>}, '
        '"criterion_notes": {"<criterion_id>": "<grounded rationale>"}} '
        "covering every criterion exactly once."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a strict, evidence-grounded evaluation assistant. "
                "Your output is a DRAFT for a human judge and carries no "
                "authority by itself."
            ),
        },
        {"role": "user", "content": user},
    ]


def parse_draft_response(
    rubric: JudgmentRubric,
    *,
    run_id: str,
    judge_model: str,
    response_text: str,
) -> DraftJudgment:
    """Parse and validate the LLM draft; fail closed on malformed output."""

    text = str(response_text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("draft response contains no JSON object")
    payload = json.loads(text[start : end + 1])
    raw_scores = payload.get("criterion_scores")
    if not isinstance(raw_scores, Mapping):
        raise ValueError("draft response lacks criterion_scores")
    expected = {item["criterion_id"] for item in rubric.criteria}
    scores: dict[str, float] = {}
    for criterion_id in expected:
        if criterion_id not in raw_scores:
            raise ValueError(f"draft response misses criterion: {criterion_id}")
        value = float(raw_scores[criterion_id])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"draft score out of range for {criterion_id}: {value}")
        scores[criterion_id] = value
    notes_raw = payload.get("criterion_notes")
    notes = {
        criterion_id: str(notes_raw.get(criterion_id, ""))
        for criterion_id in expected
    } if isinstance(notes_raw, Mapping) else {criterion_id: "" for criterion_id in expected}
    return DraftJudgment(
        run_id=run_id,
        rubric_digest=rubric.digest,
        judge_model=judge_model,
        criterion_scores=scores,
        criterion_notes=notes,
    )


def confirm_draft(
    draft: Mapping[str, Any],
    *,
    operator_id: str,
    authority: str = "human_confirmed",
    adjusted_scores: Mapping[str, float] | None = None,
    adjusted_notes: Mapping[str, str] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a reviewed draft into an ``evaluate score`` result payload.

    The confirming human owns the final scores. The draft's provenance
    (model, rubric digest, original scores) is preserved in metadata so a
    reviewer can always audit what the LLM suggested versus what the human
    confirmed.
    """

    operator = str(operator_id or "").strip()
    if not operator:
        raise ValueError("confirmation requires a non-empty operator_id")
    if authority not in _CONFIRMABLE_AUTHORITIES:
        raise ValueError(
            "confirmation authority must be human_confirmed or independent_judge"
        )
    if str(draft.get("authority", "")) != DRAFT_AUTHORITY:
        raise ValueError("only llm_draft judgments can be confirmed")
    base_scores = dict(draft.get("criterion_scores", {}) or {})
    if not base_scores:
        raise ValueError("draft has no criterion scores to confirm")
    final_scores: dict[str, float] = {**base_scores, **dict(adjusted_scores or {})}
    unknown = set(final_scores) - set(base_scores)
    if unknown:
        raise ValueError(f"adjusted scores reference unknown criteria: {sorted(unknown)}")
    for criterion_id, value in final_scores.items():
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"confirmed score out of range for {criterion_id}")
        final_scores[criterion_id] = value
    notes = {**dict(draft.get("criterion_notes", {}) or {}), **dict(adjusted_notes or {})}
    return {
        "criterion_scores": final_scores,
        "criterion_notes": {key: str(value) for key, value in notes.items()},
        "evidence": dict(evidence or {}),
        "metadata": {
            "judgment_authority": authority,
            "confirmed_by": operator,
            "confirmed_at": utc_now().isoformat(),
            "llm_draft": {
                "judge_model": str(draft.get("judge_model", "")),
                "rubric_digest": str(draft.get("rubric_digest", "")),
                "draft_scores": base_scores,
                "adjusted": sorted(
                    key
                    for key in final_scores
                    if float(final_scores[key]) != float(base_scores.get(key, -1))
                ),
            },
        },
    }


async def draft_for_goal(
    chat: Any,
    *,
    goal: Mapping[str, Any],
    artifacts: Mapping[str, str],
    run_id: str,
    judge_model: str,
    max_artifact_chars: int = 24_000,
) -> DraftJudgment:
    """Produce one draft via an injected async ``chat(user, system) -> str``.

    Shared by the single-run and campaign-batch CLI paths so both produce
    identical, rubric-sealed drafts.
    """

    rubric = JudgmentRubric.from_goal(goal)
    messages = build_draft_prompt(
        rubric, artifacts=artifacts, max_artifact_chars=max_artifact_chars
    )
    response = await chat(messages[1]["content"], messages[0]["content"])
    return parse_draft_response(
        rubric,
        run_id=run_id,
        judge_model=judge_model,
        response_text=str(response or ""),
    )


def cohens_kappa(labels_a: list[Any], labels_b: list[Any]) -> float:
    """Cohen's kappa over two aligned categorical label sequences."""

    if len(labels_a) != len(labels_b):
        raise ValueError("label sequences must be the same length")
    if not labels_a:
        raise ValueError("kappa requires at least one label")
    total = len(labels_a)
    observed = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / total
    categories = set(labels_a) | set(labels_b)
    expected = sum(
        (labels_a.count(category) / total) * (labels_b.count(category) / total)
        for category in categories
    )
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def judgment_agreement(
    result_a: Mapping[str, Any],
    result_b: Mapping[str, Any],
    *,
    minimum_scores: Mapping[str, float],
) -> dict[str, Any]:
    """Inter-judge agreement over two confirmed results for the same run.

    Labels each criterion pass/fail against its rubric minimum per judge,
    then reports raw agreement, Cohen's kappa, and score deltas — evidence
    for how trustworthy the ``independent_judge`` authority actually is.
    """

    scores_a = dict(result_a.get("criterion_scores", {}) or {})
    scores_b = dict(result_b.get("criterion_scores", {}) or {})
    criteria = sorted(set(minimum_scores) & set(scores_a) & set(scores_b))
    if not criteria:
        raise ValueError("no shared criteria between the two results and the rubric")
    labels_a = [
        float(scores_a[criterion]) >= float(minimum_scores[criterion])
        for criterion in criteria
    ]
    labels_b = [
        float(scores_b[criterion]) >= float(minimum_scores[criterion])
        for criterion in criteria
    ]
    deltas = [
        abs(float(scores_a[criterion]) - float(scores_b[criterion]))
        for criterion in criteria
    ]
    disagreements = [
        criterion
        for criterion, a, b in zip(criteria, labels_a, labels_b)
        if a != b
    ]
    return {
        "criteria": criteria,
        "pass_fail_agreement_rate": round(
            sum(1 for a, b in zip(labels_a, labels_b) if a == b) / len(criteria), 6
        ),
        "cohens_kappa": round(cohens_kappa(labels_a, labels_b), 6),
        "mean_abs_score_delta": round(sum(deltas) / len(deltas), 6),
        "max_abs_score_delta": round(max(deltas), 6),
        "disagreements": disagreements,
    }
