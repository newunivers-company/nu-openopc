"""Fail-closed release dossier across dependency, outcome, shadow, and canary gates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping


def build_promotion_dossier(
    *,
    dependency_report: Mapping[str, Any],
    outcome_report: Mapping[str, Any],
    shadow_report: Mapping[str, Any],
    canary_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine independent evidence reports without weakening any source gate."""

    release_id = str(dependency_report.get("release_id", "") or "").strip()
    dependency_blockers = (
        []
        if dependency_report.get("ok", False) and release_id
        else [
            str(dependency_report.get("error", "") or "")
            or (
                "dependency release_id is missing"
                if dependency_report.get("ok", False)
                else "dependency release verification did not pass"
            )
        ]
    )
    dependency_ok = not dependency_blockers
    trusted_pair_count = int(outcome_report.get("trusted_pair_count", 0) or 0)
    outcome_blockers = [
        str(item) for item in outcome_report.get("blockers", []) or []
    ]
    if trusted_pair_count < 1:
        outcome_blockers.append("no trusted outcome pair evidence")
    outcome_ok = bool(
        outcome_report.get("promotion_eligible", False)
        and outcome_report.get("product_claim_ready", False)
        and outcome_report.get("status") == "pass"
        and not outcome_blockers
    )
    trusted_observation_count = int(
        shadow_report.get("trusted_observation_count", 0) or 0
    )
    shadow_blockers = [
        str(item) for item in shadow_report.get("blockers", []) or []
    ]
    if trusted_observation_count < 1:
        shadow_blockers.append("no trusted shadow observation evidence")
    shadow_ok = bool(
        shadow_report.get("promotion_ready", False)
        and shadow_report.get("status") == "pass"
        and not shadow_blockers
    )
    providers = dict(canary_report.get("providers", {}) or {})
    provider_blockers = {
        str(name): _provider_readiness_blockers(str(name), item)
        for name, item in sorted(providers.items())
    }
    canary_ok = bool(providers and not any(provider_blockers.values()))
    gates: dict[str, dict[str, Any]] = {
        "dependencies": {
            "passed": dependency_ok,
            "release_id": release_id,
            "blockers": dependency_blockers,
        },
        "outcomes": {
            "passed": outcome_ok,
            "trusted_pair_count": trusted_pair_count,
            "blockers": list(dict.fromkeys(outcome_blockers)),
        },
        "shadow": {
            "passed": shadow_ok,
            "trusted_observation_count": trusted_observation_count,
            "blockers": list(dict.fromkeys(shadow_blockers)),
        },
        "canary": {
            "passed": canary_ok,
            "providers": {
                name: {
                    "production_ready": bool(
                        item.get("production_ready", False)
                    ),
                    "readiness_state": str(
                        item.get("readiness_state", "") or ""
                    ),
                    "blockers": provider_blockers[str(name)],
                }
                for name, item in sorted(providers.items())
                if isinstance(item, Mapping)
            },
            "blockers": (
                ["no provider readiness evidence"]
                if not providers
                else [
                    f"{name}:{blocker}"
                    for name, blockers in provider_blockers.items()
                    for blocker in blockers
                ]
            ),
        },
    }
    blockers = [
        f"{gate_name}:{blocker}"
        for gate_name, gate in gates.items()
        if not gate["passed"]
        for blocker in (
            gate["blockers"]
            or [f"{gate_name} gate did not pass"]
        )
    ]
    promotion_ready = all(gate["passed"] for gate in gates.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if promotion_ready else "blocked",
        "promotion_ready": promotion_ready,
        "product_claim_ready": promotion_ready,
        "blockers": blockers,
        "gates": gates,
        "input_digests": {
            "dependencies": _digest(dependency_report),
            "outcomes": _digest(outcome_report),
            "shadow": _digest(shadow_report),
            "canary": _digest(canary_report),
        },
    }


def _provider_readiness_blockers(name: str, raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["provider readiness payload is not an object"]
    blockers = [str(item) for item in raw.get("blockers", []) or []]
    sample_target = max(1, int(raw.get("sample_target", 1) or 1))
    required_checks = {
        "production_ready": bool(raw.get("production_ready", False)),
        "readiness_state": raw.get("readiness_state") == "ready",
        "sample_floor": int(raw.get("samples", 0) or 0) >= sample_target,
        "observation_span": raw.get("observation_target_met") is True,
        "freshness": raw.get("freshness_target_met") is True,
        "failure_drills": raw.get("failure_drill_target_met") is True,
        "trend": bool(
            isinstance(raw.get("trend"), Mapping)
            and raw["trend"].get("ready") is True
        ),
    }
    failed = [check for check, passed in required_checks.items() if not passed]
    if failed and not blockers:
        blockers.append(
            f"provider readiness checks did not pass: {', '.join(failed)}"
        )
    return list(dict.fromkeys(blockers))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
