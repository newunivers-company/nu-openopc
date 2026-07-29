"""Durability policy for high-volume runtime events."""

from __future__ import annotations

from typing import Any


# Streaming fragments are live transport, not durable recovery state. Final
# assistant messages, tool-call rows, and transcripts preserve completed
# content without retaining thousands of partial fragments.
TRANSIENT_RUNTIME_EVENT_TYPES = frozenset(
    {
        "assistant_delta",
        "thinking_delta",
        "tool_call_delta",
    }
)


def should_persist_runtime_event(event_type: str) -> bool:
    """Return whether a Native Runtime event belongs in ``runtime_events``."""

    return str(event_type or "").strip() not in TRANSIENT_RUNTIME_EVENT_TYPES


def should_persist_generic_event(
    event_type: str,
    payload: dict[str, Any] | None,
) -> bool:
    """Return whether an EventBus event belongs in the generic event table.

    Native Runtime persists its own canonical event row before publishing to
    the EventBus. A runtime session identifier therefore means the generic row
    would be a second copy used by neither recovery nor transcript playback.
    """

    if str(event_type or "").strip() != "runtime_event":
        return True
    return not str((payload or {}).get("runtime_session_id", "") or "").strip()
