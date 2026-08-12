"""Evidence-based staffing recommendations and post-run regret accounting."""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any, Mapping, Sequence

from opc.core.config import StaffingOperationsConfig
from opc.operations.models import (
    RoleOutcome,
    StaffingCandidate,
    StaffingDecision,
    clamp_score,
)
from opc.operations.repository import OperationsRepository


class StaffingOptimizer:
    """Rank employees from declared fit plus persisted role outcomes."""

    def __init__(
        self,
        repository: OperationsRepository,
        config: StaffingOperationsConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or StaffingOperationsConfig()

    async def recommend(
        self,
        *,
        role_id: str,
        candidates: Sequence[StaffingCandidate | Mapping[str, Any]],
        required_domains: Sequence[str] | None = None,
        project_id: str = "default",
        run_id: str = "",
        max_cost_usd: float | None = None,
        persist: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> StaffingDecision:
        clean_role = str(role_id or "").strip()
        if not clean_role:
            raise ValueError("role_id is required")
        parsed = [
            item if isinstance(item, StaffingCandidate) else StaffingCandidate.from_dict(item)
            for item in candidates
        ]
        parsed = [item for item in parsed if item.employee_id.strip()]
        if not parsed:
            raise ValueError(f"no staffing candidates supplied for role {clean_role!r}")
        if max_cost_usd is not None and max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative or null")
        supplied_count = len(parsed)
        excluded_over_budget = [
            item.employee_id
            for item in parsed
            if max_cost_usd is not None and item.expected_cost_usd > max_cost_usd
        ]
        if max_cost_usd is not None:
            parsed = [item for item in parsed if item.expected_cost_usd <= max_cost_usd]
        if not parsed:
            raise ValueError(
                f"no staffing candidate for role {clean_role!r} fits the cost ceiling"
            )
        domains = list(dict.fromkeys(str(item).strip().lower() for item in required_domains or [] if str(item).strip()))
        history = await self._historical_outcomes(
            project_id=project_id,
            role_id=clean_role,
        )
        scored: list[tuple[float, str, StaffingCandidate, dict[str, float], int]] = []
        for candidate in parsed:
            outcomes = history.get(candidate.employee_id, [])
            enriched = self._components(
                candidate,
                outcomes=outcomes,
                role_id=clean_role,
                required_domains=domains,
                max_cost_usd=max_cost_usd,
            )
            components, evidence_count = enriched
            weights = self.config.normalized_weights()
            predicted = clamp_score(sum(components[name] * weights[name] for name in weights))
            scored.append((predicted, candidate.employee_id, candidate, components, evidence_count))
        scored.sort(key=lambda item: (-item[0], item[1]))
        predicted, _, selected, components, evidence_count = scored[0]
        alternatives = [
            {
                "employee_id": candidate.employee_id,
                "predicted_score": score,
                "component_scores": component_scores,
                "historical_evidence_count": count,
            }
            for score, _, candidate, component_scores, count in scored[1:]
        ]
        rationale = self._rationale(
            selected,
            role_id=clean_role,
            required_domains=domains,
            components=components,
            evidence_count=evidence_count,
        )
        decision = StaffingDecision(
            role_id=clean_role,
            selected_employee_id=selected.employee_id,
            predicted_score=predicted,
            project_id=str(project_id or "default"),
            run_id=str(run_id or ""),
            required_domains=domains,
            component_scores=components,
            alternatives=alternatives,
            rationale=rationale,
            metadata={
                "weights": self.config.normalized_weights(),
                "historical_evidence_count": evidence_count,
                "candidate_count": len(scored),
                "supplied_candidate_count": supplied_count,
                "excluded_over_budget": excluded_over_budget,
                "decision_type": "evidence_based_recommendation",
                **dict(metadata or {}),
            },
        )
        if persist:
            await self.repository.save_staffing_decision(decision)
        return decision

    async def recommend_from_employee_pool(
        self,
        *,
        role_id: str,
        employees: Sequence[Any],
        summaries: Sequence[Mapping[str, Any]] | None = None,
        required_domains: Sequence[str] | None = None,
        project_id: str = "default",
        run_id: str = "",
        persist: bool = True,
    ) -> StaffingDecision:
        summaries_by_id = {
            str(item.get("employee_id", "")): dict(item)
            for item in summaries or []
            if str(item.get("employee_id", "")).strip()
        }
        candidates: list[StaffingCandidate] = []
        for employee in employees:
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if not employee_id:
                continue
            summary = summaries_by_id.get(employee_id, {})
            employee_metadata = dict(getattr(employee, "metadata", {}) or {})
            role_ids = list(summary.get("role_ids", []) or [])
            if not role_ids:
                role_ids = [str(getattr(employee, "role_id", "") or "")]
            candidates.append(
                StaffingCandidate(
                    employee_id=employee_id,
                    role_ids=[str(item) for item in role_ids if str(item).strip()],
                    domains=[str(item) for item in list(getattr(employee, "domains", []) or [])],
                    availability=clamp_score(employee_metadata.get("availability", 1.0)),
                    expected_cost_usd=max(
                        0.0,
                        float(employee_metadata.get("expected_cost_usd", 0.0) or 0.0),
                    ),
                    experience_score=max(
                        0.0,
                        float(summary.get("experience_score", 0.0) or 0.0),
                    ),
                    quality_score=clamp_score(employee_metadata.get("quality_score", 0.5)),
                    reliability_score=clamp_score(employee_metadata.get("reliability_score", 0.5)),
                    metadata={
                        "name": str(getattr(employee, "name", "") or ""),
                        "category": str(getattr(employee, "category", "") or ""),
                    },
                )
            )
        return await self.recommend(
            role_id=role_id,
            candidates=candidates,
            required_domains=required_domains,
            project_id=project_id,
            run_id=run_id,
            persist=persist,
            metadata={"source": "company_recruiter_employee_pool"},
        )

    async def observe(
        self,
        decision_id: str,
        *,
        observed_score: float,
        alternative_observed_scores: Mapping[str, float] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> StaffingDecision:
        decision = await self.repository.get_staffing_decision(decision_id)
        if decision is None:
            raise KeyError(f"staffing decision not found: {decision_id}")
        observed = clamp_score(observed_score)
        observed_alternatives = {
            str(key): clamp_score(value)
            for key, value in dict(alternative_observed_scores or {}).items()
        }
        if observed_alternatives:
            best_counterfactual = max(observed_alternatives.values())
            regret_source = "observed_counterfactual"
        else:
            best_counterfactual = max(
                (float(item.get("predicted_score", 0.0) or 0.0) for item in decision.alternatives),
                default=observed,
            )
            regret_source = "predicted_counterfactual"
        decision.observed_score = observed
        decision.regret = max(0.0, best_counterfactual - observed)
        decision.metadata.update(
            {
                "regret_source": regret_source,
                "best_counterfactual_score": best_counterfactual,
                "alternative_observed_scores": observed_alternatives,
                **dict(metadata or {}),
            }
        )
        return await self.repository.save_staffing_decision(decision)

    async def observe_from_scorecard(
        self,
        decision_id: str,
        *,
        run_id: str,
    ) -> StaffingDecision:
        decision = await self.repository.get_staffing_decision(decision_id)
        if decision is None:
            raise KeyError(f"staffing decision not found: {decision_id}")
        scorecard = await self.repository.get_scorecard(run_id)
        if scorecard is None:
            raise KeyError(f"run scorecard not found: {run_id}")
        outcome = next(
            (
                item
                for item in scorecard.role_outcomes
                if item.role_id == decision.role_id
                and item.employee_id == decision.selected_employee_id
            ),
            None,
        )
        if outcome is None:
            raise KeyError(
                f"scorecard {run_id} has no role outcome for "
                f"{decision.role_id}/{decision.selected_employee_id}"
            )
        observed = (0.7 * outcome.quality_score) + (0.3 * outcome.reliability_score)
        return await self.observe(
            decision_id,
            observed_score=observed,
            metadata={"observed_from_run_id": run_id},
        )

    async def _historical_outcomes(
        self,
        *,
        project_id: str,
        role_id: str,
    ) -> dict[str, list[RoleOutcome]]:
        scorecards = await self.repository.list_scorecards(project_id=project_id, limit=1000)
        grouped: dict[str, list[RoleOutcome]] = {}
        for scorecard in scorecards:
            for outcome in scorecard.role_outcomes:
                if outcome.role_id != role_id or not outcome.employee_id:
                    continue
                grouped.setdefault(outcome.employee_id, []).append(outcome)
        return grouped

    @staticmethod
    def _components(
        candidate: StaffingCandidate,
        *,
        outcomes: Sequence[RoleOutcome],
        role_id: str,
        required_domains: Sequence[str],
        max_cost_usd: float | None,
    ) -> tuple[dict[str, float], int]:
        if outcomes:
            quality = fmean(item.quality_score for item in outcomes)
            reliability = fmean(item.reliability_score for item in outcomes)
        else:
            quality = candidate.quality_score
            reliability = candidate.reliability_score
        candidate_domains = {item.strip().lower() for item in candidate.domains if item.strip()}
        required_set = set(required_domains)
        declared_domain_fit = (
            len(required_set & candidate_domains) / len(required_set)
            if required_set
            else 1.0
        )
        historical_domain_values = [
            item.domain_scores[domain]
            for item in outcomes
            for domain in required_set
            if domain in item.domain_scores
        ]
        historical_domain_fit = (
            fmean(historical_domain_values)
            if historical_domain_values
            else declared_domain_fit
        )
        role_fit = 1.0 if role_id in set(candidate.role_ids) else 0.5
        domain = clamp_score((0.6 * declared_domain_fit) + (0.25 * historical_domain_fit) + (0.15 * role_fit))
        experience = clamp_score(math.log1p(candidate.experience_score) / math.log1p(20.0))
        availability = clamp_score(candidate.availability)
        if max_cost_usd is None:
            cost = 1.0 / (1.0 + max(0.0, candidate.expected_cost_usd))
        elif max_cost_usd == 0:
            cost = 1.0 if candidate.expected_cost_usd == 0 else 0.0
        else:
            cost = clamp_score(1.0 - (candidate.expected_cost_usd / max_cost_usd))
        return (
            {
                "quality": clamp_score(quality),
                "domain": domain,
                "reliability": clamp_score(reliability),
                "experience": experience,
                "availability": availability,
                "cost": clamp_score(cost),
            },
            len(outcomes),
        )

    @staticmethod
    def _rationale(
        candidate: StaffingCandidate,
        *,
        role_id: str,
        required_domains: Sequence[str],
        components: Mapping[str, float],
        evidence_count: int,
    ) -> list[str]:
        strongest = sorted(components.items(), key=lambda item: (-item[1], item[0]))[:2]
        lines = [
            f"selected {candidate.employee_id} for role {role_id}",
            "strongest factors: " + ", ".join(f"{name}={score:.3f}" for name, score in strongest),
            f"historical role outcomes used: {evidence_count}",
        ]
        if required_domains:
            lines.append("required domains: " + ", ".join(required_domains))
        if evidence_count == 0:
            lines.append("cold-start estimate; collect a role outcome before treating this rank as stable")
        return lines
