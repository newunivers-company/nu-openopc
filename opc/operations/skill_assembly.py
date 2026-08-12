"""Deterministic goal-to-role assembly over the installed local skill catalog."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from opc.layer5_memory.skill_library import Skill, SkillLibrary


_TOKEN_RE = re.compile(r"[a-z0-9가-힣][a-z0-9가-힣_+.-]*", re.IGNORECASE)
_STOP_WORDS = {
    "and",
    "for",
    "from",
    "into",
    "the",
    "this",
    "that",
    "with",
    "role",
    "work",
    "task",
    "using",
    "및",
    "위한",
    "작업",
    "역할",
}


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(str(value or ""))
        if len(token) > 1 and token.lower() not in _STOP_WORDS
    }


def _list_field(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value or [] if str(item).strip()]


def _skill_digest(skill: Skill) -> str:
    return hashlib.sha256(skill.content.encode("utf-8")).hexdigest()


class SkillAssemblyService:
    """Recommend installed skills without installing or mutating role config."""

    def __init__(self, skill_library: SkillLibrary | None = None) -> None:
        self.skill_library = skill_library

    def bind(self, skill_library: SkillLibrary) -> None:
        self.skill_library = skill_library

    def recommend(
        self,
        *,
        goal: str,
        roles: Sequence[Mapping[str, Any]],
        required_capabilities: Sequence[str] | None = None,
        project_id: str = "default",
        max_additions_per_role: int = 4,
    ) -> dict[str, Any]:
        normalized_goal = str(goal or "").strip()
        if not normalized_goal:
            raise ValueError("skill assembly goal is required")
        if self.skill_library is None:
            raise RuntimeError("skill assembly has no bound local skill library")
        self.skill_library.load_all(project_id)
        catalog = sorted(self.skill_library.list_skills(), key=lambda item: item.name)
        required = list(
            dict.fromkeys(
                str(item).strip()
                for item in required_capabilities or []
                if str(item).strip()
            )
        )
        catalog_rows = [self._catalog_row(item) for item in catalog]
        normalized_roles = [
            dict(role)
            for role in roles
            if str(role.get("role_id") or role.get("id") or "").strip()
        ]
        assignments = self._assign_required_capabilities(
            normalized_roles,
            required,
        )
        proposals = [
            self._recommend_role(
                normalized_goal,
                role,
                catalog_rows,
                assignments.get(
                    str(role.get("role_id") or role.get("id") or "").strip(),
                    [],
                ),
                max(1, min(int(max_additions_per_role), 12)),
            )
            for role in normalized_roles
        ]
        coverage = self._organization_coverage(
            required,
            proposals,
            catalog_rows,
        )
        catalog_payload = [
            {
                "name": row["name"],
                "content_digest": row["content_digest"],
                "capabilities": row["capabilities"],
            }
            for row in catalog_rows
        ]
        return {
            "schema_version": 1,
            "project_id": project_id,
            "goal": normalized_goal,
            "catalog_digest": hashlib.sha256(
                json.dumps(
                    catalog_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "catalog_size": len(catalog_rows),
            "required_capabilities": required,
            "organization_capability_coverage": coverage,
            "organization_capability_gaps": [
                capability
                for capability, role_ids in coverage.items()
                if not role_ids
            ],
            "roles": proposals,
            "mutations_applied": False,
            "operator_note": (
                "Review the evidence and apply chosen skill_refs explicitly; this "
                "recommendation never installs skills or changes a role."
            ),
        }

    @staticmethod
    def _assign_required_capabilities(
        roles: list[dict[str, Any]],
        required: list[str],
    ) -> dict[str, list[str]]:
        assignments: dict[str, list[str]] = {
            str(role.get("role_id") or role.get("id") or "").strip(): []
            for role in roles
        }
        for capability in required:
            capability_tokens = _tokens(capability)
            candidates: list[tuple[int, str]] = []
            for role in roles:
                role_id = str(
                    role.get("role_id") or role.get("id") or ""
                ).strip()
                declared = [
                    *_list_field(role.get("capabilities", [])),
                    *_list_field(role.get("required_capabilities", [])),
                ]
                explicit = any(
                    capability_tokens
                    and capability_tokens <= _tokens(item)
                    for item in declared
                )
                role_tokens = _tokens(
                    " ".join(
                        [
                            str(role.get("name", "")),
                            str(role.get("responsibility", "")),
                            " ".join(declared),
                        ]
                    )
                )
                overlap = len(capability_tokens & role_tokens)
                if explicit or overlap:
                    candidates.append(
                        ((100 if explicit else 0) + overlap, role_id)
                    )
            if candidates:
                _, selected_role = sorted(
                    candidates,
                    key=lambda item: (-item[0], item[1]),
                )[0]
                assignments[selected_role].append(capability)
        return assignments

    @staticmethod
    def _organization_coverage(
        required: list[str],
        proposals: list[dict[str, Any]],
        catalog: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        rows_by_name = {str(row["name"]): row for row in catalog}
        coverage: dict[str, list[str]] = {}
        for capability in required:
            capability_tokens = _tokens(capability)
            covered_roles: list[str] = []
            for proposal in proposals:
                role_id = str(proposal["role_id"])
                if capability not in proposal["assigned_required_capabilities"]:
                    continue
                if any(
                    capability_tokens
                    & set(rows_by_name[name]["tokens"])
                    for name in proposal["proposed_skill_refs"]
                    if name in rows_by_name
                ):
                    covered_roles.append(role_id)
            coverage[capability] = covered_roles
        return coverage

    @staticmethod
    def _catalog_row(skill: Skill) -> dict[str, Any]:
        metadata = dict(skill.metadata or {})
        capabilities = list(
            dict.fromkeys(
                [
                    *_list_field(metadata.get("capabilities", [])),
                    *_list_field(metadata.get("domains", [])),
                    *_list_field(metadata.get("tags", [])),
                ]
            )
        )
        identity_haystack = " ".join(
            [
                skill.name,
                skill.description,
                " ".join(capabilities),
            ]
        )
        return {
            "name": skill.name,
            "description": skill.description,
            "source_path": skill.source_path,
            "level": skill.level,
            "content_digest": _skill_digest(skill),
            "capabilities": capabilities,
            # Recommendations must be justified by discovery metadata. A word
            # mentioned incidentally in a long instruction body is not evidence
            # that the skill provides that capability.
            "tokens": _tokens(identity_haystack),
        }

    @staticmethod
    def _recommend_role(
        goal: str,
        role: dict[str, Any],
        catalog: list[dict[str, Any]],
        required: list[str],
        limit: int,
    ) -> dict[str, Any]:
        role_id = str(role.get("role_id") or role.get("id") or "").strip()
        existing = list(
            dict.fromkeys(
                _list_field(role.get("skill_refs", []))
            )
        )
        role_capabilities = list(
            dict.fromkeys(
                [
                    *_list_field(role.get("capabilities", [])),
                    *_list_field(role.get("required_capabilities", [])),
                ]
            )
        )
        query = " ".join(
            [
                goal,
                str(role.get("name", "")),
                str(role.get("responsibility", "")),
                " ".join(role_capabilities),
                " ".join(required),
            ]
        )
        query_tokens = _tokens(query)
        scored: list[tuple[float, dict[str, Any], list[str]]] = []
        for row in catalog:
            matched = sorted(query_tokens & set(row["tokens"]))
            explicit_matches = [
                capability
                for capability in [*required, *role_capabilities]
                if _tokens(capability) & set(row["tokens"])
            ]
            requested_capabilities = [*required, *role_capabilities]
            if requested_capabilities and not explicit_matches:
                continue
            exact_matches = [
                capability
                for capability in [*required, *role_capabilities]
                if capability.casefold() == str(row["name"]).casefold()
                or capability.casefold()
                in {
                    str(item).casefold()
                    for item in row["capabilities"]
                }
            ]
            score = (
                len(matched) / math.sqrt(max(1, len(query_tokens)))
                + (1.5 * len(explicit_matches))
                + (2.5 * len(exact_matches))
            )
            if row["name"] in existing:
                score += 0.25
            if score > 0:
                scored.append((score, row, list(dict.fromkeys(explicit_matches))))
        scored.sort(key=lambda item: (-item[0], item[1]["name"]))
        additions = [
            {
                "skill_ref": row["name"],
                "score": round(score, 4),
                "matched_terms": matched,
                "matched_capabilities": explicit,
                "description": row["description"],
                "content_digest": row["content_digest"],
                "source_path": row["source_path"],
                "level": row["level"],
            }
            for score, row, explicit in scored
            if row["name"] not in existing
            for matched in [sorted(query_tokens & set(row["tokens"]))]
        ][:limit]
        covered_tokens = set()
        for item in additions:
            covered_tokens.update(_tokens(" ".join(item["matched_capabilities"])))
        for existing_name in existing:
            existing_row: dict[str, Any] | None = None
            for catalog_item in catalog:
                if catalog_item["name"] == existing_name:
                    existing_row = catalog_item
                    break
            if existing_row:
                covered_tokens.update(existing_row["tokens"])
        gaps = [
            capability
            for capability in [*required, *role_capabilities]
            if not (_tokens(capability) & covered_tokens)
        ]
        missing_existing = [
            name
            for name in existing
            if not any(row["name"] == name for row in catalog)
        ]
        return {
            "role_id": role_id,
            "assigned_required_capabilities": list(required),
            "existing_skill_refs": existing,
            "recommended_additions": additions,
            "proposed_skill_refs": [
                *existing,
                *(item["skill_ref"] for item in additions),
            ],
            "capability_gaps": list(dict.fromkeys(gaps)),
            "missing_existing_refs": missing_existing,
        }
