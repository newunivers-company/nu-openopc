#!/usr/bin/env python3
"""Verify the stable NU package contracts consumed by OpenOPC.

This script imports the stable facades plus OpenOPC's explicitly pinned prompt
evaluation hook so it can run both in the development checkout and in an
isolated wheel-only environment.
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def verify(*, expected_llm: str, expected_resource: str) -> dict[str, Any]:
    from nu_resource_gen_lib import evaluate_prompt as resource_evaluate_prompt
    from nu_llm_routing_lib import api as llm
    from nu_resource_gen_lib import api as resource

    failures: list[str] = []
    if llm.__version__ != expected_llm:
        failures.append(f"nu-llm-routing-lib {llm.__version__} != {expected_llm}")
    if resource.__version__ != expected_resource:
        failures.append(f"nu-resource-gen-lib {resource.__version__} != {expected_resource}")
    if resource.STABLE_API_VERSION != "1":
        failures.append(
            f"nu-resource-gen-lib stable API {resource.STABLE_API_VERSION} != 1"
        )

    template = llm.config_template("ollama")
    schema = llm.read_router_schema()
    if not isinstance(template.get("providers"), dict) or not template["providers"]:
        failures.append("LLM router wheel template has no providers")
    if schema.get("type") != "object":
        failures.append("LLM router wheel schema is not an object schema")

    required_llm = ("ChatRequest", "Router", "load_router_from_dict", "route_metadata")
    required_resource = (
        "CandidateSpec",
        "ExecutionPolicy",
        "ResourceGenerator",
        "ResourceRequest",
        "evaluate_execution_policy",
        "list_provider_routes",
    )
    missing_llm = [name for name in required_llm if not hasattr(llm, name)]
    missing_resource = [name for name in required_resource if not hasattr(resource, name)]
    if missing_llm:
        failures.append(f"missing LLM stable exports: {missing_llm}")
    if missing_resource:
        failures.append(f"missing resource stable exports: {missing_resource}")
    if not callable(resource_evaluate_prompt):
        failures.append("missing pinned resource prompt-evaluation hook")

    report = {
        "compatible": not failures,
        "expected": {"llm": expected_llm, "resource": expected_resource},
        "installed": {"llm": llm.__version__, "resource": resource.__version__},
        "resource_stable_api": resource.STABLE_API_VERSION,
        "llm_template": "ollama",
        "failures": failures,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-llm", default="0.2.1")
    parser.add_argument("--expected-resource", default="0.2.0")
    args = parser.parse_args()
    report = verify(
        expected_llm=args.expected_llm,
        expected_resource=args.expected_resource,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
