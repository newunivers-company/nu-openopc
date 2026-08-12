"""Secret-safe, read-mostly diagnostics used by ``opc doctor``."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Mapping

from opc.channels.provider_registry import ordered_provider_specs
from opc.core.config import OPCConfig
from opc.integrations.nu_llm_routing import NULlmRoutingBridge
from opc.layer3_agent.adapters.registry import ADAPTER_CLASSES


def _has_value(value: Any) -> bool:
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, False, [], {})


def channel_diagnostics(
    config: OPCConfig,
    *,
    runtime_state: Mapping[str, Any] | None = None,
    runtime_running: bool = False,
) -> dict[str, Any]:
    active_channels = {
        str(item) for item in list((runtime_state or {}).get("channels", []) or [])
    }
    rows: list[dict[str, Any]] = []
    for spec in ordered_provider_specs():
        channel = getattr(config.channels, spec.name)
        enabled = bool(getattr(channel, "enabled", False))
        missing = [
            field
            for field in spec.required_config_fields
            if not _has_value(getattr(channel, field, None))
        ]
        if spec.name == "email" and not bool(getattr(channel, "consent_granted", False)):
            missing.append("consent_granted")
        dependency_available = True
        if spec.required_package:
            try:
                dependency_available = importlib.util.find_spec(spec.required_package) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                dependency_available = False
        runtime_active = runtime_running and spec.name in active_channels
        configured = not missing
        issues: list[str] = []
        if enabled and missing:
            issues.append("missing required config: " + ", ".join(missing))
        if enabled and not dependency_available:
            issues.append(f"optional dependency is missing: {spec.required_package}")
        if enabled and spec.bridge_required and not configured:
            issues.append("bridge endpoint is not configured")
        rows.append({
            "name": spec.name,
            "enabled": enabled,
            "configured": configured,
            "delivery_mode": spec.delivery_mode,
            "bridge_required": spec.bridge_required,
            "dependency": spec.required_package or "builtin",
            "dependency_available": dependency_available,
            "required_config_fields": list(spec.required_config_fields),
            "missing_config_fields": missing,
            "runtime_active": runtime_active,
            "ready": bool(enabled and configured and dependency_available),
            "issues": issues,
            "setup_hint": spec.login_summary,
        })
    enabled_rows = [row for row in rows if row["enabled"]]
    return {
        "runtime_running": runtime_running,
        "runtime_pid": int((runtime_state or {}).get("pid", 0) or 0),
        "providers": rows,
        "enabled": len(enabled_rows),
        "ready": sum(bool(row["ready"]) for row in enabled_rows),
        "issues": [
            f"{row['name']}: {issue}"
            for row in enabled_rows
            for issue in row["issues"]
        ],
    }


def external_agent_diagnostics(config: OPCConfig, *, opc_home: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for name, adapter_cls in ADAPTER_CLASSES.items():
        configured = config.agents.agents.get(name)
        adapter = adapter_cls(config=configured)
        enabled = bool(adapter.config.enabled)
        binary = adapter.resolve_binary() if enabled else None
        command = adapter.configured_command()
        isolation_slug = adapter.agent_isolation_home_slug()
        isolated_home = opc_home / "agent_homes" / isolation_slug if isolation_slug else None
        issues = []
        if enabled and not binary:
            issues.append(f"executable not found: {command}")
        rows.append({
            "name": name,
            "enabled": enabled,
            "command": command,
            "available": bool(binary),
            "binary": str(binary or ""),
            "isolated_home": str(isolated_home or ""),
            "isolated_home_ready": bool(isolated_home and isolated_home.is_dir()),
            "ready": bool(enabled and binary),
            "issues": issues,
        })
    enabled_rows = [row for row in rows if row["enabled"]]
    return {
        "agents": rows,
        "enabled": len(enabled_rows),
        "ready": sum(bool(row["ready"]) for row in enabled_rows),
        "issues": [
            f"{row['name']}: {issue}"
            for row in enabled_rows
            for issue in row["issues"]
        ],
    }


def nu_llm_routing_diagnostics(config: OPCConfig, *, opc_home: Path) -> dict[str, Any]:
    """Report whether NU planning is active or intentionally falling back.

    Loading the router is local and network-free. A fail-open fallback is a
    visible notice rather than a Doctor failure because the configured default
    LLM transport remains the supported execution path.
    """
    routing = config.llm.nu_routing
    enabled = bool(routing.enabled)
    fail_open = bool(routing.fail_open)
    try:
        package_available = importlib.util.find_spec("nu_llm_routing_lib") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        package_available = False

    if not enabled:
        return {
            "enabled": False,
            "state": "disabled",
            "ready": True,
            "route_planning_available": False,
            "fallback_active": False,
            "fail_open": fail_open,
            "package_available": package_available,
            "config_path": "",
            "issues": [],
            "notices": [],
        }

    bridge = NULlmRoutingBridge(
        routing,
        opc_home=opc_home,
        emit_load_logs=False,
    )
    load_error = ""
    try:
        available = bridge.available
        config_path = str(bridge.config_path or "")
        load_error = bridge.load_error
    except Exception as exc:
        available = False
        config_path = ""
        exception_text = str(exc)
        load_error = (
            exception_text
            if "no router config was found" in exception_text
            else f"NU LLM router initialization failed: {type(exc).__name__}"
        )

    if "no router config was found" in load_error:
        safe_detail = (
            "NU LLM routing is enabled but no router config was found; set "
            "NU_LLM_ROUTER_CONFIG or llm.nu_routing.config_path"
        )
    elif "unavailable" in load_error:
        safe_detail = "nu-llm-routing-lib is unavailable"
    elif load_error:
        safe_detail = "the configured NU LLM router could not be loaded"
    else:
        safe_detail = ""

    if available:
        state = "active"
        issues: list[str] = []
        notices: list[str] = []
    elif fail_open:
        state = "fallback"
        issues = []
        notices = [
            safe_detail
            or "NU LLM route planning is unavailable; the default LLM transport remains active"
        ]
    else:
        state = "blocked"
        issues = [
            safe_detail
            or "NU LLM route planning is unavailable and fail_open is disabled"
        ]
        notices = []

    return {
        "enabled": True,
        "state": state,
        "ready": bool(available or fail_open),
        "route_planning_available": bool(available),
        "fallback_active": bool(not available and fail_open),
        "fail_open": fail_open,
        "package_available": package_available,
        "config_path": config_path,
        "issues": issues,
        "notices": notices,
    }


def filesystem_diagnostics(opc_home: Path, workspace: Path) -> dict[str, Any]:
    def writable_target(path: Path) -> dict[str, Any]:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = probe.exists() and os.access(probe, os.W_OK | os.X_OK)
        return {
            "path": str(path),
            "existing_ancestor": str(probe),
            "exists": path.exists(),
            "writable": bool(writable),
        }

    checks = {
        "opc_home": writable_target(Path(opc_home)),
        "config": writable_target(Path(opc_home) / "config"),
        "workspace": writable_target(Path(workspace)),
    }
    return {
        "checks": checks,
        "ready": all(item["writable"] for item in checks.values()),
        "issues": [
            f"{name} is not writable: {item['path']}"
            for name, item in checks.items()
            if not item["writable"]
        ],
    }
