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

    dependency_ok = bool(dependency_report.get("ok", False))
    outcome_ok = bool(
        outcome_report.get("promotion_eligible", False)
        and outcome_report.get("product_claim_ready", False)
        and outcome_report.get("status") == "pass"
    )
    shadow_ok = bool(
        shadow_report.get("promotion_ready", False)
        and shadow_report.get("status") == "pass"
    )
    providers = dict(canary_report.get("providers", {}) or {})
    canary_ok = bool(
        providers
        and all(
            bool(item.get("production_ready", False))
            for item in providers.values()
            if isinstance(item, Mapping)
        )
        and all(isinstance(item, Mapping) for item in providers.values())
    )
    gates = {
        "dependencies": {
            "passed": dependency_ok,
            "release_id": str(
                dependency_report.get("release_id", "") or ""
            ),
            "blockers": (
                []
                if dependency_ok
                else [
                    str(dependency_report.get("error", "") or "")
                    or "dependency release verification did not pass"
                ]
            ),
        },
        "outcomes": {
            "passed": outcome_ok,
            "trusted_pair_count": int(
                outcome_report.get("trusted_pair_count", 0) or 0
            ),
            "blockers": [
                str(item)
                for item in outcome_report.get("blockers", []) or []
            ],
        },
        "shadow": {
            "passed": shadow_ok,
            "trusted_observation_count": int(
                shadow_report.get("trusted_observation_count", 0) or 0
            ),
            "blockers": [
                str(item)
                for item in shadow_report.get("blockers", []) or []
            ],
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
                    "blockers": [
                        str(blocker)
                        for blocker in item.get("blockers", []) or []
                    ],
                }
                for name, item in sorted(providers.items())
                if isinstance(item, Mapping)
            },
            "blockers": (
                ["no provider readiness evidence"]
                if not providers
                else [
                    f"{name}:{blocker}"
                    for name, item in sorted(providers.items())
                    if isinstance(item, Mapping)
                    for blocker in item.get("blockers", []) or []
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
