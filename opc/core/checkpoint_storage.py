"""Size-safe storage projections for resumable Company checkpoints."""

from __future__ import annotations

import copy
from typing import Any


_ACTIVE_CHECKPOINT_STATUSES = frozenset({"pending", "resuming"})
_TERMINAL_RUNTIME_SNAPSHOT_KEYS = frozenset(
    {
        "active_work_items",
        "task_snapshots",
        "native_runtime_resume",
        "adapter_session_state",
        "external_sessions",
    }
)


def compact_work_item_checkpoint_metadata(value: Any) -> dict[str, Any]:
    """Keep only immutable assignment data from a WorkItem metadata snapshot.

    The WorkItem table remains authoritative for live metadata. Suspend
    checkpoints only need the employee assignment to restore execution
    identity; verification output, playbooks, and dossiers are already durable
    in their owning WorkItem and Task rows.
    """

    if not isinstance(value, dict):
        return {}
    employee_assignment = value.get("employee_assignment")
    if not isinstance(employee_assignment, dict) or not employee_assignment:
        return {}
    return {"employee_assignment": copy.deepcopy(employee_assignment)}


def compact_company_runtime_checkpoint_payload(value: Any) -> dict[str, Any]:
    """Upgrade a Company suspend/interruption payload to the bounded v3 form."""

    if not isinstance(value, dict):
        return {}
    payload = copy.deepcopy(value)
    for item in list(payload.get("active_work_items", []) or []):
        if isinstance(item, dict):
            item["metadata"] = compact_work_item_checkpoint_metadata(
                item.get("metadata")
            )
    for snapshot in list(payload.get("task_snapshots", []) or []):
        if not isinstance(snapshot, dict):
            continue
        work_item = snapshot.get("work_item")
        if isinstance(work_item, dict):
            work_item["metadata"] = compact_work_item_checkpoint_metadata(
                work_item.get("metadata")
            )
    payload["version"] = max(3, int(payload.get("version", 0) or 0))
    return payload


def compact_company_delivery_checkpoint_payload(value: Any) -> dict[str, Any]:
    """Remove Task/WorkItem duplicates from a delivery-review checkpoint.

    Delivery feedback is a human decision capability, not a second runtime
    snapshot. The referenced Task and WorkItem remain authoritative for member
    inbox/session state and full role/projection details.
    """

    if not isinstance(value, dict):
        return {}
    payload = copy.deepcopy(value)
    member_state = payload.pop("member_session_state", None)
    if isinstance(member_state, dict) and not payload.get("member_session_id"):
        payload["member_session_id"] = str(
            member_state.get("member_session_id", "") or ""
        ).strip()

    delivery_package = payload.get("delivery_package")
    if isinstance(delivery_package, dict):
        compacted_package = {
            key: copy.deepcopy(delivery_package[key])
            for key in (
                "executive_summary",
                "summary",
                "delivered_items",
                "artifact_manifest",
                "constraints",
                "risks",
                "open_issues",
                "next_steps",
                "role_ids",
                "source_projection_ids",
            )
            if delivery_package.get(key) not in (None, "", [], {})
        }
        role_task_map = delivery_package.get("role_task_map")
        if isinstance(role_task_map, dict) and role_task_map:
            compacted_package["role_ids"] = sorted(
                str(role_id).strip()
                for role_id in role_task_map
                if str(role_id).strip()
            )
        source_refs = delivery_package.get("source_projection_refs")
        if isinstance(source_refs, list):
            projection_ids = [
                str(item.get("projection_id", "") or "").strip()
                for item in source_refs
                if isinstance(item, dict)
                and str(item.get("projection_id", "") or "").strip()
            ]
            if projection_ids:
                compacted_package["source_projection_ids"] = list(
                    dict.fromkeys(projection_ids)
                )
        payload["delivery_package"] = compacted_package
    payload["storage_projection_version"] = max(
        2,
        int(payload.get("storage_projection_version", 0) or 0),
    )
    return payload


def compact_execution_checkpoint_payload(
    checkpoint_type: str,
    value: Any,
    *,
    status: str = "pending",
) -> dict[str, Any]:
    """Return the bounded durable projection for a checkpoint row."""

    payload = copy.deepcopy(value) if isinstance(value, dict) else {}
    checkpoint_kind = str(checkpoint_type or "").strip()
    status_value = str(status or "pending").strip().lower() or "pending"

    if checkpoint_kind in {
        "company_runtime_interrupted",
        "company_runtime_suspended",
    } and (
        "version" in payload
        or "active_work_items" in payload
        or "task_snapshots" in payload
    ):
        payload = compact_company_runtime_checkpoint_payload(payload)
    elif checkpoint_kind == "company_delivery_feedback":
        payload = compact_company_delivery_checkpoint_payload(payload)

    if status_value in _ACTIVE_CHECKPOINT_STATUSES:
        return payload

    if checkpoint_kind in {
        "company_runtime_interrupted",
        "company_runtime_suspended",
    }:
        archived_counts: dict[str, int] = {}
        for key in _TERMINAL_RUNTIME_SNAPSHOT_KEYS:
            removed = payload.pop(key, None)
            if isinstance(removed, (dict, list)) and removed:
                archived_counts[key] = len(removed)
        if archived_counts:
            payload["archived_runtime_snapshot_counts"] = archived_counts
    elif checkpoint_kind == "company_staffing_selection":
        staffing_pool = payload.pop("staffing_pool", None)
        if isinstance(staffing_pool, dict) and staffing_pool:
            payload["archived_staffing_pool_counts"] = {
                key: len(items)
                for key, items in staffing_pool.items()
                if isinstance(items, list)
            }
    return payload
